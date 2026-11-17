"""Application & Worker Lifecycle Management Engine.

Provides centralized runtime startup and shutdown hooks for FastAPI, background
workers, and index monitoring services:
1. Tier 3 Reader Startup Synchronization: loads authoritative active index into memory on boot.
2. Promotion Crash Reconciliation: advances or reverts interrupted candidate promotions.
3. Watchdog Restart Recovery: recovers interrupted rollbacks (ROLLBACK_PENDING) and
   resumes active 300-second canary monitoring schedulers for uncompleted windows.
4. Tier 2 Reader Background Reconciliation: starts periodic active-version syncer.
"""

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.logging import configure_safe_logger
from app.modules.knowledge.promoter import IndexPromoter, ReconciliationResult
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.knowledge.watchdog import IndexHealthWatchdog, WatchdogEvaluationResult
from app.modules.ticketing.worker import EscalationWorker
from app.modules.evidence.worker import ReconciliationWorker
from app.modules.evidence.lifecycle_manager import StorageLifecycleManager, EvidenceRetentionWorker
from app.modules.knowledge.retry_service import KBIngestionRetryWorker
from app.modules.multilingual.outbox_worker import OutboxWorker
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))


class WorkerLifecycleManager:
    """Coordinates runtime startup synchronization, crash recovery, and scheduled monitoring."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        settings: Optional[Settings] = None,
        redis_service: Optional[RedisService] = None,
        reader: Optional[ActiveIndexReader] = None,
        promoter: Optional[IndexPromoter] = None,
        watchdog: Optional[IndexHealthWatchdog] = None,
        escalation_worker: Optional[EscalationWorker] = None,
        reconciliation_worker: Optional[ReconciliationWorker] = None,
        storage_lifecycle_manager: Optional[StorageLifecycleManager] = None,
        retention_worker: Optional[EvidenceRetentionWorker] = None,
        kb_retry_worker: Optional[KBIngestionRetryWorker] = None,
        outbox_worker: Optional[OutboxWorker] = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or default_settings
        self.redis_service = redis_service or get_redis_service()
        self.reader = reader or ActiveIndexReader(settings=self.settings, redis_service=self.redis_service)
        self.watchdog = watchdog or IndexHealthWatchdog(
            settings=self.settings,
            reader=self.reader,
            redis_service=self.redis_service,
        )
        self.promoter = promoter or IndexPromoter(
            settings=self.settings,
            redis_service=self.redis_service,
            watchdog=self.watchdog,
            session_factory=self.session_factory,
        )
        # Ensure watchdog is attached to promoter
        self.promoter.watchdog = self.watchdog
        self.promoter.session_factory = self.session_factory
        self.escalation_worker = escalation_worker or EscalationWorker(
            session_factory=self.session_factory
        )
        self.reconciliation_worker = reconciliation_worker or ReconciliationWorker(
            session_factory=self.session_factory,
            poll_interval_seconds=float(getattr(self.settings, "RECONCILIATION_POLL_INTERVAL_SECONDS", 10.0)),
            stale_timeout_seconds=float(getattr(self.settings, "RECONCILIATION_STALE_TIMEOUT_SECONDS", 300.0)),
        )
        self.storage_lifecycle_manager = storage_lifecycle_manager or StorageLifecycleManager(
            max_batch_size=int(getattr(self.settings, "EVIDENCE_RETENTION_BATCH_SIZE", 50)),
            max_retries=int(getattr(self.settings, "EVIDENCE_RETENTION_MAX_RETRIES", 3)),
        )
        self.retention_worker = retention_worker or EvidenceRetentionWorker(
            session_factory=self.session_factory,
            lifecycle_manager=self.storage_lifecycle_manager,
            scan_interval_seconds=float(getattr(self.settings, "EVIDENCE_RETENTION_SCAN_INTERVAL_SECONDS", 3600.0)),
            batch_size=int(getattr(self.settings, "EVIDENCE_RETENTION_BATCH_SIZE", 50)),
        )
        self.kb_retry_worker = kb_retry_worker or KBIngestionRetryWorker(
            session_factory=self.session_factory,
            settings=self.settings,
            scan_interval_seconds=float(getattr(self.settings, "KB_RETRY_SCAN_INTERVAL_SECONDS", 30.0)),
        )
        self.outbox_worker = outbox_worker or OutboxWorker(
            redis_client=self.redis_service.client if self.redis_service else None,
        )

    async def on_startup(self) -> Dict[str, Any]:
        """Executes complete worker startup protocol.

        1. Tier 3 Reader boot synchronization.
        2. Startup promotion crash reconciliation.
        3. Watchdog restart recovery for interrupted rollbacks and active monitors.
        4. Resumes scheduled background canary monitoring if any index is in MONITORING.
        5. Starts Tier 2 reader periodic background reconciler.
        """
        logger.info("Initializing worker startup lifecycle...")
        results: Dict[str, Any] = {
            "reader_synced": False,
            "reconciliation": None,
            "watchdog_recovery": [],
            "monitoring_resumed": False,
        }

        async with self.session_factory() as session:
            # 1. Tier 3 Reader Startup Synchronization
            try:
                synced = await self.reader.sync_on_startup(session=session)
                results["reader_synced"] = synced
                logger.info("Worker Lifecycle: Reader startup sync result = %s", synced)
            except Exception as exc:
                logger.error("Worker Lifecycle: Reader startup sync failed: %s", exc)

            # 2. Promoter Crash Reconciliation
            try:
                recon = await self.promoter.reconcile_active_index_state(session=session)
                results["reconciliation"] = recon
                logger.info(
                    "Worker Lifecycle: Promoter reconciliation result = %s (action: %s)",
                    recon.reconciled,
                    recon.action_taken,
                )
            except Exception as exc:
                logger.error("Worker Lifecycle: Promoter reconciliation failed: %s", exc)

            # 3. Watchdog Restart Recovery
            try:
                recoveries = await self.watchdog.recover_monitoring_on_restart(session=session)
                results["watchdog_recovery"] = recoveries
                logger.info("Worker Lifecycle: Recovered %d watchdog monitoring items.", len(recoveries))

                # If an active monitoring window was resumed, automatically launch the scheduled monitor!
                for rec in recoveries:
                    if rec.status == "HEALTHY" and rec.active_tag:
                        interval_sec = getattr(self.settings, "WATCHDOG_CHECK_INTERVAL_SECONDS", 30.0)
                        self.watchdog.start_monitoring_scheduler(
                            session_factory=self.session_factory,
                            active_tag=rec.active_tag,
                            interval_seconds=interval_sec,
                        )
                        results["monitoring_resumed"] = True
                        logger.info(
                            "Worker Lifecycle: Automatically resumed monitoring scheduler for '%s'.",
                            rec.active_tag,
                        )
            except Exception as exc:
                logger.error("Worker Lifecycle: Watchdog restart recovery failed: %s", exc)

            enable_workers = getattr(self.settings, "ENABLE_BACKGROUND_WORKERS", True)
            if enable_workers:
                # 5. Escalation Worker Restart Recovery (Day 08)
                try:
                    esc_audits = await self.escalation_worker.scan_and_escalate_batch(session)
                    results["escalation_recovery"] = [a.id for a in esc_audits]
                    logger.info(
                        "Worker Lifecycle: Escalation restart recovery processed %d unresolved negative cases.",
                        len(esc_audits),
                    )
                except Exception as exc:
                    logger.error("Worker Lifecycle: Escalation restart recovery failed: %s", exc)

                # 6. Evidence Reconciliation Startup Recovery & Processing (Day 10)
                try:
                    recon_recovered = await self.reconciliation_worker.recover_stale_jobs(session)
                    results["reconciliation_recovered"] = recon_recovered
                    logger.info(
                        "Worker Lifecycle: Reconciliation startup recovery recovered %d stale job(s).",
                        recon_recovered,
                    )
                    recon_processed = await self.reconciliation_worker.scan_and_process_batch(session)
                    results["reconciliation_startup_processed"] = len(recon_processed)
                    logger.info(
                        "Worker Lifecycle: Reconciliation startup processing processed %d pending job(s).",
                        len(recon_processed),
                    )
                except Exception as exc:
                    logger.error("Worker Lifecycle: Reconciliation startup recovery/processing failed: %s", exc)

                # 7. Evidence Retention Startup Recovery & Sweep (Day 11)
                try:
                    purge_res = await self.storage_lifecycle_manager.execute_purge_batch(session)
                    results["retention_startup_sweep"] = purge_res
                    logger.info(
                        "Worker Lifecycle: Storage retention startup sweep scanned=%d, purged=%d, failed=%d.",
                        purge_res.get("scanned", 0),
                        purge_res.get("purged", 0),
                        purge_res.get("failed", 0),
                    )
                except Exception as exc:
                    logger.error("Worker Lifecycle: Storage retention startup sweep failed: %s", exc)

                # 8. KB Ingestion Retry Startup Lease Recovery (Phase 2.2-B Wave 3)
                try:
                    kb_recovered = await self.kb_retry_worker.retry_service.recover_expired_leases(session)
                    results["kb_retry_recovered"] = kb_recovered
                    logger.info("Worker Lifecycle: KB Ingestion Retry startup recovery recovered %d expired lease(s).", kb_recovered)
                except Exception as exc:
                    logger.error("Worker Lifecycle: KB Ingestion Retry startup recovery failed: %s", exc)

                # 9. Outbox Worker Startup Processing & Recovery
                try:
                    outbox_recovered = await self.outbox_worker.claim_and_process_pending_events(session, batch_size=50)
                    results["outbox_startup_processed"] = len(outbox_recovered)
                    logger.info("Worker Lifecycle: Outbox worker startup recovery processed %d pending event(s).", len(outbox_recovered))
                except Exception as exc:
                    logger.error("Worker Lifecycle: Outbox worker startup recovery failed: %s", exc)
            else:
                logger.info("Worker Lifecycle: Startup recovery sweeps skipped on API mode (ENABLE_BACKGROUND_WORKERS=False).")

        # 4. Start Tier 2 Reader Background Periodic Reconciliation
        try:
            self.reader.start_background_reconciliation(
                interval_seconds=30.0,
                session_provider=self.session_factory,
            )
        except Exception as exc:
            logger.warning("Worker Lifecycle: Could not start reader background reconciler: %s", exc)

        if enable_workers:
            # 6. Start Escalation Worker Periodic Background Scanner
            try:
                self.escalation_worker.start_periodic_scanner()
                results["escalation_worker_started"] = True
                logger.info("Worker Lifecycle: EscalationWorker periodic background scanner started.")
            except Exception as exc:
                logger.error("Worker Lifecycle: Failed to start EscalationWorker background scanner: %s", exc)

            # 7. Start Reconciliation Worker Periodic Background Scanner (Day 10)
            try:
                self.reconciliation_worker.start_periodic_scanner()
                results["reconciliation_worker_started"] = True
                logger.info("Worker Lifecycle: ReconciliationWorker periodic background scanner started.")
            except Exception as exc:
                logger.error("Worker Lifecycle: Failed to start ReconciliationWorker background scanner: %s", exc)

            # 8. Start Evidence Retention Worker Periodic Background Scanner (Day 11)
            try:
                self.retention_worker.start_periodic_scanner()
                results["retention_worker_started"] = True
                logger.info("Worker Lifecycle: EvidenceRetentionWorker periodic background scanner started.")
            except Exception as exc:
                logger.error("Worker Lifecycle: Failed to start EvidenceRetentionWorker background scanner: %s", exc)

            # 9. Start KB Ingestion Retry Worker Periodic Background Scanner (Phase 2.2-B Wave 3)
            try:
                self.kb_retry_worker.start_periodic_scanner()
                results["kb_retry_worker_started"] = True
                logger.info("Worker Lifecycle: KBIngestionRetryWorker periodic background scanner started.")
            except Exception as exc:
                logger.error("Worker Lifecycle: Failed to start KBIngestionRetryWorker background scanner: %s", exc)

            # 10. Start Outbox Worker Periodic Background Scanner
            try:
                outbox_interval = float(getattr(self.settings, "CHAT_OUTBOX_POLL_INTERVAL_SECONDS", 10.0))
                self.outbox_worker.start_periodic_scanner(
                    session_factory=self.session_factory,
                    poll_interval_seconds=outbox_interval,
                )
                results["outbox_worker_started"] = True
                logger.info("Worker Lifecycle: OutboxWorker periodic background scanner started.")
            except Exception as exc:
                logger.error("Worker Lifecycle: Failed to start OutboxWorker background scanner: %s", exc)
        else:
            logger.info("Worker Lifecycle: In-process background workers disabled (ENABLE_BACKGROUND_WORKERS=False).")

        logger.info("Worker startup lifecycle completed successfully.")
        return results

    async def on_shutdown(self) -> None:
        """Executes graceful shutdown of all background tasks and schedulers."""
        logger.info("Shutting down worker lifecycle...")
        if self.watchdog:
            await self.watchdog.stop_monitoring_scheduler_async()
        if self.reader:
            self.reader.stop()
        if self.escalation_worker:
            await self.escalation_worker.stop_async()
        if self.reconciliation_worker:
            await self.reconciliation_worker.stop_async()
        if self.retention_worker:
            await self.retention_worker.stop_async()
        if self.kb_retry_worker:
            await self.kb_retry_worker.stop_async()
        if self.outbox_worker:
            await self.outbox_worker.stop_periodic_scanner()
        logger.info("Worker lifecycle shutdown complete.")


@asynccontextmanager
async def application_lifespan(
    app: Any = None,
    lifecycle_manager: Optional[WorkerLifecycleManager] = None,
):
    """FastAPI/Starlette compatible async lifespan context manager."""
    if lifecycle_manager:
        await lifecycle_manager.on_startup()
    try:
        yield
    finally:
        if lifecycle_manager:
            await lifecycle_manager.on_shutdown()
