"""Background Escalation Worker for Unresolved Negative Cases.

Scans for open conversations with negative sentiment exceeding the 15-minute threshold.
Ensures worker restart resilience, concurrency protection (FOR UPDATE SKIP LOCKED on PostgreSQL),
and zero-regression ticket/audit generation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import List, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.models.customer import Conversation
from app.models.ticket import EscalationAudit, Ticket, TicketEvent
from app.modules.ticketing.escalation_service import EscalationService
from app.modules.ticketing.on_call import OnCallDispatcher

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
        on_call_dispatcher: Optional[OnCallDispatcher] = None,
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock or (escalation_service.clock if escalation_service else system_clock)
        self.escalation_service = escalation_service or EscalationService(clock=self.clock)
        self.on_call_dispatcher = on_call_dispatcher or OnCallDispatcher()
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

    async def scan_and_dispatch_sla_warnings_batch(
        self,
        session: AsyncSession,
        batch_size: Optional[int] = None,
    ) -> List[Ticket]:
        """Scans for active tickets that have reached or passed their 75% SLA warning threshold.

        Guarantees:
        - Exactly-once dispatch: Filters strictly by `sla_warning_sent == False`.
        - Concurrency protection: Employs `FOR UPDATE SKIP LOCKED` on PostgreSQL.
        - SLA Pause Respect: Omits tickets with `sla_paused_at IS NOT NULL` (e.g. PENDING_CUSTOMER).
        - Audit trail: Appends an immutable `TicketEvent` with event_type="SLA_WARNING_TRIGGERED".
        """
        now = self.clock.now()
        limit = batch_size or self.batch_size

        stmt = (
            select(Ticket)
            .where(
                Ticket.status.in_(["OPEN", "IN_PROGRESS"]),
                Ticket.sla_warning_sent == False,
                Ticket.sla_warning_at <= now,
                Ticket.sla_paused_at.is_(None),
            )
            .order_by(Ticket.sla_warning_at.asc())
            .limit(limit)
        )

        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        result = await session.execute(stmt)
        candidate_tickets = result.scalars().all()

        if not candidate_tickets:
            return []

        logger.info(
            "EscalationWorker found %d ticket(s) crossing 75%% SLA warning threshold.",
            len(candidate_tickets),
        )

        warned_tickets: List[Ticket] = []
        for ticket in candidate_tickets:
            try:
                payload = {
                    "ticket_id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "title": ticket.title,
                    "priority": ticket.priority,
                    "priority_score": ticket.priority_score,
                    "category": ticket.category,
                    "assigned_team": ticket.assigned_team,
                    "sla_target_minutes": ticket.sla_target_minutes,
                    "sla_warning_at": ticket.sla_warning_at.isoformat(),
                    "sla_breach_at": ticket.sla_breach_at.isoformat(),
                    "warning_threshold_ratio": 0.75,
                    "dispatched_at": now.isoformat(),
                }

                # Dispatch notification via OnCallDispatcher
                if self.on_call_dispatcher:
                    await self.on_call_dispatcher.dispatch(
                        incident_id=f"SLA-WARN-{ticket.ticket_number}",
                        priority=ticket.priority,
                        summary=f"75% SLA Warning: Ticket {ticket.ticket_number} approaching breach",
                        urgency_score=ticket.priority_score,
                        ticket_id=str(ticket.id),
                        conversation_id=str(ticket.conversation_id) if ticket.conversation_id else None,
                        metadata=payload,
                    )

                ticket.sla_warning_sent = True

                event = TicketEvent(
                    id=uuid.uuid4(),
                    ticket_id=ticket.id,
                    event_type="SLA_WARNING_TRIGGERED",
                    event_payload=payload,
                    created_at=now,
                )
                session.add(event)
                warned_tickets.append(ticket)
            except Exception as exc:
                logger.error(
                    "Error dispatching SLA warning for ticket %s: %s",
                    ticket.ticket_number,
                    exc,
                    exc_info=True,
                )

        if warned_tickets:
            await session.commit()
            logger.info("Successfully dispatched %d 75%% SLA warning(s).", len(warned_tickets))

        return warned_tickets

    async def scan_and_escalate_sla_breaches_batch(
        self,
        session: AsyncSession,
        batch_size: Optional[int] = None,
    ) -> List[EscalationAudit]:
        """Scans open tickets that crossed their 100% SLA breach deadline and auto-escalates them."""
        now = self.clock.now()
        limit = batch_size or self.batch_size

        stmt = (
            select(Ticket)
            .where(
                Ticket.status.in_(["OPEN", "IN_PROGRESS", "ESCALATED"]),
                Ticket.sla_breached == False,
                Ticket.sla_breach_at <= now,
                Ticket.sla_paused_at.is_(None),
            )
            .order_by(Ticket.sla_breach_at.asc())
            .limit(limit)
        )

        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        result = await session.execute(stmt)
        breached_tickets = result.scalars().all()
        if not breached_tickets:
            return []

        logger.warning("EscalationWorker found %d ticket(s) crossing 100%% SLA breach.", len(breached_tickets))
        audits: List[EscalationAudit] = []

        for ticket in breached_tickets:
            try:
                previous_state = ticket.status
                ticket.sla_breached = True
                ticket.status = "ESCALATED"
                if ticket.priority in ("LOW", "MEDIUM"):
                    ticket.priority = "HIGH"
                    ticket.priority_score = max(ticket.priority_score or 0.0, 3.8)
                elif ticket.priority == "HIGH":
                    ticket.priority = "URGENT"
                    ticket.priority_score = max(ticket.priority_score or 0.0, 4.5)

                if ticket.conversation_id:
                    conv = await session.get(Conversation, ticket.conversation_id)
                    if conv and not conv.is_escalated:
                        conv.status = "ESCALATED"
                        conv.is_escalated = True

                payload = {
                    "ticket_id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "previous_state": previous_state,
                    "new_state": "ESCALATED",
                    "new_priority": ticket.priority,
                    "sla_target_minutes": ticket.sla_target_minutes,
                    "sla_breach_at": ticket.sla_breach_at.isoformat(),
                    "breached_at": now.isoformat(),
                }

                if self.on_call_dispatcher:
                    await self.on_call_dispatcher.dispatch(
                        incident_id=f"SLA-BREACH-{ticket.ticket_number}",
                        priority=ticket.priority,
                        summary=f"100% SLA Breach: Ticket {ticket.ticket_number} escalated",
                        urgency_score=ticket.priority_score,
                        ticket_id=str(ticket.id),
                        conversation_id=str(ticket.conversation_id) if ticket.conversation_id else None,
                        metadata=payload,
                    )

                session.add(
                    TicketEvent(
                        id=uuid.uuid4(),
                        ticket_id=ticket.id,
                        event_type="SLA_BREACHED",
                        event_payload=payload,
                        created_at=now,
                    )
                )
                audit = EscalationAudit(
                    id=uuid.uuid4(),
                    ticket_id=ticket.id,
                    conversation_id=ticket.conversation_id,
                    trigger_condition="SLA_BREACH",
                    previous_state=previous_state,
                    new_state="ESCALATED",
                    escalation_reason=f"Ticket {ticket.ticket_number} exceeded 100% SLA breach deadline ({ticket.sla_breach_at.isoformat()}).",
                    escalation_payload=payload,
                    triggered_at=now,
                )
                session.add(audit)
                audits.append(audit)
            except Exception as exc:
                logger.error(
                    "Error escalating SLA-breached ticket %s: %s",
                    ticket.ticket_number,
                    exc,
                    exc_info=True,
                )

        if audits:
            await session.commit()
            logger.warning("Auto-escalated %d SLA-breached ticket(s).", len(audits))

        return audits

    async def run_once(self) -> List[EscalationAudit]:
        """Runs a single scan cycle using the configured session factory."""
        if not self.session_factory:
            raise RuntimeError("Cannot call run_once() without a configured session_factory.")
        async with self.session_factory() as session:
            try:
                audits = await self.scan_and_escalate_batch(session)
                await self.scan_and_dispatch_sla_warnings_batch(session)
                breach_audits = await self.scan_and_escalate_sla_breaches_batch(session)
                audits.extend(breach_audits)
                return audits
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

