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

        # 4. Start Tier 2 Reader Background Periodic Reconciliation
        try:
            self.reader.start_background_reconciliation(
                interval_seconds=30.0,
                session_provider=self.session_factory,
            )
        except Exception as exc:
            logger.warning("Worker Lifecycle: Could not start reader background reconciler: %s", exc)

        logger.info("Worker startup lifecycle completed successfully.")
        return results

    async def on_shutdown(self) -> None:
        """Executes graceful shutdown of all background tasks and schedulers."""
        logger.info("Shutting down worker lifecycle...")
        if self.watchdog:
            await self.watchdog.stop_monitoring_scheduler_async()
        if self.reader:
            self.reader.stop()
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
