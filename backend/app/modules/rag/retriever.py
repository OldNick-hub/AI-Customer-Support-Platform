"""Security-Aware Knowledge Retriever (Three-Layer Filtering + IDSelectorBatch).

Implements the 3-Layer Zero-Trust Retrieval Engine:
- Layer 1: PostgreSQL relational pre-filtering enforcing server-side identity,
  differentiated RBAC (AGENT vs SUPERVISOR vs ADMIN), unknown-region default-deny,
  temporal validity (effective_from <= query_date <= effective_until), and quarantine exclusion.
- Layer 2: FAISS IDSelectorBatch hardware-accelerated vector search over pre-authorized candidate IDs,
  preventing recall starvation.
- Layer 3: Application-level zero-trust validation gate re-verifying returned vector IDs,
  enforcing category-specific similarity thresholds, and detecting contradictory policy terms.
"""

from datetime import date, datetime, timezone
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import faiss
import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    RAGAuthorizationError,
    RAGPolicyConflictError,
    RAGSecurityQuarantineError,
)
from app.core.logging import configure_safe_logger
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.rag.security import SAFE_GLOBAL_CATEGORIES, SecurityContext
from app.services.embedding_service import EmbeddingService, get_embedding_service

logger = configure_safe_logger(logging.getLogger(__name__))

STATUTORY_POLICY_KEYWORDS: Set[str] = {
    "refund",
    "return",
    "warranty",
    "guarantee",
    "cooling-off",
    "cancellation",
    "cancel",
    "restocking fee",
    "return window",
    "statutory",
}


class RetrievedChunk(BaseModel):
    """Authoritative retrieved chunk with complete provenance and similarity metadata."""

    vector_id: int
    chunk_id: str
    version_id: str
    document_id: str
    document_code: str
    document_title: str
    version_number: int
    category: str
    product_line: str
    region: str
    access_level: str
    effective_from: date
    effective_until: Optional[date] = None
    chunk_index: int
    token_count: int
    chunk_text: str
    similarity_score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)
    sha256_hash: str


class RetrievalResult(BaseModel):
    """Complete retrieval evaluation outcome including gating and conflict signals."""

    query: str
    chunks: List[RetrievedChunk] = Field(default_factory=list)
    candidate_count: int = 0
    retrieved_count: int = 0
    top_similarity: float = 0.0
    is_refusal: bool = False
    refusal_reason: Optional[str] = None
    conflict_detected: bool = False
    effective_as_of_date_applied: date
    security_context: SecurityContext
    latency_ms: float = 0.0


