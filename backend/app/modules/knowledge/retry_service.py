"""Knowledge Base Document Ingestion Retry Service and Persistent Worker.

Implements Task 1, Criterion 10:
1. Automated, persistent retries at 15m, 30m, and 60m intervals.
2. Tenant isolation and idempotency protection via composite unique key (tenant_id, idempotency_key).
3. Database-backed worker leases, heartbeats, and CAS fencing.
4. Tenant-content concurrency serialization via PostgreSQL advisory locking (pg_try_advisory_xact_lock).
5. Poison-pill protection: separate tracking of operational worker interruptions (max 3) vs application retries.
6. Dead-letter state machine: permanent failures (validation, security, quarantine) -> immediate DEAD_LETTER;
   3rd retry failure -> DEAD_LETTER.
7. Safe candidate index generation and promotion coordination via shared promotion lock (5432002).
8. Process-restart and crash recovery.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    CandidateIndexBuildError,
    DocumentCorruptError,
    DocumentOversizedError,
    DocumentQuarantinedError,
    DocumentSecurityError,
    EmbeddingDimensionMismatchError,
    IndexIntegrityError,
    KnowledgeConcurrencyError,
    KnowledgeError,
    KnowledgeLockTimeoutError,
    MaintenanceWindowError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePayloadTooLargeError,
    StorageQuotaExceededError,
    StorageSecurityError,
)
from app.core.logging import configure_safe_logger
from app.db.base import utc_now
from app.models.document import Document, DocumentChunk, DocumentVersion, KBDocumentIngestionJob
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.change_detector import _get_sqlite_lock
from app.modules.knowledge.chunker import StructureAwareChunker
from app.modules.knowledge.maintenance import MaintenanceWindowService
from app.modules.knowledge.sanitizer import IngestionSanitizer
from app.services.embedding_service import get_embedding_service
from app.services.storage_service import (
    StorageService,
    get_storage_service,
    validate_storage_key,
    validate_tenant_id,
)

logger = configure_safe_logger(logging.getLogger(__name__))

# Retry Intervals and Maximums
RETRY_DELAYS_MINUTES: List[int] = [15, 30, 60]
MAX_RETRY_COUNT: int = 3
MAX_OPERATIONAL_INTERRUPTIONS: int = 3
DEFAULT_LEASE_DURATION_SECONDS: float = 300.0  # 5 minutes
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 30.0
KB_PROMOTION_ADVISORY_LOCK_ID: int = 5432002


def compute_idempotency_key(document_code: str, sha256_hash: str) -> str:
    """Computes deterministic 64-character idempotency key from doc code and content hash."""
    clean_code = (document_code or "").strip().upper()
    clean_hash = (sha256_hash or "").strip().lower()
    return hashlib.sha256(f"{clean_code}:{clean_hash}".encode("utf-8")).hexdigest()


def compute_content_lock_id(tenant_id: str, sha256_hash: str) -> int:
    """Computes deterministic 31-bit signed int for tenant-content PostgreSQL advisory locking."""
    clean_tenant = (tenant_id or "").strip()
    clean_hash = (sha256_hash or "").strip().lower()
    raw = hashlib.md5(f"kb_ingest:{clean_tenant}:{clean_hash}".encode("utf-8")).hexdigest()
    return int(raw[:15], 16) % (2**31 - 1)


def is_retryable_error(exc: Optional[Exception]) -> bool:
    """Classifies whether an exception is transient (retryable) or permanent (terminal).

    Permanent errors:
    - DocumentCorruptError, DocumentOversizedError, DocumentSecurityError, DocumentQuarantinedError
    - StorageSecurityError, StoragePayloadTooLargeError, StorageIntegrityError
    - EmbeddingDimensionMismatchError
    - ValueError, KeyError, TypeError, or text containing quarantine / security markers

    Transient / Retryable errors:
    - KnowledgeLockTimeoutError, KnowledgeConcurrencyError
    - MaintenanceWindowError
    - CandidateIndexBuildError
    - Database connection, timeout, or lock wait errors
    - asyncio.TimeoutError, TimeoutError, OSError, IOError
    """
    if exc is None:
        return True

    permanent_types = (
        DocumentCorruptError,
        DocumentOversizedError,
        DocumentSecurityError,
        DocumentQuarantinedError,
        StorageSecurityError,
        StoragePayloadTooLargeError,
        StorageIntegrityError,
        EmbeddingDimensionMismatchError,
        ValueError,
        KeyError,
        TypeError,
    )
    if isinstance(exc, permanent_types):
        return False

    msg = str(exc).lower()
    if any(k in msg for k in ("quarantine", "prompt injection", "traversal", "malformed", "security violation")):
        return False

    return True


class KBIngestionRetryService:
    """Persistent orchestration service for Knowledge Base document ingestion retries."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        storage_service: Optional[StorageService] = None,
        sanitizer: Optional[IngestionSanitizer] = None,
        chunker: Optional[StructureAwareChunker] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.storage_service = storage_service or get_storage_service()
        self.sanitizer = sanitizer or IngestionSanitizer(settings=self.settings)
        self.chunker = chunker or StructureAwareChunker()
        self.lease_duration = getattr(self.settings, "KB_RETRY_LEASE_DURATION_SECONDS", DEFAULT_LEASE_DURATION_SECONDS)
        self.max_interruptions = getattr(
            self.settings, "KB_RETRY_MAX_OPERATIONAL_INTERRUPTIONS", MAX_OPERATIONAL_INTERRUPTIONS
        )

    async def schedule_job(
        self,
        session: AsyncSession,
        tenant_id: str,
        document_code: str,
        sha256_hash: str,
        storage_key: str,
        failure_reason: str,
        exc: Optional[Exception] = None,
        document_id: Optional[uuid.UUID] = None,
        retry_metadata: Optional[Dict[str, Any]] = None,
    ) -> KBDocumentIngestionJob:
        """Schedules an initial failure for persistent retry or routes immediately to DEAD_LETTER."""
        v_tenant = validate_tenant_id(tenant_id)
        v_key = validate_storage_key(storage_key)
        idemp_key = compute_idempotency_key(document_code, sha256_hash)
        now = utc_now()
        retryable = is_retryable_error(exc)

        # 1. Check if job already exists under unique constraint (tenant_id, idempotency_key)
        stmt = select(KBDocumentIngestionJob).where(
            KBDocumentIngestionJob.tenant_id == v_tenant,
            KBDocumentIngestionJob.idempotency_key == idemp_key,
        )
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()

        if existing:
            logger.info(
                "Ingestion job already exists for tenant '%s', idempotency '%s' (status: %s).",
                v_tenant,
                idemp_key,
                existing.status,
            )
            # If in terminal or completed state, return existing
            if existing.status in ("COMPLETED", "PROCESSING", "RETRY_SCHEDULED"):
                return existing

            # If existing was DEAD_LETTER and now re-submitted with retryable:
            if not retryable:
                existing.failure_reason = failure_reason
                existing.updated_at = now
                await session.flush()
                return existing

        # 2. Determine initial state and schedule
        if not retryable:
            job = KBDocumentIngestionJob(
                tenant_id=v_tenant,
                document_id=document_id,
                document_code=document_code.strip(),
                sha256_hash=sha256_hash.strip().lower(),
                storage_key=v_key,
                idempotency_key=idemp_key,
                status="DEAD_LETTER",
                failure_type="PERMANENT",
                failure_reason=failure_reason,
                retry_count=0,
                interruption_count=0,
                initial_failure_at=now,
                dead_lettered_at=now,
                next_retry_at=None,
                retry_metadata=retry_metadata or {},
            )
        else:
            # Initial failure -> retry after 15 minutes
            job = KBDocumentIngestionJob(
                tenant_id=v_tenant,
                document_id=document_id,
                document_code=document_code.strip(),
                sha256_hash=sha256_hash.strip().lower(),
                storage_key=v_key,
                idempotency_key=idemp_key,
                status="RETRY_SCHEDULED",
                failure_type="RETRYABLE",
                failure_reason=failure_reason,
                retry_count=0,
                interruption_count=0,
                initial_failure_at=now,
                next_retry_at=now + timedelta(minutes=RETRY_DELAYS_MINUTES[0]),
                retry_metadata=retry_metadata or {},
            )

        session.add(job)
        await session.flush()
        return job

    async def claim_next_job(
        self,
        session: AsyncSession,
        worker_id: str,
        lease_duration_seconds: Optional[float] = None,
    ) -> Optional[KBDocumentIngestionJob]:
        """Atomically claims the next eligible retry job using FOR UPDATE SKIP LOCKED and advisory locks."""
        lease_sec = lease_duration_seconds or self.lease_duration
        now = utc_now()

        # Query exactly one eligible candidate
        bind = session.get_bind()
        is_postgres = (getattr(bind.dialect, "name", "") == "postgresql")

        stmt = (
            select(KBDocumentIngestionJob)
            .where(
                KBDocumentIngestionJob.status == "RETRY_SCHEDULED",
                KBDocumentIngestionJob.next_retry_at <= now,
            )
            .order_by(KBDocumentIngestionJob.next_retry_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        res = await session.execute(stmt)
        candidate = res.scalar_one_or_none()

        if not candidate:
            return None

        # Content advisory lock to serialize workers for the same tenant & content hash
        content_lock_id = compute_content_lock_id(candidate.tenant_id, candidate.sha256_hash)
        sqlite_lock = None
        if is_postgres:
            lock_stmt = text("SELECT pg_try_advisory_xact_lock(:lock_id)")
            lock_res = await session.execute(lock_stmt, {"lock_id": content_lock_id})
            acquired = bool(lock_res.scalar_one_or_none())
            if not acquired:
                logger.info(
                    "Advisory lock %d for tenant '%s' and hash '%s' held by another worker; skipping.",
                    content_lock_id,
                    candidate.tenant_id,
                    candidate.sha256_hash,
                )
                await session.rollback()
                return None
        else:
            sqlite_lock = _get_sqlite_lock(content_lock_id)
            if sqlite_lock.locked():
                await session.rollback()
                return None
            await sqlite_lock.acquire()

        try:
            # Verify no other job with same (tenant_id, sha256_hash) is currently PROCESSING
            active_check_stmt = select(func.count(KBDocumentIngestionJob.id)).where(
                KBDocumentIngestionJob.tenant_id == candidate.tenant_id,
                KBDocumentIngestionJob.sha256_hash == candidate.sha256_hash,
                KBDocumentIngestionJob.status == "PROCESSING",
                KBDocumentIngestionJob.id != candidate.id,
            )
            active_res = await session.execute(active_check_stmt)
            active_count = active_res.scalar() or 0
            if active_count > 0:
                logger.info("Another job is actively processing the same content hash; skipping.")
                await session.rollback()
                return None

            # Atomically update claimed candidate
            candidate.status = "PROCESSING"
            candidate.locked_by = worker_id
            candidate.locked_at = now
            candidate.heartbeat_at = now
            candidate.lease_expires_at = now + timedelta(seconds=lease_sec)
            candidate.claim_version += 1
            candidate.last_attempt_at = now
            candidate.updated_at = now

            await session.commit()
            return candidate
        except Exception:
            await session.rollback()
            raise
        finally:
            if sqlite_lock and sqlite_lock.locked():
                sqlite_lock.release()

    async def heartbeat(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        worker_id: str,
        claim_version: int,
        lease_duration_seconds: Optional[float] = None,
    ) -> bool:
        """Sends a periodic heartbeat extending worker lease. Returns False if fenced."""
        lease_sec = lease_duration_seconds or self.lease_duration
        now = utc_now()

        stmt = (
            update(KBDocumentIngestionJob)
            .where(
                KBDocumentIngestionJob.id == job_id,
                KBDocumentIngestionJob.locked_by == worker_id,
                KBDocumentIngestionJob.claim_version == claim_version,
                KBDocumentIngestionJob.status == "PROCESSING",
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_sec),
                updated_at=now,
            )
        )
        res = await session.execute(stmt)
        await session.commit()
        return res.rowcount > 0

    async def record_success(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        worker_id: str,
        claim_version: int,
        auto_commit: bool = True,
    ) -> bool:
        """Finalizes job as COMPLETED under CAS fencing with clock timestamp lease validity.

        Invariants:
        1. Verifies job_id, locked_by, claim_version, and status == 'PROCESSING'.
        2. Verifies lease_expires_at > CLOCK_TIMESTAMP() (PostgreSQL) or utc_now() (SQLite).
        3. If CAS check affects zero rows, immediately rolls back the session and returns False.
        4. If CAS check succeeds and auto_commit is True, commits the transaction.
        """
        bind = session.get_bind()
        is_pg = (getattr(bind.dialect, "name", "") == "postgresql")
        now_expr = func.clock_timestamp() if is_pg else utc_now()

        stmt = (
            update(KBDocumentIngestionJob)
            .where(
                KBDocumentIngestionJob.id == job_id,
                KBDocumentIngestionJob.locked_by == worker_id,
                KBDocumentIngestionJob.claim_version == claim_version,
                KBDocumentIngestionJob.status == "PROCESSING",
                KBDocumentIngestionJob.lease_expires_at > now_expr,
            )
            .values(
                status="COMPLETED",
                completed_at=now_expr,
                locked_by=None,
                lease_expires_at=None,
                updated_at=now_expr,
            )
        )
        res = await session.execute(stmt)
        if res.rowcount == 0:
            logger.warning(
                "CAS check failed for job %s (worker: %s, claim_version: %d). Lease expired or job superseded.",
                job_id,
                worker_id,
                claim_version,
            )
            await session.rollback()
            return False

        if auto_commit:
            await session.commit()
        return True

    async def record_failure(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        worker_id: str,
        claim_version: int,
        exc: Exception,
        failure_reason: str,
    ) -> bool:
        """Records an execution failure under CAS fencing, advancing retry schedule or dead-lettering."""
        now = utc_now()
        stmt = (
            select(KBDocumentIngestionJob)
            .where(
                KBDocumentIngestionJob.id == job_id,
                KBDocumentIngestionJob.locked_by == worker_id,
                KBDocumentIngestionJob.claim_version == claim_version,
            )
            .with_for_update()
        )
        res = await session.execute(stmt)
        job = res.scalar_one_or_none()

        if not job:
            logger.warning("Job %s was fenced or not held by worker %s (v%d).", job_id, worker_id, claim_version)
            await session.rollback()
            return False

        permanent = not is_retryable_error(exc)
        current_retries = job.retry_count

        # Schedule progression:
        # initial failure -> retry_count=0 (next_retry_at = +15m)
        # 1st retry fail  -> retry_count=1 (next_retry_at = +30m)
        # 2nd retry fail  -> retry_count=2 (next_retry_at = +60m)
        # 3rd retry fail  -> retry_count=3 -> DEAD_LETTER
        if permanent or current_retries >= 2:
            job.status = "DEAD_LETTER"
            job.failure_type = "PERMANENT" if permanent else "RETRYABLE"
            job.retry_count = min(3, current_retries + 1)
            job.dead_lettered_at = now
            job.next_retry_at = None
        else:
            job.status = "RETRY_SCHEDULED"
            job.failure_type = "RETRYABLE"
            job.retry_count = current_retries + 1
            # Delay mapping: after attempt 1 (retry_count becomes 1) -> 30 min; after attempt 2 (retry_count becomes 2) -> 60 min
            delay_minutes = RETRY_DELAYS_MINUTES[job.retry_count]
            job.next_retry_at = now + timedelta(minutes=delay_minutes)

        job.failure_reason = failure_reason
        job.locked_by = None
        job.lease_expires_at = None
        job.updated_at = now

        await session.commit()
        return True

    async def recover_expired_leases(self, session: AsyncSession) -> int:
        """Recovers jobs whose worker lease has expired, tracking operational interruptions."""
        now = utc_now()
        stmt = (
            select(KBDocumentIngestionJob)
            .where(
                KBDocumentIngestionJob.status == "PROCESSING",
                KBDocumentIngestionJob.lease_expires_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        res = await session.execute(stmt)
        expired_jobs = res.scalars().all()
        recovered_count = 0

        for job in expired_jobs:
            job.interruption_count += 1
            logger.warning(
                "Job %s lease expired (held by %s). Interruption count: %d/%d.",
                job.id,
                job.locked_by,
                job.interruption_count,
                self.max_interruptions,
            )
            if job.interruption_count >= self.max_interruptions:
                job.status = "DEAD_LETTER"
                job.failure_type = "INTERRUPTED"
                job.failure_reason = (
                    f"Exceeded maximum operational worker interruptions ({self.max_interruptions}). "
                    f"Last locked by worker '{job.locked_by}'."
                )
                job.dead_lettered_at = now
                job.next_retry_at = None
            else:
                job.status = "RETRY_SCHEDULED"
                # Reschedule retry for recovery
                job.next_retry_at = now + timedelta(seconds=10)

            job.locked_by = None
            job.lease_expires_at = None
            job.updated_at = now
            recovered_count += 1

        if recovered_count > 0:
            await session.commit()
        return recovered_count

    async def cleanup_unreferenced_scratch_dirs(
        self,
        session: AsyncSession,
        storage_dir: Path,
        max_age_seconds: float = 3600.0,
    ) -> Dict[str, Any]:
        """Cleans unreferenced candidate and scratch directories safely under promotion lock (5432002)."""
        bind = session.get_bind()
        is_postgres = (getattr(bind.dialect, "name", "") == "postgresql")

        # 1. Acquire promotion try-lock to avoid collision with IndexPromoter or rollback
        if is_postgres:
            stmt = text("SELECT pg_try_advisory_xact_lock(:lock_id)")
            res = await session.execute(stmt, {"lock_id": KB_PROMOTION_ADVISORY_LOCK_ID})
            if not bool(res.scalar_one_or_none()):
                logger.info("Candidate cleanup skipped: promotion advisory lock 5432002 currently held.")
                return {"status": "skipped", "reason": "promotion_active"}
        else:
            lock = _get_sqlite_lock(KB_PROMOTION_ADVISORY_LOCK_ID)
            if lock.locked():
                return {"status": "skipped", "reason": "promotion_active"}
            await lock.acquire()

        results = {"scanned": 0, "cleaned": 0, "errors": 0}
        try:
            if not storage_dir.exists():
                return results

            now_dt = utc_now()
            now_ts = datetime.now(timezone.utc).timestamp()

            # Gather active database references
            idx_stmt = select(KnowledgeIndex)
            idx_res = await session.execute(idx_stmt)
            all_indexes = idx_res.scalars().all()

            active_index_paths: Set[str] = set()
            active_scratch_prefixes: Set[str] = set()

            for k_idx in all_indexes:
                if k_idx.storage_path:
                    active_index_paths.add(os.path.normpath(k_idx.storage_path))
                    active_index_paths.add(os.path.dirname(os.path.normpath(k_idx.storage_path)))

                # Protect active BUILDING indexes whose lease has not expired
                if k_idx.status == "BUILDING":
                    exp_dt = k_idx.watchdog_expires_at
                    if exp_dt and exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if exp_dt and exp_dt > now_dt:
                        tag = k_idx.index_version_tag
                        active_scratch_prefixes.add(f".building_{tag}")
                        active_scratch_prefixes.add(f"candidate_{tag}")
                    elif exp_dt and exp_dt <= now_dt:
                        k_idx.status = "ABANDONED"
                        k_idx.watchdog_expires_at = None

            ver_stmt = select(DocumentVersion.storage_uri)
            ver_res = await session.execute(ver_stmt)
            active_version_paths = {os.path.normpath(p) for p in ver_res.scalars().all() if p}

            for item in storage_dir.iterdir():
                if not item.is_dir():
                    continue

                norm_item = os.path.normpath(str(item))
                if norm_item in active_index_paths or norm_item in active_version_paths:
                    continue

                name = item.name
                # Protect if it belongs to an active candidate writer
                if name in active_scratch_prefixes:
                    logger.info("Skipping directory '%s' belonging to active candidate writer.", name)
                    continue

                # Check if it is a scratch (.building_) or abandoned candidate directory or trash
                is_scratch = name.startswith(".building_") or name.startswith(".trash_") or name.startswith("candidate_") or ".failed" in name
                if not is_scratch:
                    continue

                results["scanned"] += 1
                try:
                    mtime = item.stat().st_mtime
                    if max_age_seconds > 0 and (now_ts - mtime < max_age_seconds):
                        continue  # Keep within grace period

                    # Two-phase deletion with fsync
                    tombstone_file = item / ".tombstone.json"
                    tombstone_data = {
                        "deleted_at": utc_now().isoformat(),
                        "directory": str(item),
                    }
                    with open(tombstone_file, "w", encoding="utf-8") as f:
                        json.dump(tombstone_data, f)
                        f.flush()
                        os.fsync(f.fileno())

                    # Atomic rename to trash before removal
                    trash_path = item.parent / f".trash_{uuid.uuid4().hex}"
                    os.replace(item, trash_path)
                    shutil.rmtree(trash_path, ignore_errors=True)
                    results["cleaned"] += 1
                except Exception as del_exc:
                    logger.error("Failed to clean unreferenced directory %s: %s", item, del_exc)
                    results["errors"] += 1
        finally:
            if not is_postgres:
                lock = _get_sqlite_lock(KB_PROMOTION_ADVISORY_LOCK_ID)
                if lock.locked():
                    lock.release()

        return results


class KBIngestionRetryWorker:
    """Dedicated background worker claiming and executing persistent Knowledge Base ingestion retries."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        retry_service: Optional[KBIngestionRetryService] = None,
        settings: Optional[Settings] = None,
        worker_id: Optional[str] = None,
        scan_interval_seconds: float = 30.0,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or default_settings
        self.retry_service = retry_service or KBIngestionRetryService(settings=self.settings)
        self.worker_id = worker_id or f"kb-retry-worker-{uuid.uuid4().hex[:8]}"
        self.scan_interval = scan_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def execute_job(
        self,
        job: KBDocumentIngestionJob,
        cancel_event: asyncio.Event,
    ) -> bool:
        """Executes ingestion workflow for a claimed retry job with heartbeat and CAS fence."""
        logger.info("Executing retry job %s (doc: %s, tenant: %s)", job.id, job.document_code, job.tenant_id)
        worker_id = self.worker_id
        claim_version = job.claim_version

        # Start background heartbeat task
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job.id, worker_id, claim_version, cancel_event)
        )

        try:
            async with self.session_factory() as session:
                # 1. Fetch document payload from storage abstraction
                payload_bytes, meta = await self.retry_service.storage_service.get_object(
                    tenant_id=job.tenant_id,
                    key=job.storage_key,
                    session=session,
                )

                if cancel_event.is_set():
                    logger.warning("Job %s cancelled due to lease loss during download.", job.id)
                    return False

                # 2. Verify SHA-256 integrity
                computed_hash = hashlib.sha256(payload_bytes).hexdigest()
                if computed_hash.lower() != job.sha256_hash.lower():
                    raise StorageIntegrityError(
                        f"Content hash mismatch: expected {job.sha256_hash}, computed {computed_hash}"
                    )

                # 3. Sanitize content and check security/quarantine
                text_content = payload_bytes.decode("utf-8", errors="replace")
                injection_reason = self.retry_service.sanitizer.detect_prompt_injection_heuristics(text_content)
                if injection_reason:
                    raise DocumentQuarantinedError(
                        message=f"Document quarantined: {injection_reason}",
                        detail={"quarantine_reason": injection_reason},
                    )

                struct_meta = self.retry_service.sanitizer.extract_structural_metadata(
                    text_content,
                    file_name=job.storage_key,
                )
                mask_res = self.retry_service.sanitizer.pii_masker.mask(text_content)
                clean_text = mask_res.masked_text

                # 4. Resolve or create parent Document entity
                doc_stmt = select(Document).where(
                    Document.tenant_id == job.tenant_id,
                    Document.document_code == job.document_code,
                )
                doc_res = await session.execute(doc_stmt)
                doc = doc_res.scalar_one_or_none()

                if not doc:
                    doc = Document(
                        tenant_id=job.tenant_id,
                        document_code=job.document_code,
                        title=struct_meta.get("title") or job.document_code,
                        category=struct_meta.get("category") or "POLICY",
                        product_line=struct_meta.get("product_line") or "GLOBAL",
                        region=struct_meta.get("region") or "GLOBAL",
                        access_level=struct_meta.get("access_level") or "PUBLIC",
                        is_active=True,
                    )
                    session.add(doc)
                    await session.flush()
                else:
                    if doc.tenant_id != job.tenant_id:
                        raise StorageSecurityError("Tenant isolation mismatch on parent document.")

                # 5. Check if version already exists
                ver_stmt = select(DocumentVersion).where(
                    DocumentVersion.document_id == doc.id,
                    DocumentVersion.sha256_hash == job.sha256_hash,
                )
                ver_res = await session.execute(ver_stmt)
                existing_ver = ver_res.scalar_one_or_none()

                if existing_ver:
                    logger.info("DocumentVersion with hash %s already exists.", job.sha256_hash)
                    success = await self.retry_service.record_success(
                        session=session,
                        job_id=job.id,
                        worker_id=worker_id,
                        claim_version=claim_version,
                        auto_commit=True,
                    )
                    return success

                # Calculate version number
                max_ver_stmt = select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
                    DocumentVersion.document_id == doc.id
                )
                max_ver_res = await session.execute(max_ver_stmt)
                next_ver_num = (max_ver_res.scalar() or 0) + 1

                # 6. Create staged DocumentVersion
                new_ver = DocumentVersion(
                    document_id=doc.id,
                    version_number=next_ver_num,
                    sha256_hash=job.sha256_hash,
                    storage_uri=f"storage://{job.tenant_id}/{job.storage_key}",
                    status="STAGED",
                    effective_from=datetime.now(timezone.utc).date(),
                    is_current=False,
                )
                session.add(new_ver)
                await session.flush()

                # 7. Chunk and extract text
                chunks = self.retry_service.chunker.chunk_document(
                    clean_text,
                    document_id=doc.id,
                    version_id=new_ver.id,
                    document_code=job.document_code,
                    title=doc.title,
                    category=doc.category,
                    product_line=doc.product_line,
                    region=doc.region,
                    access_level=doc.access_level,
                    tenant_id=job.tenant_id,
                )

                # 8. Insert chunks
                bind = session.get_bind()
                is_pg = (getattr(bind.dialect, "name", "") == "postgresql")
                if not is_pg:
                    max_id_res = await session.execute(text("SELECT COALESCE(MAX(faiss_vector_id), 0) FROM document_chunks"))
                    vector_id_counter = int(max_id_res.scalar() or 0) + 1
                else:
                    vector_id_counter = None

                for c in chunks:
                    chunk_meta = dict(c.metadata_) if c.metadata_ else {}
                    chunk_meta.update({
                        "document_code": job.document_code,
                        "category": doc.category,
                        "section_hierarchy": c.section_hierarchy,
                    })
                    if is_pg:
                        v_id_res = await session.execute(text("SELECT nextval('document_chunks_faiss_vector_id_seq')"))
                        vector_id = int(v_id_res.scalar_one())
                    else:
                        vector_id = vector_id_counter
                        vector_id_counter += 1

                    chunk_obj = DocumentChunk(
                        tenant_id=job.tenant_id,
                        version_id=new_ver.id,
                        chunk_index=c.chunk_index,
                        chunk_text=c.chunk_text,
                        token_count=c.token_count,
                        metadata_=chunk_meta,
                        faiss_vector_id=vector_id,
                    )
                    session.add(chunk_obj)

                await session.flush()

                if cancel_event.is_set():
                    logger.warning("Job %s cancelled due to lease expiration before commit.", job.id)
                    await session.rollback()
                    return False

                # 9. Atomically commit document records and job completion in the SAME transaction.
                # If CAS validation fails (lease expired or worker superseded), record_success
                # executes rollback, discarding DocumentVersion and DocumentChunks cleanly.
                success = await self.retry_service.record_success(
                    session=session,
                    job_id=job.id,
                    worker_id=worker_id,
                    claim_version=claim_version,
                    auto_commit=True,
                )
                if not success:
                    logger.warning(
                        "Job %s CAS validation failed during final commit; entire transaction rolled back.",
                        job.id,
                    )
                    return False

                return True

        except Exception as exc:
            logger.error("Job %s failed during ingestion execution: %s", job.id, exc, exc_info=True)
            async with self.session_factory() as err_session:
                await self.retry_service.record_failure(
                    session=err_session,
                    job_id=job.id,
                    worker_id=worker_id,
                    claim_version=claim_version,
                    exc=exc,
                    failure_reason=str(exc),
                )
            return False
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        claim_version: int,
        cancel_event: asyncio.Event,
    ) -> None:
        """Periodically extends lease while processing."""
        while not cancel_event.is_set():
            await asyncio.sleep(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
            try:
                async with self.session_factory() as session:
                    alive = await self.retry_service.heartbeat(
                        session=session,
                        job_id=job_id,
                        worker_id=worker_id,
                        claim_version=claim_version,
                    )
                    if not alive:
                        logger.warning("Heartbeat failed for job %s: lease lost or stolen.", job_id)
                        cancel_event.set()
                        break
            except Exception as exc:
                logger.error("Error in heartbeat loop: %s", exc)

    async def scan_and_process_batch(self, max_batch_size: int = 5) -> int:
        """Processes a single batch of eligible retry jobs. Used in testing and worker loop."""
        # 1. First recover any expired leases
        async with self.session_factory() as rec_session:
            recovered = await self.retry_service.recover_expired_leases(rec_session)
            if recovered > 0:
                logger.info("Recovered %d expired ingestion job lease(s).", recovered)

        processed = 0
        for _ in range(max_batch_size):
            async with self.session_factory() as claim_session:
                job = await self.retry_service.claim_next_job(claim_session, self.worker_id)
                if not job:
                    break

            cancel_event = asyncio.Event()
            await self.execute_job(job, cancel_event)
            processed += 1

        return processed

    def start_periodic_scanner(self) -> None:
        """Launches the background scanner loop task."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker_loop())
        logger.info("KBIngestionRetryWorker periodic scanner started (worker_id: %s).", self.worker_id)

    async def _worker_loop(self) -> None:
        """Periodic background execution loop."""
        while not self._stop_event.is_set():
            try:
                await self.scan_and_process_batch()
            except Exception as exc:
                logger.error("Unexpected error in KBIngestionRetryWorker loop: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.scan_interval)
            except asyncio.TimeoutError:
                pass

    async def stop_async(self) -> None:
        """Gracefully halts the background scanner loop."""
        if not self._running:
            return
        logger.info("Stopping KBIngestionRetryWorker...")
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("KBIngestionRetryWorker stopped.")
