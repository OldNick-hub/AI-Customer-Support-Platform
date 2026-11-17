"""Evidence Storage Retention Lifecycle Manager.

Handles automated, auditable, and recoverable cleanup of expired evidence files:
1. Expiration discovery: queries `evidence_files` where `retention_expires_at <= utc_now()` and `status not in ('PURGED', 'PURGE_FAILED')`.
2. Concurrency safety: PostgreSQL row-locking `with_for_update(skip_locked=True)` prevents multiple workers from double-purging.
3. Path containment: verifies file path is strictly within `settings.EVIDENCE_STORAGE_DIR`. Prevents path traversal and symlink escapes.
4. Idempotent deletion: safely removes encrypted file and related caches; if file is already deleted on disk, marks DB record as PURGED without error.
5. Bounded Retries & State Machine: transitions failing files up to `max_retries` (default 3), then moves them to `PURGE_FAILED`.
6. Non-blocking I/O: offloads filesystem operations to background threads via `asyncio.to_thread`.
7. Periodic background worker & administrative trigger endpoints with recovery mechanisms.
"""

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.db.base import utc_now
from app.models.evidence import EvidenceFile
from app.modules.evidence.storage import EvidenceStorageManager, default_evidence_storage

logger = configure_safe_logger(logging.getLogger(__name__))


