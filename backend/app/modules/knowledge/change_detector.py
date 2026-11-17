"""Knowledge Base Document Change Detector & Ingestion Scanner.

Scans the raw documents repository, computes SHA-256 hashes, detects NEW, MODIFIED,
UNTOUCHED, and DELETED documents, performs deduplication, executes existing-field tombstones,
and avoids redundant re-indexing with cross-process database-backed concurrency protection.
"""

import asyncio
from datetime import date, datetime, timezone
import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    DocumentCorruptError,
    KnowledgeLockTimeoutError,
)
from app.core.logging import configure_safe_logger
from app.models.document import Document, DocumentVersion
from app.modules.knowledge.sanitizer import IngestionSanitizer, SanitizationResult

logger = configure_safe_logger(logging.getLogger(__name__))

_sqlite_locks: Dict[Any, Dict[int, asyncio.Lock]] = {}


def _get_sqlite_lock(lock_id: int = 5432001) -> asyncio.Lock:
    """Retrieves or creates an asyncio.Lock bound to the current running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop not in _sqlite_locks:
        _sqlite_locks[loop] = {}
    if lock_id not in _sqlite_locks[loop]:
        _sqlite_locks[loop][lock_id] = asyncio.Lock()
    return _sqlite_locks[loop][lock_id]


class _SQLiteLockProxy:
    """Proxy that dynamically delegates to the current event loop's SQLite lock."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_get_sqlite_lock(5432001), name)

    async def acquire(self) -> bool:
        return await _get_sqlite_lock(5432001).acquire()

    def release(self) -> None:
        _get_sqlite_lock(5432001).release()

    def locked(self) -> bool:
        return _get_sqlite_lock(5432001).locked()


_sqlite_scan_lock = _SQLiteLockProxy()


class DatabaseAdvisoryLock:
    """Acquires a database-backed advisory lock with timeout and safe release.

    Uses transaction-scoped PostgreSQL pg_try_advisory_xact_lock across multi-process workers,
    ensuring the lock is automatically released upon commit, rollback, cancellation, or connection abort.
    Falls back to an asyncio lock for SQLite or in-memory test sessions.
    """

    def __init__(
        self,
        session: Optional[AsyncSession],
        lock_id: int = 5432001,
        timeout_seconds: float = 10.0,
        retry_interval_seconds: float = 0.2,
    ) -> None:
        self.session = session
        self.lock_id = lock_id
        self.timeout_seconds = timeout_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self._acquired = False
        self._is_postgres = False

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()

    async def acquire(self) -> bool:
        if not self.session:
            return True

        # Check dialect
        bind = self.session.get_bind()
        dialect_name = bind.dialect.name if hasattr(bind, "dialect") else ""
        self._is_postgres = (dialect_name == "postgresql")

        if not self._is_postgres:
            lock = _get_sqlite_lock(self.lock_id)
            try:
                await asyncio.wait_for(
                    lock.acquire(),
                    timeout=self.timeout_seconds,
                )
                self._acquired = True
                return True
            except asyncio.TimeoutError as exc:
                raise KnowledgeLockTimeoutError(
                    message=f"Timed out after {self.timeout_seconds}s waiting to acquire scan lock.",
                    detail={"lock_id": self.lock_id, "timeout_seconds": self.timeout_seconds},
                ) from exc

        # PostgreSQL transaction-scoped advisory lock with timeout polling
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                stmt = text("SELECT pg_try_advisory_xact_lock(:lock_id)")
                result = await self.session.execute(stmt, {"lock_id": self.lock_id})
                is_locked = bool(result.scalar_one_or_none())
                if is_locked:
                    self._acquired = True
                    logger.info("Acquired transaction-scoped PostgreSQL advisory lock %d.", self.lock_id)
                    return True
                # Rollback failed polling attempt so session does not hold idle-in-transaction
                await self.session.rollback()
            except Exception as db_exc:
                logger.warning("Error querying PostgreSQL advisory lock: %s", db_exc)
                try:
                    await self.session.rollback()
                except Exception:
                    pass

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= self.timeout_seconds:
                raise KnowledgeLockTimeoutError(
                    message=f"Timed out after {self.timeout_seconds}s waiting to acquire PostgreSQL advisory lock {self.lock_id}.",
                    detail={"lock_id": self.lock_id, "timeout_seconds": self.timeout_seconds},
                )

            await asyncio.sleep(self.retry_interval_seconds)

    async def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._is_postgres and self.session:
                if self.session.is_active and self.session.in_transaction():
                    await self.session.rollback()
                logger.info("Released transaction-scoped PostgreSQL advisory lock %d.", self.lock_id)
            elif not self._is_postgres:
                lock = _get_sqlite_lock(self.lock_id)
                if lock.locked():
                    lock.release()
        except Exception as exc:
            logger.error("Failed to release advisory lock %d: %s", self.lock_id, exc)
        finally:
            self._acquired = False