class SecurityAwareRetriever:
    """Enterprise 3-Layer Security-Aware Vector & Relational Retriever."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        active_index_reader: Optional[ActiveIndexReader] = None,
        embedding_service: Optional[EmbeddingService] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.active_index_reader = active_index_reader or ActiveIndexReader(settings=self.settings)
        self.embedding_service = embedding_service or get_embedding_service()

    def get_calibrated_threshold(self, category: Optional[str]) -> float:
        """Returns the empirically calibrated similarity threshold by document category."""
        cat_upper = (category or "").upper()
        if "POLICY" in cat_upper:
            return self.settings.RAG_SIMILARITY_THRESHOLD_POLICY
        if cat_upper in ("TROUBLESHOOTING", "PRODUCT_MANUAL"):
            return self.settings.RAG_SIMILARITY_THRESHOLD_FAQ
        return self.settings.RAG_SIMILARITY_THRESHOLD_DEFAULT

    async def prune_candidates_sql(
        self,
        session: AsyncSession,
        security_context: SecurityContext,
        query_date: date,
        product_line: Optional[str] = None,
        document_code: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Layer 1: PostgreSQL relational pre-filtering enforcing server-side identity & temporal validity."""
        conditions = [
            Document.is_active.is_(True),
            DocumentVersion.status == "ACTIVE",
            Document.access_level.in_(security_context.permitted_access_tiers),
            # Temporal boundaries: effective_from <= query_date <= effective_until
            DocumentVersion.effective_from <= query_date,
            or_(
                DocumentVersion.effective_until.is_(None),
                DocumentVersion.effective_until >= query_date,
            ),
        ]

        # Unknown customer region: Strict Default-Deny on regional docs
        if security_context.is_safe_global_only:
            conditions.append(Document.region == "GLOBAL")
            conditions.append(Document.category.in_(SAFE_GLOBAL_CATEGORIES))
        else:
            conditions.append(Document.region.in_(security_context.permitted_regions))
            conditions.append(Document.category.in_(security_context.permitted_categories))

        # Explicit Document Code Pin (Tier 1 specificity)
        if document_code and document_code.strip():
            conditions.append(Document.document_code == document_code.strip())

        # Product Line filtering
        if security_context.permitted_product_lines is not None:
            allowed_products = set(security_context.permitted_product_lines) | {"GLOBAL"}
            conditions.append(Document.product_line.in_(allowed_products))
        elif product_line and product_line.strip():
            target_prod = product_line.strip().upper()
            conditions.append(Document.product_line.in_([target_prod, "GLOBAL"]))

        if category and category.strip():
            target_cat = category.strip().upper()
            conditions.append(Document.category == target_cat)

        stmt = (
            select(
                DocumentChunk.faiss_vector_id,
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.chunk_index,
                DocumentChunk.chunk_text,
                DocumentChunk.token_count,
                DocumentChunk.metadata_.label("chunk_meta"),
                DocumentVersion.id.label("version_id"),
                DocumentVersion.version_number,
                DocumentVersion.sha256_hash,
                DocumentVersion.effective_from,
                DocumentVersion.effective_until,
                Document.id.label("document_id"),
                Document.document_code,
                Document.title.label("document_title"),
                Document.category,
                Document.product_line,
                Document.region,
                Document.access_level,
            )
            .join(DocumentVersion, DocumentChunk.version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(and_(*conditions))
        )

        result = await session.execute(stmt)
        candidates: Dict[int, Dict[str, Any]] = {}

        for row in result.mappings():
            meta = row["chunk_meta"] or {}
            # Exclude quarantined chunks
            if meta.get("is_quarantined") is True or meta.get("quarantined") is True:
                logger.warning(
                    "Layer 1: Excluding quarantined chunk (vector_id=%d, doc=%s)",
                    row["faiss_vector_id"],
                    row["document_code"],
                )
                continue

            candidates[row["faiss_vector_id"]] = {
                "vector_id": row["faiss_vector_id"],
                "chunk_id": str(row["chunk_id"]),
                "chunk_index": row["chunk_index"],
                "chunk_text": row["chunk_text"],
                "token_count": row["token_count"],
                "metadata": meta,
                "version_id": str(row["version_id"]),
                "version_number": row["version_number"],
                "sha256_hash": row["sha256_hash"],
                "effective_from": row["effective_from"],
                "effective_until": row["effective_until"],
                "document_id": str(row["document_id"]),
                "document_code": row["document_code"],
                "document_title": row["document_title"],
                "category": row["category"],
                "product_line": row["product_line"],
                "region": row["region"],
                "access_level": row["access_level"],
            }

        return candidates

    def _is_statutory_policy_query(self, query: str) -> bool:
        """Determines if query inquires about statutory or jurisdiction-dependent policy terms."""
        q_lower = query.lower()
        return any(kw in q_lower for kw in STATUTORY_POLICY_KEYWORDS)

    def _detect_policy_conflicts(self, chunks: List[RetrievedChunk]) -> bool:
        """Detects whether applicable policy chunks at the same specificity tier assert conflicting terms.

        Checks for contradictory numbers/days (e.g., '14 days' vs '30 days') within policy chunks.
        """
        policy_chunks = [c for c in chunks if "POLICY" in c.category.upper()]
        if len(policy_chunks) < 2:
            return False

        # Group by (product_line, region) specificity tier
        tiers: Dict[Tuple[str, str], List[RetrievedChunk]] = {}
        for c in policy_chunks:
            key = (c.product_line.upper(), c.region.upper())
            tiers.setdefault(key, []).append(c)

        for key, group in tiers.items():
            if len(group) < 2:
                continue

            # Extract numbers associated with refund/return days or percentage limits
            extracted_numbers: Set[str] = set()
            for c in group:
                matches = re.findall(r"\b(\d+)\s*(?:days?|business days?|percent|%)\b", c.chunk_text, re.IGNORECASE)
                for m in matches:
                    extracted_numbers.add(m)

            # If multiple distinct numbers appear across different document codes in the same tier
            if len(extracted_numbers) > 1:
                # Confirm they come from different documents
                doc_codes = {c.document_code for c in group}
                if len(doc_codes) > 1:
                    logger.warning(
                        "Policy conflict detected in tier %s across documents %s: conflicting values %s",
                        key,
                        doc_codes,
                        extracted_numbers,
                    )
                    return True

        return False

    def _sort_by_specificity_cascade(
        self,
        chunks: List[RetrievedChunk],
        requested_product_line: Optional[str] = None,
        requested_document_code: Optional[str] = None,
        query_date: Optional[date] = None,
    ) -> List[RetrievedChunk]:
        """Applies the deterministic four-tier specificity cascade to rank retrieved chunks."""
        q_date = query_date or datetime.now(timezone.utc).date()

        def rank_key(chunk: RetrievedChunk) -> Tuple[int, int, int, int, int, float]:
            # Priority 1: Document code match
            code_score = 1 if requested_document_code and chunk.document_code == requested_document_code else 0

            # Priority 2: Product specificity (Exact product > GLOBAL)
            prod_score = 0
            if chunk.product_line != "GLOBAL":
                prod_score = 2 if requested_product_line and chunk.product_line.upper() == requested_product_line.upper() else 1

            # Priority 3: Regional specificity (Specific region > GLOBAL)
            region_score = 1 if chunk.region != "GLOBAL" else 0

            # Priority 4: Recency (version_number descending, temporal proximity to query_date)
            version_score = chunk.version_number
            date_diff = -abs((q_date - chunk.effective_from).days)

            # Similarity score
            sim = chunk.similarity_score

            return (code_score, prod_score, region_score, version_score, date_diff, sim)

        return sorted(chunks, key=rank_key, reverse=True)

    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        security_context: SecurityContext,
        effective_as_of_date: Optional[date] = None,
        product_line: Optional[str] = None,
        document_code: Optional[str] = None,
        category: Optional[str] = None,
        top_k: Optional[int] = None,
        faiss_index_override: Optional[faiss.Index] = None,
    ) -> RetrievalResult:
        """Executes the full 3-layer security-aware retrieval pipeline."""
        start_time = time.perf_counter()
        k = top_k or self.settings.RAG_TOP_K
        query_date = effective_as_of_date or datetime.now(timezone.utc).date()

        # Check for unknown region policy clarification requirement
        if security_context.is_safe_global_only and (
            (category and "POLICY" in category.upper()) or self._is_statutory_policy_query(query)
        ):
            logger.info("Refusal: Unknown customer region attempting statutory policy query.")
            elapsed = (time.perf_counter() - start_time) * 1000
            return RetrievalResult(
                query=query,
                chunks=[],
                candidate_count=0,
                retrieved_count=0,
                top_similarity=0.0,
                is_refusal=True,
                refusal_reason="UNKNOWN_REGION_POLICY_CLARIFICATION",
                conflict_detected=False,
                effective_as_of_date_applied=query_date,
                security_context=security_context,
                latency_ms=elapsed,
            )

        # Layer 1: PostgreSQL relational pre-filtering
        candidates = await self.prune_candidates_sql(
            session=session,
            security_context=security_context,
            query_date=query_date,
            product_line=product_line,
            document_code=document_code,
            category=category,
        )

        candidate_vector_ids = set(candidates.keys())
        if not candidate_vector_ids:
            elapsed = (time.perf_counter() - start_time) * 1000
            return RetrievalResult(
                query=query,
                chunks=[],
                candidate_count=0,
                retrieved_count=0,
                top_similarity=0.0,
                is_refusal=True,
                refusal_reason="NO_AUTHORIZED_CANDIDATES",
                conflict_detected=False,
                effective_as_of_date_applied=query_date,
                security_context=security_context,
                latency_ms=elapsed,
            )

        # Layer 2: FAISS Vector Search with IDSelectorBatch
        index = faiss_index_override or self.active_index_reader.get_index()
        if index is None or index.ntotal == 0:
            logger.warning("Active FAISS index is empty or unavailable.")
            elapsed = (time.perf_counter() - start_time) * 1000
            return RetrievalResult(
                query=query,
                chunks=[],
                candidate_count=len(candidate_vector_ids),
                retrieved_count=0,
                top_similarity=0.0,
                is_refusal=True,
                refusal_reason="INDEX_UNAVAILABLE",
                conflict_detected=False,
                effective_as_of_date_applied=query_date,
                security_context=security_context,
                latency_ms=elapsed,
            )

        # Generate query vector
        query_vec = await self.embedding_service.embed_text(query)
        query_mat = np.array([query_vec], dtype=np.float32)

        # Construct IDSelectorBatch with authorized vector IDs
        selector_ids = np.array(list(candidate_vector_ids), dtype=np.int64)
        id_selector = faiss.IDSelectorBatch(selector_ids)
        search_params = faiss.SearchParameters(sel=id_selector)

        search_k = min(k, len(candidate_vector_ids))
        distances, labels = index.search(query_mat, search_k, params=search_params)

        # Layer 3: Application-level Zero-Trust Validation Gate
        valid_chunks: List[RetrievedChunk] = []
        top_sim = 0.0

        for dist, vid in zip(distances[0], labels[0]):
            vid_int = int(vid)
            if vid_int == -1:
                continue

            # Zero-trust verification: Must exist in authorized candidates mapping
            if vid_int not in candidates:
                logger.error(
                    "SECURITY BREACH ATTEMPT: FAISS returned vector_id %d not in candidate set!",
                    vid_int,
                )
                continue

            sim_score = float(dist)
            if sim_score > top_sim:
                top_sim = sim_score

            cand_data = candidates[vid_int]

            chunk_obj = RetrievedChunk(
                vector_id=vid_int,
                chunk_id=cand_data["chunk_id"],
                version_id=cand_data["version_id"],
                document_id=cand_data["document_id"],
                document_code=cand_data["document_code"],
                document_title=cand_data["document_title"],
                version_number=cand_data["version_number"],
                category=cand_data["category"],
                product_line=cand_data["product_line"],
                region=cand_data["region"],
                access_level=cand_data["access_level"],
                effective_from=cand_data["effective_from"],
                effective_until=cand_data["effective_until"],
                chunk_index=cand_data["chunk_index"],
                token_count=cand_data["token_count"],
                chunk_text=cand_data["chunk_text"],
                similarity_score=sim_score,
                metadata=cand_data["metadata"],
                sha256_hash=cand_data["sha256_hash"],
            )
            valid_chunks.append(chunk_obj)

        if not valid_chunks:
            elapsed = (time.perf_counter() - start_time) * 1000
            return RetrievalResult(
                query=query,
                chunks=[],
                candidate_count=len(candidate_vector_ids),
                retrieved_count=0,
                top_similarity=0.0,
                is_refusal=True,
                refusal_reason="NO_RELEVANT_MATCHES",
                conflict_detected=False,
                effective_as_of_date_applied=query_date,
                security_context=security_context,
                latency_ms=elapsed,
            )

        # Calibrated threshold evaluation
        primary_category = valid_chunks[0].category if valid_chunks else category
        calibrated_threshold = self.get_calibrated_threshold(primary_category)

        # Check for low similarity / out-of-domain or borderline confidence
        if top_sim < calibrated_threshold:
            elapsed = (time.perf_counter() - start_time) * 1000
            if top_sim >= 0.65:
                # Inconclusive grey-zone: strictly abstain, zero tentative answers
                refusal_reason = "INCONCLUSIVE_RETRIEVAL_CONFIDENCE"
            else:
                # Clear out-of-domain query
                refusal_reason = "LOW_SIMILARITY_OUT_OF_DOMAIN"

            return RetrievalResult(
                query=query,
                chunks=valid_chunks,
                candidate_count=len(candidate_vector_ids),
                retrieved_count=len(valid_chunks),
                top_similarity=top_sim,
                is_refusal=True,
                refusal_reason=refusal_reason,
                conflict_detected=False,
                effective_as_of_date_applied=query_date,
                security_context=security_context,
                latency_ms=elapsed,
            )

        # Specificity cascade ranking
        ranked_chunks = self._sort_by_specificity_cascade(
            valid_chunks,
            requested_product_line=product_line,
            requested_document_code=document_code,
            query_date=query_date,
        )

        # Contradictory policy detection
        conflict = self._detect_policy_conflicts(ranked_chunks)
        is_refusal = False
        refusal_reason = None
        if conflict:
            is_refusal = True
            refusal_reason = "CONFLICTING_POLICY_EVIDENCE"

        elapsed = (time.perf_counter() - start_time) * 1000
        return RetrievalResult(
            query=query,
            chunks=ranked_chunks[:k],
            candidate_count=len(candidate_vector_ids),
            retrieved_count=len(ranked_chunks[:k]),
            top_similarity=top_sim,
            is_refusal=is_refusal,
            refusal_reason=refusal_reason,
            conflict_detected=conflict,
            effective_as_of_date_applied=query_date,
            security_context=security_context,
            latency_ms=elapsed,
        )