class StorageLifecycleManager:
    """Manages secure, auditable, and recoverable evidence file retention lifecycles."""

    DEFAULT_BATCH_SIZE: int = 50
    MAX_BATCH_SIZE: int = 500
    DEFAULT_MAX_RETRIES: int = 3

    def __init__(
        self,
        storage: Optional[EvidenceStorageManager] = None,
        max_batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.storage = storage or default_evidence_storage
        self.max_batch_size = min(max_batch_size, self.MAX_BATCH_SIZE)
        self.max_retries = max(1, max_retries)

    async def discover_expired_evidence(
        self,
        db: AsyncSession,
        batch_size: Optional[int] = None,
        include_failed: bool = False,
    ) -> List[EvidenceFile]:
        """Queries expired evidence records using PostgreSQL SKIP LOCKED to prevent races.

        Excludes PURGED records and records that reached terminal PURGE_FAILED state,
        unless include_failed is explicitly requested.
        """
        effective_batch = min(batch_size or self.max_batch_size, self.MAX_BATCH_SIZE)
        now_utc = utc_now()

        excluded_statuses = ["PURGED"]
        if not include_failed:
            excluded_statuses.append("PURGE_FAILED")

        stmt = (
            select(EvidenceFile)
            .where(
                EvidenceFile.retention_expires_at <= now_utc,
                EvidenceFile.status.notin_(excluded_statuses),
            )
        )
        if not include_failed:
            stmt = stmt.where(EvidenceFile.purge_retry_count < self.max_retries)

        stmt = (
            stmt.order_by(EvidenceFile.retention_expires_at.asc())
            .limit(effective_batch)
        )

        bind = db.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        result = await db.execute(stmt)
        return list(result.scalars().all())

    def _validate_safe_path(self, storage_uri: str) -> Tuple[bool, Optional[Path], Optional[str]]:
        """Verifies storage path is strictly within evidence storage root and not a symlink escape."""
        try:
            target_path = Path(storage_uri).resolve()
            base_dir = self.storage.base_dir.resolve()

            if not target_path.is_relative_to(base_dir):
                return False, None, f"Storage path escapes base directory '{base_dir}'"

            # Check for symlink escape
            if target_path.is_symlink():
                real_target = target_path.resolve()
                if not real_target.is_relative_to(base_dir):
                    return False, None, "Symlink target resolves outside authorized storage root"

            return True, target_path, None
        except Exception as exc:
            return False, None, f"Path resolution error: {str(exc)}"

    async def purge_single_record(
        self,
        db: AsyncSession,
        evidence: EvidenceFile,
    ) -> Tuple[bool, Optional[str]]:
        """Safely unlinks disk payload and transitions database status to PURGED.

        Idempotent: If the file is already missing on disk, the database status is
        still marked PURGED successfully.
        Bounded: Increments purge_retry_count on I/O failure and transitions to
        PURGE_FAILED once max_retries is reached.
        Permanent violations (e.g. symlinks/path traversal) fail closed immediately to PURGE_FAILED.
        Non-blocking: Uses asyncio.to_thread for filesystem I/O.
        """
        evidence_id = evidence.id
        storage_uri = evidence.storage_uri
        evidence.last_purge_attempt_at = utc_now()

        # 1. Path safety check
        is_safe, target_path, error_msg = self._validate_safe_path(storage_uri)
        if not is_safe:
            logger.error("Security violation: purge aborted for evidence %s: %s", evidence_id, error_msg)
            evidence.status = "PURGE_FAILED"
            evidence.processing_error = f"PURGE_SECURITY_ERROR: {error_msg}"
            return False, error_msg

        # 2. Disk Unlink using asyncio.to_thread to avoid blocking event loop
        def _sync_unlink(path: Path) -> Tuple[bool, Optional[str]]:
            try:
                if path.is_file():
                    path.unlink()
                    return True, None
                elif not path.exists():
                    return True, None
                return False, f"Path is not a regular file: {path}"
            except Exception as exc:
                return False, str(exc)

        success, unlink_err = await asyncio.to_thread(_sync_unlink, target_path)

        if not success:
            err_str = f"File deletion failed: {unlink_err}"
            logger.error("Failed to delete evidence file %s: %s", evidence_id, err_str)
            evidence.purge_retry_count += 1
            if evidence.purge_retry_count >= self.max_retries:
                evidence.status = "PURGE_FAILED"
                evidence.processing_error = (
                    f"PURGE_MAX_RETRIES_EXCEEDED ({evidence.purge_retry_count}/{self.max_retries}): {err_str}"
                )
            else:
                evidence.processing_error = (
                    f"PURGE_IO_RETRY ({evidence.purge_retry_count}/{self.max_retries}): {err_str}"
                )
            return False, err_str

        # 3. Transition database status to PURGED
        evidence.status = "PURGED"
        evidence.processing_error = None
        evidence.purged_at = utc_now()

        return True, None

    async def execute_purge_batch(
        self,
        db: AsyncSession,
        batch_size: Optional[int] = None,
        include_failed: bool = False,
    ) -> Dict[str, Any]:
        """Discovers, deletes, and updates database records in an atomic batch."""
        expired_files = await self.discover_expired_evidence(
            db, batch_size=batch_size, include_failed=include_failed
        )
        scanned_count = len(expired_files)
        purged_count = 0
        failed_count = 0
        errors: List[Dict[str, str]] = []

        for ev in expired_files:
            success, err_msg = await self.purge_single_record(db, ev)
            if success:
                purged_count += 1
            else:
                failed_count += 1
                errors.append({"evidence_id": str(ev.id), "error": err_msg or "Unknown error"})

        if scanned_count > 0:
            try:
                await db.commit()
                logger.info(
                    "Storage retention purge completed: scanned=%d, purged=%d, failed=%d",
                    scanned_count,
                    purged_count,
                    failed_count,
                )
            except Exception as commit_exc:
                await db.rollback()
                logger.error("Storage retention purge database commit failed: %s", commit_exc)
                raise

        return {
            "scanned": scanned_count,
            "purged": purged_count,
            "failed": failed_count,
            "errors": errors,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def reset_failed_purges(
        self,
        db: AsyncSession,
        evidence_id: Optional[uuid.UUID] = None,
    ) -> int:
        """Safely resets PURGE_FAILED records to allow administrative reprocessing."""
        stmt = select(EvidenceFile).where(EvidenceFile.status == "PURGE_FAILED")
        if evidence_id:
            stmt = stmt.where(EvidenceFile.id == evidence_id)

        res = await db.execute(stmt)
        failed_files = list(res.scalars().all())
        for f in failed_files:
            f.status = "STAGED"
            f.purge_retry_count = 0
            f.processing_error = None
        if failed_files:
            await db.commit()
            logger.info("Reset %d PURGE_FAILED evidence record(s) for reprocessing.", len(failed_files))
        return len(failed_files)


class EvidenceRetentionWorker:
    """Asynchronous background worker executing periodic evidence retention purges."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        lifecycle_manager: Optional[StorageLifecycleManager] = None,
        scan_interval_seconds: float = 3600.0,
        batch_size: int = 50,
    ) -> None:
        self.session_factory = session_factory
        self.lifecycle_manager = lifecycle_manager or StorageLifecycleManager()
        self.scan_interval_seconds = scan_interval_seconds
        self.batch_size = batch_size
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        self.scan_count: int = 0
        self.last_scan_at: Optional[datetime] = None
        self.last_error: Optional[Exception] = None

    def start_periodic_scanner(self, max_iterations: Optional[int] = None) -> asyncio.Task:
        """Starts periodic retention scanning as a managed asyncio.Task."""
        if self._task and not self._task.done():
            logger.warning("EvidenceRetentionWorker periodic scanner is already running.")
            return self._task

        self._stop_event.clear()
        self.is_running = True
        self._task = asyncio.create_task(self.start(max_iterations=max_iterations))

        def _on_done(task: asyncio.Task) -> None:
            self.is_running = False
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    self.last_error = exc
                    logger.critical(
                        "EvidenceRetentionWorker background task terminated with exception: %s",
                        exc,
                        exc_info=exc,
                    )

        self._task.add_done_callback(_on_done)
        return self._task

    async def start(self, max_iterations: Optional[int] = None) -> None:
        """Polling loop executing purge batches periodically."""
        iterations = 0
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self.last_error = exc
                logger.error("EvidenceRetentionWorker iteration failed non-fatally: %s", exc)

            self.scan_count += 1
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.scan_interval_seconds)
            except asyncio.TimeoutError:
                pass

        self.is_running = False

    async def run_once(self) -> Dict[str, Any]:
        """Executes a single pass of expired evidence purging."""
        if not self.session_factory:
            raise RuntimeError("Cannot call run_once() without a configured session_factory.")

        async with self.session_factory() as session:
            try:
                res = await self.lifecycle_manager.execute_purge_batch(
                    session, batch_size=self.batch_size
                )
                self.last_scan_at = datetime.now(timezone.utc)
                return res
            except Exception:
                await session.rollback()
                raise

    def stop(self) -> None:
        """Signals background task to stop."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
        self.is_running = False

    async def stop_async(self) -> None:
        """Asynchronously stops and awaits completion of the background task."""
        self.stop()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