class DocumentChangeItem(BaseModel):
    """Represents a single document detected or evaluated during change scanning."""

    file_path: str
    file_name: str
    document_code: str
    change_type: str  # 'NEW', 'MODIFIED', 'UNTOUCHED', 'DELETED', 'DUPLICATE_CONTENT', 'QUARANTINED'
    sha256_hash: str
    file_size_bytes: int = 0
    mime_type: str = "text/plain"
    existing_version_number: Optional[int] = None
    target_version_number: Optional[int] = None
    quarantine_reason: Optional[str] = None
    error_code: Optional[str] = None
    sanitized_text: Optional[str] = None
    metadata_: Dict[str, Any] = Field(default_factory=dict)


class ChangeDetectionSummary(BaseModel):
    """Structured report of all detected changes across the scanned directory."""

    scanned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_directory: str
    total_files_scanned: int = 0
    new_documents: List[DocumentChangeItem] = Field(default_factory=list)
    modified_documents: List[DocumentChangeItem] = Field(default_factory=list)
    untouched_documents: List[DocumentChangeItem] = Field(default_factory=list)
    deleted_documents: List[DocumentChangeItem] = Field(default_factory=list)
    duplicate_documents: List[DocumentChangeItem] = Field(default_factory=list)
    quarantined_documents: List[DocumentChangeItem] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Returns True if any new, modified, or deleted documents require candidate rebuild."""
        return bool(self.new_documents or self.modified_documents or self.deleted_documents)


class ScannedFileCandidate:
    """Internal container for validated file before database state reconciliation."""

    def __init__(
        self,
        file_path: str,
        file_name: str,
        file_sha256: str,
        san_res: SanitizationResult,
    ) -> None:
        self.file_path = file_path
        self.file_name = file_name
        self.sha256_hash = file_sha256
        self.san_res = san_res
        self.doc_code = san_res.document_code or Path(file_name).stem


class ChangeDetector:
    """Detects new, modified, untouched, and deleted documents using SHA-256 hashes."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        sanitizer: Optional[IngestionSanitizer] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.sanitizer = sanitizer or IngestionSanitizer(settings=self.settings)
        self.raw_dir = os.path.abspath(self.settings.KNOWLEDGE_DOCS_DIR)
        os.makedirs(self.raw_dir, exist_ok=True)
        self.lock_timeout = getattr(self.settings, "KB_SCAN_LOCK_TIMEOUT_SECONDS", 10.0)
        self.lock_id = getattr(self.settings, "KB_SCAN_ADVISORY_LOCK_ID", 5432001)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Computes the cryptographic SHA-256 digest of a file safely in 64KB blocks."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Strips path traversal markers and unsafe control characters from a filename."""
        clean = os.path.basename(filename)
        clean = re.sub(r"[^\w\-.]", "_", clean)
        return clean.strip("._")

    async def scan_and_detect_changes(
        self,
        session: Optional[AsyncSession] = None,
        source_dir: Optional[str] = None,
        enable_semantic_security_check: bool = False,
        persist_staged_versions: bool = False,
        lock_timeout_seconds: Optional[float] = None,
    ) -> ChangeDetectionSummary:
        """Scans the document directory and compares files against active database versions.

        Executes file scanning, SHA-256 computation, and sanitization OUTSIDE the database lock.
        Acquires database advisory lock only during database reconciliation and version staging.
        """
        target_dir = os.path.abspath(source_dir or self.raw_dir)
        summary = ChangeDetectionSummary(raw_directory=target_dir)

        if not os.path.exists(target_dir):
            logger.warning("Document directory does not exist: %s", target_dir)
            return summary

        real_target_dir = os.path.realpath(target_dir)

        # =========================================================================
        # PHASE A: FILE SCANNING & SANITIZATION (OUTSIDE DATABASE LOCK)
        # =========================================================================
        seen_document_codes_in_scan: Set[str] = set()
        seen_hashes_in_this_scan: Dict[str, Tuple[str, str, Optional[str]]] = {}
        candidates: List[ScannedFileCandidate] = []
        scanned_count = 0

        # Sort files deterministically
        file_names = sorted(os.listdir(target_dir))
        for file_name in file_names:
            file_path = os.path.join(target_dir, file_name)

            # Skip hidden files or gitkeep
            if file_name.startswith(".") or file_name.endswith(".gitkeep"):
                continue

            # 1. Symlink and Path Traversal Verification
            if os.path.islink(file_path):
                logger.warning("Symlink detected and quarantined: %s", file_name)
                q_path = self.sanitizer.quarantine_file(
                    file_path=file_path,
                    sha256_hash="symlink",
                    reason="Symlinks are strictly prohibited in document storage.",
                    error_code="SYMLINK_NOT_ALLOWED",
                )
                summary.quarantined_documents.append(DocumentChangeItem(
                    file_path=file_path,
                    file_name=file_name,
                    document_code=Path(file_name).stem,
                    change_type="QUARANTINED",
                    sha256_hash="",
                    quarantine_reason="Symlinks are prohibited.",
                    error_code="SYMLINK_NOT_ALLOWED",
                ))
                continue

            if not os.path.isfile(file_path):
                continue

            real_file_path = os.path.realpath(file_path)
            if not real_file_path.startswith(real_target_dir + os.sep) and real_file_path != real_target_dir:
                logger.error("Path traversal attempt detected: %s", file_name)
                q_path = self.sanitizer.quarantine_file(
                    file_path=file_path,
                    sha256_hash="traversal",
                    reason="Path traversal outside document root detected.",
                    error_code="PATH_TRAVERSAL_ATTEMPT",
                )
                summary.quarantined_documents.append(DocumentChangeItem(
                    file_path=file_path,
                    file_name=file_name,
                    document_code=Path(file_name).stem,
                    change_type="QUARANTINED",
                    sha256_hash="",
                    quarantine_reason="Path traversal outside document root.",
                    error_code="PATH_TRAVERSAL_ATTEMPT",
                ))
                continue

            scanned_count += 1
            try:
                file_sha256 = self.compute_sha256(file_path)
            except Exception as exc:
                logger.error("Failed to read file '%s': %s", file_name, exc)
                continue

            # 2. Sanitize and validate file (size, MIME, magic bytes, heuristics, LLM, PII)
            san_res: SanitizationResult = await self.sanitizer.sanitize_document(
                file_path=file_path,
                sha256_hash=file_sha256,
                enable_semantic_check=enable_semantic_security_check,
            )

            if san_res.is_quarantined:
                change_item = DocumentChangeItem(
                    file_path=file_path,
                    file_name=file_name,
                    document_code=san_res.document_code or Path(file_name).stem,
                    change_type="QUARANTINED",
                    sha256_hash=file_sha256,
                    file_size_bytes=san_res.file_size_bytes,
                    mime_type=san_res.mime_type,
                    quarantine_reason=san_res.quarantine_reason,
                    error_code=san_res.error_code,
                )
                summary.quarantined_documents.append(change_item)
                continue

            doc_code = san_res.document_code or Path(file_name).stem

            # 3. Same-Batch Deduplication Check
            # If two files in the exact same scan claim the exact same document code
            if doc_code in seen_document_codes_in_scan:
                prior_file = next(
                    (p_file for p_hash, (p_file, p_code, _) in seen_hashes_in_this_scan.items() if p_code == doc_code),
                    file_path,
                )
                change_item = DocumentChangeItem(
                    file_path=file_path,
                    file_name=file_name,
                    document_code=doc_code,
                    change_type="DUPLICATE_CONTENT",
                    sha256_hash=file_sha256,
                    file_size_bytes=san_res.file_size_bytes,
                    mime_type=san_res.mime_type,
                    quarantine_reason=f"Duplicate document code identical to '{os.path.basename(prior_file)}'.",
                    error_code="DUPLICATE_HASH",
                )
                summary.duplicate_documents.append(change_item)
                continue

            # If identical content in the same scan without distinct document identities
            if file_sha256 in seen_hashes_in_this_scan:
                prior_file, prior_code, prior_title = seen_hashes_in_this_scan[file_sha256]
                is_copy = (
                    doc_code == prior_code
                    or (san_res.title == prior_title and (
                        "copy" in file_name.lower() or "copy" in os.path.basename(prior_file).lower()
                        or not re.match(r"^[A-Z]{2,10}-", doc_code)
                    ))
                )
                if is_copy:
                    change_item = DocumentChangeItem(
                        file_path=file_path,
                        file_name=file_name,
                        document_code=doc_code,
                        change_type="DUPLICATE_CONTENT",
                        sha256_hash=file_sha256,
                        file_size_bytes=san_res.file_size_bytes,
                        mime_type=san_res.mime_type,
                        quarantine_reason=f"Duplicate content identical to '{os.path.basename(prior_file)}'.",
                        error_code="DUPLICATE_HASH",
                    )
                    summary.duplicate_documents.append(change_item)
                    continue

            seen_document_codes_in_scan.add(doc_code)
            seen_hashes_in_this_scan[file_sha256] = (
                file_path,
                doc_code,
                san_res.title,
            )
            candidates.append(ScannedFileCandidate(file_path, file_name, file_sha256, san_res))

        summary.total_files_scanned = scanned_count

        # If no session provided, return inspected summary
        if not session:
            for cand in candidates:
                summary.new_documents.append(DocumentChangeItem(
                    file_path=cand.file_path,
                    file_name=cand.file_name,
                    document_code=cand.doc_code,
                    change_type="NEW",
                    sha256_hash=cand.sha256_hash,
                    file_size_bytes=cand.san_res.file_size_bytes,
                    mime_type=cand.san_res.mime_type,
                    target_version_number=1,
                    sanitized_text=cand.san_res.sanitized_text,
                ))
            return summary

        # =========================================================================
        # PHASE B: DATABASE RECONCILIATION & STAGING (INSIDE DATABASE ADVISORY LOCK)
        # =========================================================================
        effective_timeout = lock_timeout_seconds if lock_timeout_seconds is not None else self.lock_timeout
        async with DatabaseAdvisoryLock(
            session=session,
            lock_id=self.lock_id,
            timeout_seconds=effective_timeout,
        ):
            # Load authoritative database state
            stmt = select(Document).options(selectinload(Document.versions))
            result = await session.execute(stmt)
            all_docs = result.scalars().all()

            existing_docs_by_code: Dict[str, Document] = {d.document_code: d for d in all_docs}
            active_version_by_code: Dict[str, DocumentVersion] = {}
            for d in all_docs:
                for ver in d.versions:
                    if ver.is_current and ver.status == "ACTIVE":
                        active_version_by_code[d.document_code] = ver

            # Process candidates against database
            for cand in candidates:
                doc_code = cand.doc_code
                file_path = cand.file_path
                file_name = cand.file_name
                san_res = cand.san_res

                # TOCTOU Check: Verify file has not been modified between scan and staging
                if not os.path.exists(file_path):
                    continue
                current_sha256 = self.compute_sha256(file_path)
                if current_sha256 != cand.sha256_hash:
                    logger.error("File '%s' was modified during scanning (TOCTOU violation).", file_name)
                    q_path = self.sanitizer.quarantine_file(
                        file_path=file_path,
                        sha256_hash=current_sha256,
                        reason="File content modified on disk during scanning process (TOCTOU violation).",
                        error_code="FILE_MODIFIED_DURING_SCAN",
                    )
                    summary.quarantined_documents.append(DocumentChangeItem(
                        file_path=file_path,
                        file_name=file_name,
                        document_code=doc_code,
                        change_type="QUARANTINED",
                        sha256_hash=current_sha256,
                        quarantine_reason="File content modified on disk during scanning.",
                        error_code="FILE_MODIFIED_DURING_SCAN",
                    ))
                    continue

                if doc_code in existing_docs_by_code:
                    existing_doc = existing_docs_by_code[doc_code]
                    active_ver = active_version_by_code.get(doc_code)

                    # Check if unchanged: matches an existing version of THIS document
                    matching_ver = next((v for v in existing_doc.versions if v.sha256_hash == cand.sha256_hash), None)
                    if matching_ver is not None:
                        summary.untouched_documents.append(DocumentChangeItem(
                            file_path=file_path,
                            file_name=file_name,
                            document_code=doc_code,
                            change_type="UNTOUCHED",
                            sha256_hash=cand.sha256_hash,
                            file_size_bytes=san_res.file_size_bytes,
                            mime_type=san_res.mime_type,
                            existing_version_number=matching_ver.version_number,
                            target_version_number=matching_ver.version_number,
                            sanitized_text=san_res.sanitized_text,
                        ))
                        continue

                    # Document modified: allocate next version number
                    highest_version = max((v.version_number for v in existing_doc.versions), default=0)
                    next_version_num = highest_version + 1

                    summary.modified_documents.append(DocumentChangeItem(
                        file_path=file_path,
                        file_name=file_name,
                        document_code=doc_code,
                        change_type="MODIFIED",
                        sha256_hash=cand.sha256_hash,
                        file_size_bytes=san_res.file_size_bytes,
                        mime_type=san_res.mime_type,
                        existing_version_number=active_ver.version_number if active_ver else highest_version,
                        target_version_number=next_version_num,
                        sanitized_text=san_res.sanitized_text,
                        metadata_={
                            "title": san_res.title,
                            "category": san_res.category,
                            "product_line": san_res.product_line,
                            "region": san_res.region,
                            "access_level": san_res.access_level,
                        },
                    ))

                    if persist_staged_versions:
                        if not existing_doc.is_active:
                            existing_doc.is_active = True
                        staged_ver = DocumentVersion(
                            document_id=existing_doc.id,
                            version_number=next_version_num,
                            sha256_hash=cand.sha256_hash,
                            storage_uri=os.path.abspath(file_path),
                            status="STAGED",
                            effective_from=date.today(),
                            effective_until=None,
                            is_current=False,
                        )
                        session.add(staged_ver)
                        await session.flush()

                else:
                    # New Document: even if content hash exists in another document,
                    # distinct document identity and authorization scope (access_level, region, product_line)
                    # are strictly preserved without merging!
                    summary.new_documents.append(DocumentChangeItem(
                        file_path=file_path,
                        file_name=file_name,
                        document_code=doc_code,
                        change_type="NEW",
                        sha256_hash=cand.sha256_hash,
                        file_size_bytes=san_res.file_size_bytes,
                        mime_type=san_res.mime_type,
                        existing_version_number=None,
                        target_version_number=1,
                        sanitized_text=san_res.sanitized_text,
                        metadata_={
                            "tenant_id": getattr(san_res, "tenant_id", "default") or "default",
                            "title": san_res.title,
                            "category": san_res.category,
                            "product_line": san_res.product_line,
                            "region": san_res.region,
                            "access_level": san_res.access_level,
                        },
                    ))

                    if persist_staged_versions:
                        new_doc = Document(
                            tenant_id=getattr(san_res, "tenant_id", "default") or "default",
                            title=san_res.title or doc_code,
                            document_code=doc_code,
                            category=san_res.category or "POLICY",
                            product_line=san_res.product_line or "GLOBAL",
                            region=san_res.region or "GLOBAL",
                            access_level=san_res.access_level or "PUBLIC",
                            is_active=True,
                        )
                        session.add(new_doc)
                        await session.flush()

                        staged_ver = DocumentVersion(
                            document_id=new_doc.id,
                            version_number=1,
                            sha256_hash=cand.sha256_hash,
                            storage_uri=os.path.abspath(file_path),
                            status="STAGED",
                            effective_from=date.today(),
                            effective_until=None,
                            is_current=False,
                        )
                        session.add(staged_ver)
                        await session.flush()

            # 4. Deletion Detection & Existing-Field Tombstone Protocol
            for doc_code, doc in existing_docs_by_code.items():
                if doc.is_active and doc_code not in seen_document_codes_in_scan:
                    active_ver = active_version_by_code.get(doc_code)
                    summary.deleted_documents.append(DocumentChangeItem(
                        file_path="",
                        file_name=f"{doc_code}.md",
                        document_code=doc_code,
                        change_type="DELETED",
                        sha256_hash=active_ver.sha256_hash if active_ver else "",
                        existing_version_number=active_ver.version_number if active_ver else None,
                    ))

                    if persist_staged_versions:
                        # Apply Existing-Field Tombstone (Zero migrations)
                        doc.is_active = False
                        if active_ver:
                            active_ver.is_current = False
                            active_ver.status = "SUPERSEDED"
                            active_ver.effective_until = date.today()
                        await session.flush()

            if persist_staged_versions:
                await session.commit()

        logger.info(
            "Change scan complete on '%s': %d files scanned (New: %d, Mod: %d, Untouched: %d, Del: %d, Dup: %d, Quar: %d).",
            target_dir, scanned_count,
            len(summary.new_documents), len(summary.modified_documents),
            len(summary.untouched_documents), len(summary.deleted_documents),
            len(summary.duplicate_documents), len(summary.quarantined_documents),
        )
        return summary
