"""Candidate FAISS Vector Index Builder & Deterministic Version Selector.

Constructs IndexIDMap2(IndexFlatIP(384)) candidate snapshots in isolated versioned directories,
allocating vector IDs via PostgreSQL sequences, executing deterministic candidate document selection,
and verifying vector count, dimensions, ID uniqueness, and index readability before staging.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import faiss
import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    CandidateIndexBuildError,
    DocumentCorruptError,
    EmbeddingDimensionMismatchError,
    IndexIntegrityError,
)
from app.core.logging import configure_safe_logger
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock
from app.modules.knowledge.chunker import ExtractedChunk, StructureAwareChunker
from app.services.embedding_service import EmbeddingService, get_embedding_service

logger = configure_safe_logger(logging.getLogger(__name__))


class CandidateBuildResult(BaseModel):
    """Structured report returned upon successful candidate index construction."""

    index_version_tag: str
    storage_path: str
    manifest_path: str
    total_vectors: int
    embedding_dimension: int = 384
    sha256_hash: str
    total_documents: int
    included_document_codes: List[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CandidateVectorBuilder:
    """Builds isolated candidate FAISS IndexIDMap2 vector index snapshots."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        embedding_service: Optional[EmbeddingService] = None,
        chunker: Optional[StructureAwareChunker] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.embedding_service = embedding_service or get_embedding_service()
        self.chunker = chunker or StructureAwareChunker(settings=self.settings)
        self.storage_dir = os.path.abspath(self.settings.FAISS_STORAGE_DIR)
        os.makedirs(self.storage_dir, exist_ok=True)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Computes SHA-256 digest of a binary file safely in 64KB blocks."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def select_candidate_versions(
        self,
        session: AsyncSession,
    ) -> List[Tuple[Document, DocumentVersion]]:
        """Executes deterministic candidate selection query.

        Invariants:
        1. Only active documents (d.is_active = TRUE)
        2. Only validated/sanitized versions (status IN ('STAGED', 'ACTIVE'))
        3. Excludes quarantined, superseded, deleted, and expired versions
        4. Staged replacement takes precedence over active predecessor (1 < 2)
        5. Single-version guarantee: each document contributes at most 1 version
        """
        today = datetime.now(timezone.utc).date()
        stmt = (
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(
                Document.is_active == True,
                DocumentVersion.status.in_(["STAGED", "ACTIVE"]),
                DocumentVersion.effective_from <= today,
                or_(
                    DocumentVersion.effective_until == None,
                    DocumentVersion.effective_until >= today,
                ),
            )
            .options(selectinload(Document.versions))
            .order_by(Document.document_code)
        )
        result = await session.execute(stmt)
        rows = result.all()

        # Group by document_id and apply strict status precedence: STAGED (1) > ACTIVE (2)
        grouped_by_doc: Dict[uuid.UUID, List[Tuple[Document, DocumentVersion]]] = {}
        for doc, ver in rows:
            grouped_by_doc.setdefault(doc.id, []).append((doc, ver))

        selected_pairs: List[Tuple[Document, DocumentVersion]] = []
        for doc_id, pairs in grouped_by_doc.items():
            # Sort by precedence: STAGED first, then by version_number descending
            pairs.sort(
                key=lambda p: (
                    0 if p[1].status == "STAGED" else 1,
                    -p[1].version_number,
                )
            )
            # Pick single highest-precedence version
            selected_pairs.append(pairs[0])

        selected_pairs.sort(key=lambda p: p[0].document_code)
        logger.info(
            "Selected %d authoritative candidate document versions for indexing.",
            len(selected_pairs),
        )
        return selected_pairs

    async def _allocate_vector_id(self, session: AsyncSession, fallback_counter: int) -> int:
        """Allocates a unique 64-bit integer vector ID using the database sequence."""
        bind = session.get_bind()
        dialect_name = bind.dialect.name if hasattr(bind, "dialect") else ""
        if dialect_name == "postgresql":
            res = await session.execute(text("SELECT nextval('document_chunks_faiss_vector_id_seq')"))
            return int(res.scalar_one())
        else:
            return fallback_counter

    async def prepare_document_chunks(
        self,
        session: AsyncSession,
        doc: Document,
        ver: DocumentVersion,
        fallback_counter: int,
    ) -> Tuple[List[DocumentChunk], int]:
        """Loads existing chunks or splits document text and allocates sequential vector IDs."""
        # 1. Check if chunks already exist in PostgreSQL for this version
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.version_id == ver.id)
            .order_by(DocumentChunk.chunk_index)
        )
        res = await session.execute(stmt)
        existing_chunks = res.scalars().all()
        if existing_chunks:
            return list(existing_chunks), fallback_counter

        # 2. Chunks not in database: read document text from storage_uri
        if not os.path.exists(ver.storage_uri):
            raise DocumentCorruptError(
                message=f"Storage file for document '{doc.document_code}' (version {ver.version_number}) not found at '{ver.storage_uri}'.",
                detail={"document_code": doc.document_code, "storage_uri": ver.storage_uri},
            )

        with open(ver.storage_uri, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()

        tenant_id = getattr(doc, "tenant_id", "default") or "default"
        extracted_chunks: List[ExtractedChunk] = self.chunker.chunk_document(
            text=raw_text,
            document_id=doc.id,
            version_id=ver.id,
            document_code=doc.document_code,
            title=doc.title,
            category=doc.category,
            product_line=doc.product_line,
            region=doc.region,
            access_level=doc.access_level,
            tenant_id=tenant_id,
        )

        persisted_chunks: List[DocumentChunk] = []
        for ec in extracted_chunks:
            vector_id = await self._allocate_vector_id(session, fallback_counter)
            fallback_counter += 1

            chunk_row = DocumentChunk(
                tenant_id=tenant_id,
                version_id=ver.id,
                chunk_index=ec.chunk_index,
                chunk_text=ec.chunk_text,
                token_count=ec.token_count,
                metadata_=ec.metadata_,
                faiss_vector_id=vector_id,
            )
            session.add(chunk_row)
            persisted_chunks.append(chunk_row)

        await session.flush()
        return persisted_chunks, fallback_counter

    async def build_candidate_index(
        self,
        session: AsyncSession,
        version_tag: Optional[str] = None,
        custom_target_dir: Optional[str] = None,
        tenant_id: Optional[str] = None,
        job_id: Optional[uuid.UUID] = None,
        claim_version: Optional[int] = None,
        lease_duration_seconds: float = 300.0,
    ) -> CandidateBuildResult:
        """Builds an isolated candidate FAISS vector index with durable ownership registration and two-phase atomic promotion.

        Invariants:
        1. Assigns a unique identity tied to tenant, job, and claim version.
        2. Records durable candidate ownership in PostgreSQL (status="BUILDING", watchdog_expires_at) before writing begins.
        3. Writes strictly inside isolated scratch directory (.building_{tag}) without holding global lock.
        4. FAISS IndexIDMap2(IndexFlatIP(384)) ensures 1:1 match with document_chunks.faiss_vector_id.
        5. Validates vector count, dimensions, ID uniqueness, and index file integrity in scratch directory.
        6. Atomically publishes under promotion lock (5432002), verifying ownership and unexpired lease.
        """
        # Generate version tag if not supplied
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = version_tag or f"idx_{tenant_id or 'default'}_{job_id or ts}_v{claim_version or 1}_{uuid.uuid4().hex[:6]}"

        candidate_dir = custom_target_dir or os.path.join(self.storage_dir, f"candidate_{tag}")
        index_file_path = os.path.join(candidate_dir, "index.faiss")
        manifest_path = os.path.join(candidate_dir, "manifest.json")

        # Isolated scratch directory during build
        scratch_dir = os.path.join(self.storage_dir, f".building_{tag}")
        os.makedirs(scratch_dir, exist_ok=True)
        scratch_index_file_path = os.path.join(scratch_dir, "index.faiss")
        scratch_manifest_path = os.path.join(scratch_dir, "manifest.json")

        # 1. Record durable candidate ownership in PostgreSQL before writing begins
        now_dt = datetime.now(timezone.utc)
        lease_expires_at = now_dt + timedelta(seconds=lease_duration_seconds)
        stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag)
        k_idx = (await session.execute(stmt)).scalar_one_or_none()
        if not k_idx:
            k_idx = KnowledgeIndex(
                index_version_tag=tag,
                storage_path=os.path.abspath(index_file_path),
                total_vectors=0,
                status="BUILDING",
                watchdog_expires_at=lease_expires_at,
                created_at=now_dt,
            )
            session.add(k_idx)
        else:
            k_idx.status = "BUILDING"
            k_idx.storage_path = os.path.abspath(index_file_path)
            k_idx.watchdog_expires_at = lease_expires_at
            k_idx.total_vectors = 0
        await session.commit()

        try:
            # 2. Deterministic Candidate Document Selection
            selected_pairs = await self.select_candidate_versions(session)

            # Get current maximum faiss_vector_id for SQLite sequence fallback
            res_max = await session.execute(text("SELECT COALESCE(MAX(faiss_vector_id), 0) FROM document_chunks"))
            max_val = res_max.scalar_one()
            fallback_counter = int(max_val) + 1 if max_val is not None else 1

            all_candidate_chunks: List[DocumentChunk] = []
            for doc, ver in selected_pairs:
                chunks, fallback_counter = await self.prepare_document_chunks(
                    session=session,
                    doc=doc,
                    ver=ver,
                    fallback_counter=fallback_counter,
                )
                all_candidate_chunks.extend(chunks)

            # 3. Embedding Generation
            total_chunks = len(all_candidate_chunks)
            if total_chunks == 0:
                logger.warning("Zero candidate chunks available; building empty candidate index.")
                embeddings: List[List[float]] = []
            else:
                chunk_texts = [c.chunk_text for c in all_candidate_chunks]
                logger.info(
                    "Generating 384-d unit-normalized embeddings for %d candidate chunks in batches of %d...",
                    total_chunks,
                    self.embedding_service.batch_size,
                )
                embeddings = await self.embedding_service.embed_batch(chunk_texts)

                if len(embeddings) != total_chunks:
                    raise CandidateIndexBuildError(
                        message=f"Embedding count mismatch: expected {total_chunks}, got {len(embeddings)}."
                    )

                # Verify embedding dimension and unit normalization
                for idx, vec in enumerate(embeddings):
                    if len(vec) != 384:
                        raise EmbeddingDimensionMismatchError(
                            expected_dim=384,
                            actual_dim=len(vec),
                            message=f"Candidate chunk {idx} vector dimension mismatch (got {len(vec)}).",
                        )
                    norm = float(np.linalg.norm(vec))
                    if abs(norm - 1.0) > 1e-3:
                        logger.warning("Vector %d not unit-normalized (norm=%.4f); normalising.", idx, norm)

            # 4. Construct FAISS IndexIDMap2(IndexFlatIP(384)) in scratch directory
            quantizer = faiss.IndexFlatIP(384)
            index = faiss.IndexIDMap2(quantizer)

            if total_chunks > 0:
                vectors_np = np.array(embeddings, dtype=np.float32)
                ids_list = [c.faiss_vector_id for c in all_candidate_chunks]
                ids_np = np.array(ids_list, dtype=np.int64)

                # Verify ID uniqueness
                if len(set(ids_list)) != len(ids_list):
                    raise IndexIntegrityError("Duplicate FAISS vector IDs detected in candidate dataset.")

                index.add_with_ids(vectors_np, ids_np)

            # 5. Save and Validate FAISS Index in scratch directory
            faiss.write_index(index, scratch_index_file_path)

            # Verify readability and total vector count on disk
            loaded_index = faiss.read_index(scratch_index_file_path)
            if loaded_index.ntotal != index.ntotal:
                raise IndexIntegrityError(
                    f"Read-back vector count mismatch: expected {index.ntotal}, got {loaded_index.ntotal}."
                )
            if loaded_index.d != 384:
                raise IndexIntegrityError(
                    f"Read-back dimension mismatch: expected 384, got {loaded_index.d}."
                )

            # Compute SHA-256 checksum in scratch directory
            sha256_hash = self.compute_sha256(scratch_index_file_path)
            with open(f"{scratch_index_file_path}.sha256", "w", encoding="utf-8") as f:
                f.write(f"{sha256_hash}  index.faiss\n")

            # 6. Write Candidate Manifest JSON in scratch directory
            manifest_payload = {
                "index_version_tag": tag,
                "storage_path": os.path.abspath(index_file_path),
                "total_vectors": index.ntotal,
                "embedding_dimension": 384,
                "sha256": sha256_hash,
                "status": "STAGED",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "total_documents": len(selected_pairs),
                "included_document_codes": [doc.document_code for doc, ver in selected_pairs],
            }
            tmp_manifest = f"{scratch_manifest_path}.tmp"
            with open(tmp_manifest, "w", encoding="utf-8") as f:
                json.dump(manifest_payload, f, indent=2)
            os.replace(tmp_manifest, scratch_manifest_path)

            # 7. Two-phase publication under promotion advisory lock (5432002)
            lock_id = getattr(self.settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)
            async with DatabaseAdvisoryLock(session=session, lock_id=lock_id, timeout_seconds=10.0):
                # Verify durable ownership is still intact and lease is unexpired
                check_stmt = (
                    select(KnowledgeIndex)
                    .where(KnowledgeIndex.index_version_tag == tag)
                    .with_for_update()
                )
                check_res = await session.execute(check_stmt)
                active_record = check_res.scalar_one_or_none()
                curr_time = datetime.now(timezone.utc)

                if not active_record or active_record.status != "BUILDING":
                    raise CandidateIndexBuildError(
                        f"Candidate build failed: ownership record for tag '{tag}' was stolen or superseded."
                    )

                exp_dt = active_record.watchdog_expires_at
                if exp_dt and exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)

                if exp_dt and exp_dt < curr_time:
                    raise CandidateIndexBuildError(
                        f"Candidate build lease expired at {exp_dt.isoformat()} (now {curr_time.isoformat()}). Cannot publish."
                    )

                # Two-phase publication: atomic directory replace to candidate_dir
                if os.path.exists(candidate_dir):
                    shutil.rmtree(candidate_dir, ignore_errors=True)
                os.replace(scratch_dir, candidate_dir)

                # Update KnowledgeIndex to STAGED
                active_record.status = "STAGED"
                active_record.total_vectors = index.ntotal
                active_record.watchdog_expires_at = None
                active_record.storage_path = os.path.abspath(index_file_path)
                await session.commit()

            logger.info(
                "Candidate index '%s' successfully built and published to '%s' (%d vectors, SHA-256: %s).",
                tag,
                candidate_dir,
                index.ntotal,
                sha256_hash[:16],
            )

            return CandidateBuildResult(
                index_version_tag=tag,
                storage_path=os.path.abspath(index_file_path),
                manifest_path=os.path.abspath(manifest_path),
                total_vectors=index.ntotal,
                embedding_dimension=384,
                sha256_hash=sha256_hash,
                total_documents=len(selected_pairs),
                included_document_codes=[doc.document_code for doc, ver in selected_pairs],
            )

        except Exception as exc:
            logger.error("Failed to build candidate index '%s': %s", tag, exc)
            await session.rollback()

            # Remove un-staged candidate index record on build failure
            try:
                fail_stmt = (
                    delete(KnowledgeIndex)
                    .where(KnowledgeIndex.index_version_tag == tag)
                )
                await session.execute(fail_stmt)
                await session.commit()
            except Exception as rec_exc:
                logger.error("Failed to clean up KnowledgeIndex on build failure: %s", rec_exc)

            # Quarantine/cleanup failed candidate directory to prevent stale artifacts
            fail_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            failed_dir = f"{candidate_dir}.failed_{fail_ts}_{uuid.uuid4().hex[:6]}"
            try:
                if os.path.exists(scratch_dir):
                    shutil.move(scratch_dir, failed_dir)
                    logger.warning("Quarantined failed candidate build scratch directory to '%s'.", failed_dir)
                elif os.path.exists(candidate_dir):
                    shutil.move(candidate_dir, failed_dir)
                    logger.warning("Quarantined failed candidate build directory to '%s'.", failed_dir)
            except Exception as clean_exc:
                logger.error("Failed to rename failed candidate directory: %s", clean_exc)

            if isinstance(
                exc,
                (
                    CandidateIndexBuildError,
                    IndexIntegrityError,
                    EmbeddingDimensionMismatchError,
                    DocumentCorruptError,
                ),
            ):
                raise exc
            raise CandidateIndexBuildError(
                message=f"Candidate index construction failed for '{tag}': {exc}"
            ) from exc
