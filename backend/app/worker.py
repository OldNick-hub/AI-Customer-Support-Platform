"""Dedicated Background Worker Process Daemon.

Provides a standalone background runner for asynchronous platform tasks:
1. EscalationWorker (15m negative sentiment SLA escalation scanner)
2. ReconciliationWorker (multimodal evidence claim reconciler & crash recovery)
3. EvidenceRetentionWorker (30-day storage lifecycle retention purger with PostgreSQL SKIP LOCKED)
4. ActiveIndexReader & IndexHealthWatchdog background reconciliation
5. KBIngestionRetryWorker (persistent 15m/30m/60m KB ingestion retry scheduler and worker)

Used in distributed/multi-container deployments when ENABLE_BACKGROUND_WORKERS=False on web API nodes.
"""

import asyncio
import logging
import signal
import sys

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.db.session import AsyncSessionLocal
from app.lifecycle import WorkerLifecycleManager

logger = configure_safe_logger(logging.getLogger("app.worker"))


async def run_worker() -> None:
    """Initializes and runs all background workers until interrupted."""
    logger.info("Starting standalone Background Worker Daemon...")

    # Fast-fail production security validation if configured in production mode
    if hasattr(settings, "validate_production_configuration"):
        try:
            settings.validate_production_configuration()
            logger.info("Production configuration validated successfully.")
        except Exception as exc:
            logger.error("Configuration validation error: %s", exc)
            sys.exit(1)

    manager = WorkerLifecycleManager(
        session_factory=AsyncSessionLocal,
        settings=settings,
    )

    startup_results = await manager.on_startup()
    logger.info("Worker startup completed successfully: %s", startup_results)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Termination signal received. Initiating graceful shutdown...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Signal handling on Windows
            pass

    logger.info("Background Worker Daemon active. Awaiting scheduled tasks...")
    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Worker interrupted by user/system.")
    finally:
        logger.info("Stopping all background workers and flushing tasks...")
        await manager.on_shutdown()
        logger.info("Background Worker Daemon terminated cleanly.")


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

