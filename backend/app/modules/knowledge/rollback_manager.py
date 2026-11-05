"""Deterministic Rollback Execution & Manifest/Database Synchronization Engine.

Coordinates the emergency restoration of a previous known-good FAISS vector index snapshot:
1. Validates target snapshot structural, cryptographic, and FAISS vector integrity before action.
2. Atomically reverts active_index.json via same-filesystem temporary file and os.replace().
3. Transactionally updates PostgreSQL under a dedicated advisory lock (5432002).
4. Synchronizes DocumentVersion active/is_current records to match the restored snapshot.
5. Emits Redis Pub/Sub reload broadcast and invalidates fast version cache with graceful degradation.
6. Hot-swaps ActiveIndexReader in-memory index pointer.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import uuid

import faiss
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import IndexIntegrityError, RollbackExecutionError
from app.core.logging import configure_safe_logger
from app.models.document import Document, DocumentVersion
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock
from app.modules.knowledge.reader import ActiveIndexReader
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))


class RollbackResult(BaseModel):
    """Summary report returned upon successful vector index rollback."""

    success: bool
    rolled_back_version_tag: str
    restored_version_tag: str
    manifest_path: str
    total_vectors: int
    sha256_hash: str
    rolled_back_at: datetime
    reason: str
    triggered_by: str = "watchdog"
    redis_notified: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class RollbackManager:
    """Manages atomic reversion of active vector index to a prior known-good candidate."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        redis_service: Optional[RedisService] = None,
        reader: Optional[ActiveIndexReader] = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.settings = settings or default_settings
        self.redis_service = redis_service or get_redis_service()
        self.reader = reader
        self.lock_timeout_seconds = lock_timeout_seconds
        self.storage_dir = os.path.abspath(self.settings.FAISS_STORAGE_DIR)
        os.makedirs(self.storage_dir, exist_ok=True)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Computes SHA-256 digest of binary file in 64KB chunks."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_active_manifest_path(self) -> str:
        """Returns the canonical path to active_index.json."""
        return os.path.join(self.storage_dir, "active_index.json")

    def check_target_snapshot_integrity(self, target_tag: str) -> Dict[str, Any]:
        """Strictly validates target snapshot existence, manifest, SHA-256, and FAISS readability.

        If target snapshot is missing or corrupted, raises RollbackExecutionError to halt rollback
        and prevent replacing a degraded index with an unreadable one.
        """
        target_dir = os.path.join(self.storage_dir, target_tag)
        if not os.path.isdir(target_dir):
            raise RollbackExecutionError(
                f"Rollback aborted: Target index directory does not exist at '{target_dir}'."
            )

        manifest_file = os.path.join(target_dir, "manifest.json")
        if not os.path.isfile(manifest_file):
            raise RollbackExecutionError(
                f"Rollback aborted: Target manifest.json missing in '{target_dir}'."
            )

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
        except Exception as exc:
            raise RollbackExecutionError(
                f"Rollback aborted: Target manifest.json is unparseable: {exc}"
            ) from exc

        index_file = os.path.join(target_dir, "index.faiss")
        if not os.path.isfile(index_file):
            raise RollbackExecutionError(
                f"Rollback aborted: Target index.faiss missing in '{target_dir}'."
            )

        # Verify cryptographic checksum
        expected_hash = manifest_data.get("sha256")
        if not expected_hash:
            raise RollbackExecutionError(
                "Rollback aborted: Target manifest missing required 'sha256' field."
            )

        computed_hash = self.compute_sha256(index_file)
        if computed_hash.lower() != expected_hash.lower():
            raise RollbackExecutionError(
                f"Rollback aborted: Target index SHA-256 mismatch for '{target_tag}': "
                f"computed={computed_hash}, expected={expected_hash}."
            )

        # Test index readability via FAISS C++
        try:
            loaded_index = faiss.read_index(index_file)
        except Exception as exc:
            raise RollbackExecutionError(
                f"Rollback aborted: Target FAISS index in '{index_file}' cannot be loaded: {exc}"
            ) from exc

        expected_vectors = manifest_data.get("total_vectors", 0)
        if loaded_index.ntotal != expected_vectors:
            raise RollbackExecutionError(
                f"Rollback aborted: Vector count mismatch for target '{target_tag}': "
                f"manifest reports {expected_vectors}, FAISS loaded {loaded_index.ntotal}."
            )

        if loaded_index.d != 384:
            raise RollbackExecutionError(
                f"Rollback aborted: Vector dimension mismatch for target '{target_tag}': "
                f"expected 384, got {loaded_index.d}."
            )

        return {
            "target_dir": target_dir,
            "manifest_file": manifest_file,
            "index_file": index_file,
            "total_vectors": loaded_index.ntotal,
            "sha256": computed_hash,
            "manifest_data": manifest_data,
        }

    def write_atomic_manifest(
        self,
        manifest_payload: Dict[str, Any],
        max_retries: int = 5,
    ) -> str:
        """Atomically writes and replaces active_index.json using same-filesystem os.replace()."""
        manifest_path = self.get_active_manifest_path()
        tmp_manifest = f"{manifest_path}.tmp_{uuid.uuid4().hex[:8]}"

        # Write, flush, and sync temporary manifest
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest_payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # Verify temporary file readability and content before replacing
        with open(tmp_manifest, "r", encoding="utf-8") as f:
            verified_payload = json.load(f)
            if verified_payload.get("active_version_tag") != manifest_payload.get("active_version_tag"):
                if os.path.exists(tmp_manifest):
                    os.remove(tmp_manifest)
                raise IndexIntegrityError("Temporary manifest failed read-back verification.")

        # Atomic replacement with retry loop handling Windows file locking (WinError 32)
        for attempt in range(max_retries):
            try:
                os.replace(tmp_manifest, manifest_path)
                logger.info(
                    "Atomically restored active_index.json -> '%s'.",
                    manifest_payload["active_version_tag"],
                )
                return manifest_path
            except (PermissionError, OSError) as exc:
                if attempt == max_retries - 1:
                    if os.path.exists(tmp_manifest):
                        try:
                            os.remove(tmp_manifest)
                        except Exception:
                            pass
                    raise RollbackExecutionError(
                        f"Failed to atomically restore active_index.json after {max_retries} attempts: {exc}"
                    ) from exc
                time.sleep(0.05 * (2 ** attempt))

        return manifest_path

    async def execute_rollback(
        self,
        session: AsyncSession,
        failed_tag: str,
        target_tag: str,
        reason: str,
        triggered_by: str = "watchdog",
    ) -> RollbackResult:
        """Executes the complete coordinated rollback protocol under PostgreSQL advisory lock."""
        now_utc = datetime.now(timezone.utc)
        logger.warning(
            "Initiating automated rollback from '%s' to target '%s'. Reason: %s (Triggered by: %s)",
            failed_tag,
            target_tag,
            reason,
            triggered_by,
        )

        # 1. Verify target snapshot integrity before taking any destructive action
        target_meta = self.check_target_snapshot_integrity(target_tag)

        # 2. Acquire database advisory lock to prevent concurrent promotion/rollback
        lock_id = getattr(self.settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)
        async with DatabaseAdvisoryLock(session=session, lock_id=lock_id, timeout_seconds=self.lock_timeout_seconds):

            # 3. Locate failed and target records in database
            stmt_failed = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == failed_tag)
            k_failed = (await session.execute(stmt_failed)).scalar_one_or_none()

            stmt_target = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == target_tag)
            k_target = (await session.execute(stmt_target)).scalar_one_or_none()

            # Record transitional state ROLLBACK_PENDING
            if k_failed:
                k_failed.status = "ROLLBACK_PENDING"
                await session.commit()

            # 4. Prepare and atomically swap active_index.json back to target
            manifest_payload = {
                "active_version_tag": target_tag,
                "index_file_path": os.path.abspath(target_meta["index_file"]),
                "total_vectors": target_meta["total_vectors"],
                "embedding_dimension": 384,
                "sha256": target_meta["sha256"],
                "swapped_at": now_utc.isoformat(),
                "previous_version_tag": failed_tag,
                "promoted_by": f"rollback_{triggered_by}",
                "override_reason": f"AUTOMATED_ROLLBACK: {reason}",
            }

            try:
                published_manifest_path = self.write_atomic_manifest(manifest_payload)
            except Exception as swap_exc:
                logger.error("Atomic manifest reversion failed for '%s': %s", target_tag, swap_exc)
                if k_failed:
                    k_failed.status = "FAILED"
                    k_failed.rollback_reason = f"Manifest rollback swap failed: {swap_exc}"
                    await session.commit()
                raise RollbackExecutionError(
                    f"Filesystem rollback manifest swap failed: {swap_exc}"
                ) from swap_exc

            # 5. Transactionally update database states
            try:
                if k_failed:
                    k_failed.status = "ROLLED_BACK"
                    k_failed.canary_status = "FAILED_ROLLED_BACK"
                    k_failed.rolled_back_at = now_utc
                    k_failed.rollback_reason = reason

                if k_target:
                    k_target.status = "ACTIVE"
                    k_target.canary_status = "CONFIRMED"
                    k_target.activated_at = now_utc

                # Synchronize DocumentVersion records to match target manifest
                target_codes = target_meta["manifest_data"].get("included_document_codes", [])
                doc_version_map = (
                    target_meta["manifest_data"].get("document_versions")
                    or target_meta["manifest_data"].get("document_version_map")
                    or {}
                )
                if target_codes:
                    stmt_docs = (
                        select(Document)
                        .options(selectinload(Document.versions))
                        .where(Document.document_code.in_(target_codes))
                    )
                    docs = (await session.execute(stmt_docs)).scalars().all()
                    for doc in docs:
                        if not doc.versions:
                            continue

                        target_ver = None
                        if doc.document_code in doc_version_map:
                            target_num = doc_version_map[doc.document_code]
                            target_ver = next((v for v in doc.versions if v.version_number == target_num), None)

                        if target_ver is None:
                            curr_ver = next((v for v in doc.versions if v.is_current), None)
                            if curr_ver:
                                prior_vers = [v for v in doc.versions if v.version_number < curr_ver.version_number]
                                if prior_vers:
                                    target_ver = max(prior_vers, key=lambda v: v.version_number)
                                else:
                                    target_ver = curr_ver
                            else:
                                target_ver = max(doc.versions, key=lambda v: v.version_number)

                        for v in doc.versions:
                            if v.id == target_ver.id:
                                v.status = "ACTIVE"
                                v.is_current = True
                            else:
                                v.is_current = False
                                v.status = "SUPERSEDED"

                await session.commit()
                logger.info(
                    "Database rollback complete: '%s' is ROLLED_BACK, '%s' restored to ACTIVE.",
                    failed_tag,
                    target_tag,
                )

            except Exception as db_exc:
                logger.error("Database rollback update failed: %s", db_exc)
                await session.rollback()
                raise RollbackExecutionError(f"Database state update failed during rollback: {db_exc}") from db_exc

            # 6. Redis Invalidation and Pub/Sub Event Broadcast
            redis_notified = False
            try:
                channel = getattr(
                    self.settings,
                    "REDIS_PUBSUB_INDEX_RELOAD_CHANNEL",
                    "channel:knowledge:index_reload",
                )
                reload_event = {
                    "event": "INDEX_ROLLED_BACK",
                    "active_version_tag": target_tag,
                    "previous_version_tag": failed_tag,
                    "total_vectors": target_meta["total_vectors"],
                    "sha256": target_meta["sha256"],
                    "reason": reason,
                    "timestamp": now_utc.isoformat(),
                }
                # Publish event
                await self.redis_service.publish(channel, json.dumps(reload_event), raise_on_error=False)

                # Set fast active version cache key
                await self.redis_service.set("cache:active_index_version", target_tag, ttl_seconds=300)
                redis_notified = True
                logger.info("Published rollback reload event to Redis channel '%s'.", channel)
            except Exception as redis_exc:
                logger.warning(
                    "Redis notification failed during rollback to '%s': %s. Background sync will reconcile.",
                    target_tag,
                    redis_exc,
                )

            # 7. Hot-swap in-memory reader if attached
            if self.reader:
                try:
                    self.reader.load_active_index(force=True)
                    logger.info("ActiveIndexReader hot-swapped in-memory pointer to '%s'.", target_tag)
                except Exception as reader_exc:
                    logger.warning("ActiveIndexReader in-memory swap failed: %s", reader_exc)

            return RollbackResult(
                success=True,
                rolled_back_version_tag=failed_tag,
                restored_version_tag=target_tag,
                manifest_path=published_manifest_path,
                total_vectors=target_meta["total_vectors"],
                sha256_hash=target_meta["sha256"],
                rolled_back_at=now_utc,
                reason=reason,
                triggered_by=triggered_by,
                redis_notified=redis_notified,
                details={"target_manifest": target_meta["manifest_data"]},
            )

