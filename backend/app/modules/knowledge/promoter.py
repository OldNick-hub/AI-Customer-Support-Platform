"""Atomic Candidate Vector Index Promotion & Reconciliation Engine.

Manages the atomic promotion of benchmark-approved candidate FAISS vector indexes into active status,
enforcing RBAC authorization, maintenance window constraints, checksum verification, same-filesystem
atomic manifest swaps via os.replace(), transactional database state transitions, Redis Pub/Sub broadcasts,
and deterministic crash reconciliation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import shutil
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

import faiss
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    BenchmarkGateFailedError,
    IndexIntegrityError,
    IndexPromotionError,
    MaintenanceWindowError,
    UnauthorizedPromotionError,
)
from app.core.logging import configure_safe_logger
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import BenchmarkRun, KnowledgeIndex
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock
from app.modules.knowledge.maintenance import (
    MaintenanceWindowEligibility,
    MaintenanceWindowService,
)
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))


# =====================================================================
# Pydantic Schemas for Promotion & Reconciliation
# =====================================================================

class PromotionResult(BaseModel):
    """Detailed summary report returned upon successful candidate index promotion."""

    success: bool
    promoted_version_tag: str
    previous_version_tag: Optional[str] = None
    manifest_path: str
    total_vectors: int
    sha256_hash: str
    promoted_at: datetime
    promoted_by: str
    admin_override_used: bool = False
    override_reason: Optional[str] = None
    redis_notified: bool = False
    canary_monitoring_started: bool = False
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class ReconciliationResult(BaseModel):
    """Audit report returned upon startup state reconciliation."""

    reconciled: bool
    action_taken: str
    active_version_tag: Optional[str] = None
    filesystem_tag: Optional[str] = None
    database_tag: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


# =====================================================================
# Atomic Index Promoter
# =====================================================================

class IndexPromoter:
    """Coordinates atomic candidate promotion, manifest publication, and crash reconciliation."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        maintenance_service: Optional[MaintenanceWindowService] = None,
        redis_service: Optional[RedisService] = None,
        watchdog: Optional[Any] = None,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.settings = settings or default_settings
        self.maintenance_service = maintenance_service or MaintenanceWindowService(settings=self.settings)
        self.redis_service = redis_service or get_redis_service()
        self.watchdog = watchdog
        self.session_factory = session_factory
        self.storage_dir = os.path.abspath(self.settings.FAISS_STORAGE_DIR)
        self.lock_timeout_seconds = lock_timeout_seconds
        os.makedirs(self.storage_dir, exist_ok=True)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Computes SHA-256 digest of binary file in 64KB chunks."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()

    def get_active_manifest_path(self) -> str:
        """Returns the authoritative path to active_index.json."""
        return os.path.join(self.storage_dir, "active_index.json")

    def read_active_manifest(self) -> Optional[Dict[str, Any]]:
        """Reads and parses active_index.json if it exists and is non-empty."""
        manifest_path = self.get_active_manifest_path()
        if not os.path.isfile(manifest_path):
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not parse existing active_index.json: %s", exc)
            return None

    def verify_candidate_integrity(self, candidate_tag: str) -> Dict[str, Any]:
        """Strictly verifies candidate directory, manifest, SHA-256 digest, and FAISS readability."""
        candidate_dir = os.path.join(self.storage_dir, candidate_tag)
        if not os.path.isdir(candidate_dir):
            raise IndexPromotionError(
                message=f"Candidate directory does not exist at '{candidate_dir}'."
            )

        manifest_file = os.path.join(candidate_dir, "manifest.json")
        if not os.path.isfile(manifest_file):
            raise IndexPromotionError(
                message=f"Candidate manifest.json missing in '{candidate_dir}'."
            )

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
        except Exception as exc:
            raise IndexIntegrityError(f"Candidate manifest.json is unparseable: {exc}") from exc

        index_file = os.path.join(candidate_dir, "index.faiss")
        if not os.path.isfile(index_file):
            raise IndexPromotionError(f"Candidate index.faiss missing in '{candidate_dir}'.")

        # Verify cryptographic checksum
        expected_hash = manifest_data.get("sha256")
        if not expected_hash:
            raise IndexIntegrityError("Candidate manifest missing required 'sha256' field.")

        computed_hash = self.compute_sha256(index_file)
        if computed_hash.lower() != expected_hash.lower():
            raise IndexIntegrityError(
                f"Candidate index SHA-256 mismatch for '{candidate_tag}': "
                f"computed={computed_hash}, expected={expected_hash}."
            )

        # Test index readability via FAISS C++
        try:
            loaded_index = faiss.read_index(index_file)
        except Exception as exc:
            raise IndexIntegrityError(
                f"Candidate FAISS index in '{index_file}' cannot be loaded: {exc}"
            ) from exc

        expected_vectors = manifest_data.get("total_vectors", 0)
        if loaded_index.ntotal != expected_vectors:
            raise IndexIntegrityError(
                f"Vector count mismatch for '{candidate_tag}': "
                f"manifest reports {expected_vectors}, FAISS loaded {loaded_index.ntotal}."
            )

        if loaded_index.d != 384:
            raise IndexIntegrityError(
                f"Vector dimension mismatch for '{candidate_tag}': "
                f"expected 384, got {loaded_index.d}."
            )

        return {
            "candidate_dir": candidate_dir,
            "manifest_file": manifest_file,
            "index_file": index_file,
            "manifest_data": manifest_data,
            "total_vectors": loaded_index.ntotal,
            "sha256": computed_hash,
        }

    async def validate_benchmark_and_status(
        self,
        session: AsyncSession,
        candidate_tag: str,
    ) -> KnowledgeIndex:
        """Verifies database KnowledgeIndex status and passing benchmark gates."""
        stmt = (
            select(KnowledgeIndex)
            .options(selectinload(KnowledgeIndex.benchmark_runs))
            .where(KnowledgeIndex.index_version_tag == candidate_tag)
        )
        k_index = (await session.execute(stmt)).scalar_one_or_none()

        if k_index is None:
            raise IndexPromotionError(
                message=f"No KnowledgeIndex database record found for candidate tag '{candidate_tag}'."
            )

        # Candidate must have undergone benchmark evaluation
        runs = sorted(k_index.benchmark_runs, key=lambda r: r.executed_at, reverse=True)
        if not runs:
            raise BenchmarkGateFailedError(
                message=f"Candidate index '{candidate_tag}' has no recorded benchmark evaluations."
            )

        latest_run = runs[0]
        if not latest_run.is_passed:
            raise BenchmarkGateFailedError(
                message=(
                    f"Candidate index '{candidate_tag}' failed Golden Benchmark gates "
                    f"(grounding: {latest_run.grounding_score:.4f}, passed: {latest_run.is_passed})."
                )
            )

        diag = latest_run.diagnostics or {}
        if diag.get("is_inconclusive"):
            raise BenchmarkGateFailedError(
                message=f"Candidate index '{candidate_tag}' evaluation was inconclusive."
            )

        return k_index

    def check_rbac_authorization(
        self,
        caller_identity: str,
        caller_roles: Optional[List[str]],
        admin_override: bool,
        override_reason: Optional[str],
    ) -> None:
        """Enforces administrative access boundaries and validates emergency override authorization."""
        roles_clean = [r.strip().upper() for r in (caller_roles or ["ADMIN"])]

        # Determine if caller has administrative privileges
        has_admin_role = any(r in ("ADMIN", "SYSTEM_ADMIN", "KNOWLEDGE_ADMIN") for r in roles_clean)

        if not has_admin_role:
            logger.warning(
                "Unauthorized promotion attempt by '%s' with roles: %s.",
                caller_identity,
                roles_clean,
            )
            raise UnauthorizedPromotionError(
                message=f"User '{caller_identity}' lacks required administrative roles for index promotion."
            )

        # Emergency override demands verified admin role AND a descriptive non-empty justification
        if admin_override:
            if not override_reason or len(override_reason.strip()) < 10:
                raise UnauthorizedPromotionError(
                    message="Emergency override requires an explicit, descriptive justification (minimum 10 characters)."
                )

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
                    "Atomically published active_index.json -> '%s'.",
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
                    raise IndexPromotionError(
                        f"Failed to atomically publish active_index.json after {max_retries} attempts: {exc}"
                    ) from exc
                time.sleep(0.05 * (2 ** attempt))

        return manifest_path

    async def promote_candidate(
        self,
        session: AsyncSession,
        candidate_tag: str,
        caller_identity: str = "system_admin",
        caller_roles: Optional[List[str]] = None,
        admin_override: bool = False,
        override_reason: Optional[str] = None,
        evaluation_time: Optional[datetime] = None,
    ) -> PromotionResult:
        """Executes full atomic promotion workflow under a PostgreSQL advisory lock."""
        now_utc = datetime.now(timezone.utc)
        logger.info(
            "Initiating promotion of candidate '%s' by '%s' (admin_override=%s).",
            candidate_tag,
            caller_identity,
            admin_override,
        )

        # 1. Enforce RBAC authorization and emergency override validation
        self.check_rbac_authorization(
            caller_identity=caller_identity,
            caller_roles=caller_roles,
            admin_override=admin_override,
            override_reason=override_reason,
        )

        # 2. Enforce Maintenance Window constraints
        self.maintenance_service.enforce_promotion_eligibility(
            evaluation_time=evaluation_time,
            admin_override=admin_override,
        )

        # 3. Verify candidate filesystem snapshot and cryptographic integrity
        cand_meta = self.verify_candidate_integrity(candidate_tag)

        # 4. Acquire database advisory lock to prevent concurrent promotions
        lock_id = getattr(self.settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)
        async with DatabaseAdvisoryLock(session=session, lock_id=lock_id, timeout_seconds=self.lock_timeout_seconds):

            # 5. Verify candidate benchmark results and database record
            k_candidate = await self.validate_benchmark_and_status(session, candidate_tag)

            # 6. Read previous active manifest and verify previous index preservation
            previous_manifest = self.read_active_manifest()
            previous_tag: Optional[str] = None
            if previous_manifest and isinstance(previous_manifest, dict):
                previous_tag = previous_manifest.get("active_version_tag")
                if previous_tag and previous_tag != candidate_tag:
                    prev_dir = os.path.join(self.storage_dir, previous_tag)
                    if not os.path.isdir(prev_dir):
                        logger.warning(
                            "Previous active index directory '%s' not found on disk. Preserving tag pointer.",
                            prev_dir,
                        )

            # 7. Record transient transitional state PROMOTING in DB
            k_candidate.status = "PROMOTING"
            await session.commit()

            # 8. Prepare and atomically publish new active_index.json
            manifest_payload = {
                "active_version_tag": candidate_tag,
                "index_file_path": os.path.abspath(cand_meta["index_file"]),
                "total_vectors": cand_meta["total_vectors"],
                "embedding_dimension": 384,
                "sha256": cand_meta["sha256"],
                "swapped_at": now_utc.isoformat(),
                "previous_version_tag": previous_tag,
                "promoted_by": caller_identity,
                "override_reason": override_reason if admin_override else None,
            }

            try:
                published_manifest_path = self.write_atomic_manifest(manifest_payload)
            except Exception as swap_exc:
                logger.error("Atomic manifest replacement failed for '%s': %s", candidate_tag, swap_exc)
                k_candidate.status = "FAILED"
                k_candidate.rollback_reason = f"Filesystem manifest publication failed: {swap_exc}"
                await session.commit()
                raise IndexPromotionError(
                    f"Filesystem manifest swap failed: {swap_exc}"
                ) from swap_exc

            # 9. Transactionally update database states
            try:
                # Mark previous active index SUPERSEDED
                stmt_prev = select(KnowledgeIndex).where(
                    KnowledgeIndex.status == "ACTIVE",
                    KnowledgeIndex.index_version_tag != candidate_tag,
                )
                prev_indices = (await session.execute(stmt_prev)).scalars().all()
                for prev_idx in prev_indices:
                    prev_idx.status = "SUPERSEDED"

                # Mark candidate ACTIVE
                k_candidate.status = "ACTIVE"
                k_candidate.activated_at = now_utc
                k_candidate.rollback_target_tag = previous_tag
                if previous_tag:
                    k_candidate.canary_status = "MONITORING"
                    k_candidate.watchdog_expires_at = now_utc + timedelta(
                        seconds=getattr(self.settings, "WATCHDOG_MONITORING_WINDOW_SECONDS", 300)
                    )
                    k_candidate.canary_failure_count = 0
                else:
                    k_candidate.canary_status = "CONFIRMED"

                # Update DocumentVersion records: staged versions become ACTIVE and is_current
                # while their predecessors become SUPERSEDED and is_current=False
                included_codes = cand_meta["manifest_data"].get("included_document_codes", [])
                if included_codes:
                    stmt_docs = (
                        select(Document)
                        .options(selectinload(Document.versions))
                        .where(Document.document_code.in_(included_codes))
                    )
                    docs = (await session.execute(stmt_docs)).scalars().all()
                    for doc in docs:
                        # Find highest version number staged for this doc
                        staged_vers = [v for v in doc.versions if v.status == "STAGED"]
                        if staged_vers:
                            new_ver = max(staged_vers, key=lambda v: v.version_number)
                            for v in doc.versions:
                                if v.id == new_ver.id:
                                    v.status = "ACTIVE"
                                    v.is_current = True
                                else:
                                    if v.is_current:
                                        v.is_current = False
                                        v.status = "SUPERSEDED"

                await session.commit()
                logger.info(
                    "Database state updated: '%s' is now ACTIVE. Previous active marked SUPERSEDED.",
                    candidate_tag,
                )

                # 9.1 Automatically start Watchdog Canary Monitoring Scheduler if armed
                canary_monitoring_started = False
                if previous_tag and self.watchdog and self.session_factory:
                    interval_sec = getattr(self.settings, "WATCHDOG_CHECK_INTERVAL_SECONDS", 30.0)
                    self.watchdog.start_monitoring_scheduler(
                        session_factory=self.session_factory,
                        active_tag=candidate_tag,
                        interval_seconds=interval_sec,
                    )
                    canary_monitoring_started = True
                    logger.info("Watchdog monitoring scheduler automatically started for '%s'.", candidate_tag)

            except Exception as db_exc:
                logger.error("Database update failed after manifest publication: %s", db_exc)
                await session.rollback()
                # Run deterministic reconciliation
                await self.reconcile_active_index_state(session)
                raise IndexPromotionError(
                    f"Database update failed after manifest swap. Reconciled state. Error: {db_exc}"
                ) from db_exc

            # 10. Redis Invalidation and Pub/Sub Event Broadcast
            redis_notified = False
            try:
                channel = getattr(
                    self.settings,
                    "REDIS_PUBSUB_INDEX_RELOAD_CHANNEL",
                    "channel:knowledge:index_reload",
                )
                reload_event = {
                    "event": "INDEX_PROMOTED",
                    "active_version_tag": candidate_tag,
                    "previous_version_tag": previous_tag,
                    "total_vectors": cand_meta["total_vectors"],
                    "sha256": cand_meta["sha256"],
                    "timestamp": now_utc.isoformat(),
                    "canary_monitoring_started": canary_monitoring_started,
                }
                # Publish event
                await self.redis_service.publish(channel, json.dumps(reload_event), raise_on_error=False)

                # Set fast active version cache key
                await self.redis_service.set("cache:active_index_version", candidate_tag, ttl_seconds=300)
                redis_notified = True
                logger.info("Published reload event to Redis channel '%s'.", channel)
            except Exception as redis_exc:
                logger.warning(
                    "Redis notification failed during promotion of '%s': %s. Background sync will reconcile.",
                    candidate_tag,
                    redis_exc,
                )

            return PromotionResult(
                success=True,
                promoted_version_tag=candidate_tag,
                previous_version_tag=previous_tag,
                manifest_path=published_manifest_path,
                total_vectors=cand_meta["total_vectors"],
                sha256_hash=cand_meta["sha256"],
                promoted_at=now_utc,
                promoted_by=caller_identity,
                admin_override_used=admin_override,
                override_reason=override_reason if admin_override else None,
                redis_notified=redis_notified,
                canary_monitoring_started=canary_monitoring_started,
                diagnostics={
                    "candidate_dir": cand_meta["candidate_dir"],
                    "included_documents": len(cand_meta["manifest_data"].get("included_document_codes", [])),
                },
            )

    async def reconcile_active_index_state(
        self,
        session: AsyncSession,
    ) -> ReconciliationResult:
        """Reconciles filesystem manifest and database states deterministically after process crash."""
        logger.info("Executing startup state reconciliation between active_index.json and PostgreSQL.")
        manifest = self.read_active_manifest()
        fs_tag = manifest.get("active_version_tag") if manifest else None

        stmt_active = select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE")
        db_active = (await session.execute(stmt_active)).scalar_one_or_none()
        db_tag = db_active.index_version_tag if db_active else None

        stmt_promoting = select(KnowledgeIndex).where(KnowledgeIndex.status == "PROMOTING")
        promoting_indices = (await session.execute(stmt_promoting)).scalars().all()

        # Case 1: Interrupted during DB update after manifest was already swapped (fs_tag != db_tag)
        if fs_tag and promoting_indices:
            promoting_match = next((idx for idx in promoting_indices if idx.index_version_tag == fs_tag), None)
            if promoting_match:
                # Verify FAISS integrity on disk
                cand_meta = self.verify_candidate_integrity(fs_tag)
                # Advance DB state: mark active, supersede others
                if db_active and db_active.id != promoting_match.id:
                    db_active.status = "SUPERSEDED"
                promoting_match.status = "ACTIVE"
                promoting_match.activated_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info("Reconciliation: Completed interrupted database promotion for '%s'.", fs_tag)
                return ReconciliationResult(
                    reconciled=True,
                    action_taken="ADVANCED_DATABASE_TO_MATCH_MANIFEST",
                    active_version_tag=fs_tag,
                    filesystem_tag=fs_tag,
                    database_tag=fs_tag,
                    details={"promoted_tag": fs_tag},
                )

        # Case 2: Interrupted before manifest swap while DB was in PROMOTING
        if promoting_indices:
            for p_idx in promoting_indices:
                if p_idx.index_version_tag != fs_tag:
                    p_idx.status = "FAILED"
                    p_idx.rollback_reason = "Process crashed before filesystem manifest swap."
            await session.commit()
            logger.info("Reconciliation: Reset orphaned PROMOTING indices to FAILED.")
            return ReconciliationResult(
                reconciled=True,
                action_taken="REVERTED_ORPHANED_PROMOTING_INDEX",
                active_version_tag=fs_tag,
                filesystem_tag=fs_tag,
                database_tag=db_tag,
                details={"reverted_indices": [p.index_version_tag for p in promoting_indices]},
            )

        # Case 3: Filesystem missing active_index.json but DB has an authoritative ACTIVE index
        if not fs_tag and db_active:
            db_dir = os.path.join(self.storage_dir, db_active.index_version_tag)
            index_path = os.path.join(db_dir, "index.faiss")
            if os.path.isfile(index_path):
                sha256_hash = self.compute_sha256(index_path)
                manifest_payload = {
                    "active_version_tag": db_active.index_version_tag,
                    "index_file_path": os.path.abspath(index_path),
                    "total_vectors": db_active.total_vectors,
                    "embedding_dimension": 384,
                    "sha256": sha256_hash,
                    "swapped_at": datetime.now(timezone.utc).isoformat(),
                    "previous_version_tag": db_active.rollback_target_tag,
                    "promoted_by": "startup_reconciliation",
                }
                self.write_atomic_manifest(manifest_payload)
                logger.info("Reconciliation: Recreated active_index.json from authoritative DB row '%s'.", db_tag)
                return ReconciliationResult(
                    reconciled=True,
                    action_taken="RECREATED_MANIFEST_FROM_DATABASE",
                    active_version_tag=db_tag,
                    filesystem_tag=db_tag,
                    database_tag=db_tag,
                )

        # Case 4: Perfectly consistent state
        if fs_tag and db_tag and fs_tag == db_tag:
            return ReconciliationResult(
                reconciled=True,
                action_taken="NO_ACTION_REQUIRED_CONSISTENT",
                active_version_tag=fs_tag,
                filesystem_tag=fs_tag,
                database_tag=db_tag,
            )

        # Case 5: Both empty / clean initial state
        if not fs_tag and not db_tag:
            return ReconciliationResult(
                reconciled=True,
                action_taken="INITIAL_EMPTY_STATE",
                active_version_tag=None,
                filesystem_tag=None,
                database_tag=None,
            )

        # Case 6: Manifest exists, but DB active row is missing (re-link DB)
        if fs_tag and not db_tag:
            stmt_cand = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == fs_tag)
            k_cand = (await session.execute(stmt_cand)).scalar_one_or_none()
            if k_cand:
                k_cand.status = "ACTIVE"
                await session.commit()
                logger.info("Reconciliation: Restored ACTIVE DB state for '%s'.", fs_tag)
                return ReconciliationResult(
                    reconciled=True,
                    action_taken="RESTORED_ACTIVE_DB_ROW",
                    active_version_tag=fs_tag,
                    filesystem_tag=fs_tag,
                    database_tag=fs_tag,
                )

        return ReconciliationResult(
            reconciled=False,
            action_taken="UNRESOLVED_DIVERGENCE_REQUIRING_ADMIN_ACTION",
            active_version_tag=None,
            filesystem_tag=fs_tag,
            database_tag=db_tag,
        )
