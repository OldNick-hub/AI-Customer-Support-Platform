"""Background Escalation Worker for Unresolved Negative Cases.

Scans for open conversations with negative sentiment exceeding the 15-minute threshold.
Ensures worker restart resilience, concurrency protection (FOR UPDATE SKIP LOCKED on PostgreSQL),
and zero-regression ticket/audit generation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.models.customer import Conversation
from app.models.ticket import EscalationAudit
from app.modules.ticketing.escalation_service import EscalationService

logger = configure_safe_logger(logging.getLogger(__name__))


class EscalationWorker:
    """Asynchronous background worker scanning and escalating unresolved negative cases."""

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        escalation_service: Optional[EscalationService] = None,
        clock: Optional[Clock] = None,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 50,
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock or (escalation_service.clock if escalation_service else system_clock)
        self.escalation_service = escalation_service or EscalationService(clock=self.clock)
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        self.scan_count: int = 0
        self.last_scan_at: Optional[datetime] = None
        self.last_error: Optional[Exception] = None

    async def scan_and_escalate_batch(
        self,
        session: AsyncSession,
        batch_size: Optional[int] = None,
    ) -> List[EscalationAudit]:
        """Performs a single deterministic scan of eligible 15-minute unresolved negative cases.

        Concurrency & Crash Recovery Guarantees:
        - Filters by `status = 'OPEN'`, `is_escalated = False`, and `negative_detected_at <= cutoff`.
        - Uses `FOR UPDATE SKIP LOCKED` on PostgreSQL to prevent parallel worker contention.
        - Persisted timestamps ensure zero state loss across worker restarts.
        """
        now = self.clock.now()
        limit = batch_size or self.batch_size
        cutoff = now - timedelta(minutes=self.escalation_service.unresolved_timeout_minutes)

        # Base query: strictly MORE THAN 15 minutes (< cutoff)
        stmt = (
            select(Conversation)
            .where(
                Conversation.status == "OPEN",
                Conversation.is_escalated == False,
                Conversation.negative_detected_at.is_not(None),
                Conversation.negative_detected_at < cutoff,
            )
            .order_by(Conversation.negative_detected_at.asc())
            .limit(limit)
        )

        # Apply PostgreSQL row-level skip-locked concurrency if supported
        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        result = await session.execute(stmt)
        candidates = result.scalars().all()

        if not candidates:
            return []

        logger.info(
            "EscalationWorker picked up %d candidate conversation(s) exceeding %dm unresolved timeout.",
            len(candidates),
            self.escalation_service.unresolved_timeout_minutes,
        )

        escalated_audits: List[EscalationAudit] = []
        for conv in candidates:
            try:
                audit = await self.escalation_service.evaluate_unresolved_negative_timeout(
                    session=session,
                    conversation_id=conv.id,
                )
                if audit:
                    escalated_audits.append(audit)
            except Exception as exc:
                logger.error(
                    "Error evaluating 15m timeout for conversation %s: %s",
                    conv.id,
                    exc,
                    exc_info=True,
                )

        if escalated_audits:
            await session.commit()
            logger.info("EscalationWorker successfully escalated %d conversation(s).", len(escalated_audits))

        return escalated_audits

    async def run_once(self) -> List[EscalationAudit]:
        """Runs a single scan cycle using the configured session factory."""
        if not self.session_factory:
            raise RuntimeError("Cannot call run_once() without a configured session_factory.")
        async with self.session_factory() as session:
            try:
                return await self.scan_and_escalate_batch(session)
            except Exception:
                await session.rollback()
                raise

    def stop(self) -> None:
        """Signals the background loop to stop cleanly."""
        self._stop_event.set()

    async def stop_async(self, timeout_seconds: float = 2.0) -> None:
        """Gracefully signals stop, cancels if necessary, and awaits completion of the background task."""
        self._stop_event.set()
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def start_periodic_scanner(self, max_iterations: Optional[int] = None) -> asyncio.Task:
        """Starts the periodic scanner as a managed background asyncio.Task."""
        if self._task and not self._task.done():
            logger.warning("EscalationWorker background scanner is already running.")
            return self._task

        self._stop_event.clear()
        self.is_running = True
        self._task = asyncio.create_task(self.start(max_iterations=max_iterations))

        def _on_task_done(task: asyncio.Task) -> None:
            self.is_running = False
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    self.last_error = exc
                    logger.critical(
                        "EscalationWorker background task terminated with unhandled exception: %s",
                        exc,
                        exc_info=exc,
                    )

        self._task.add_done_callback(_on_task_done)
        return self._task

    async def start(self, max_iterations: Optional[int] = None) -> None:
        """Starts the worker polling loop until stopped or max_iterations reached."""
        self._stop_event.clear()
        self.is_running = True
        iterations = 0
        logger.info("Starting EscalationWorker loop (interval: %.1fs)...", self.poll_interval_seconds)

        try:
            while not self._stop_event.is_set():
                try:
                    self.scan_count += 1
                    self.last_scan_at = self.clock.now()
                    await self.run_once()
                except Exception as exc:
                    self.last_error = exc
                    logger.error("Unexpected error in EscalationWorker loop: %s", exc, exc_info=True)

                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("EscalationWorker task received cancellation. Exiting cleanly.")
            raise
        except Exception as unhandled:
            self.last_error = unhandled
            logger.critical("Unhandled critical failure in EscalationWorker loop: %s", unhandled, exc_info=True)
            raise
        finally:
            self.is_running = False
            logger.info("EscalationWorker loop terminated.")

