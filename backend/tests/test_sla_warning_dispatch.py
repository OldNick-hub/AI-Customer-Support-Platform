"""Tests for 75% SLA Warning Dispatch Worker & Webhook Integration (Task 3 Criterion 9).

Verifies:
1. Tickets reaching or exceeding 75% SLA threshold trigger warning dispatch and status update.
2. Immutable TicketEvent audit record with event_type='SLA_WARNING_TRIGGERED' is generated.
3. Tickets whose SLA is paused (e.g., PENDING_CUSTOMER) are skipped.
4. Tickets below the 75% threshold are not warned prematurely.
5. Exactly-once dispatch: idempotent consecutive worker runs never double-dispatch warnings.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.clock import Clock
from app.db.base import Base
from app.models.customer import Conversation, Customer
from app.models.ticket import EscalationAudit, SLACalendar, Ticket, TicketEvent
from app.modules.ticketing.on_call import OnCallDispatcher, OnCallDispatchResult
from app.modules.ticketing.worker import EscalationWorker


class MockClock(Clock):
    """Controllable deterministic clock for SLA time travel tests."""

    def __init__(self, start_time: datetime) -> None:
        self._current_time = start_time

    def now(self) -> datetime:
        return self._current_time

    def advance(self, duration: timedelta) -> None:
        self._current_time += duration


@pytest.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        yield session, session_factory

    await engine.dispose()


@pytest.fixture
def mock_on_call() -> OnCallDispatcher:
    dispatcher = OnCallDispatcher(enabled=True, webhook_url="http://mock-oncall/webhook")
    dispatcher.dispatch = AsyncMock(
        return_value=OnCallDispatchResult(
            status="CONFIRMED",
            destination="http://mock-oncall/webhook",
            dispatched_at=datetime.now(timezone.utc),
            dispatched_payload={},
            response_code=200,
        )
    )
    return dispatcher


@pytest.mark.asyncio
async def test_sla_75_percent_warning_dispatched(async_db, mock_on_call: OnCallDispatcher):
    """Verifies that a ticket crossing 75% SLA target triggers warning webhook and audit event."""
    session, session_factory = async_db

    # Baseline time: 10:00 UTC
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(start_time=t0)

    # 60 minute target: 75% threshold is at 45 minutes (10:45 UTC), breach at 60 minutes (11:00 UTC)
    warning_time = t0 + timedelta(minutes=45)
    breach_time = t0 + timedelta(minutes=60)

    cal = SLACalendar(id=uuid.uuid4(), name="Test 24/7 Calendar")
    cust = Customer(id=uuid.uuid4(), email="customer@example.com", full_name="Jane Doe")
    ticket = Ticket(
        id=uuid.uuid4(),
        ticket_number="TCK-SLA-001",
        customer_id=cust.id,
        sla_calendar_id=cal.id,
        title="Payment deduction with no receipt",
        description="I was charged $80 but no confirmation was received.",
        masked_summary="Payment deduction with no receipt",
        category="payment_issue",
        status="OPEN",
        priority="HIGH",
        priority_score=3.8,
        assigned_team="Billing",
        sla_target_minutes=60,
        sla_warning_at=warning_time,
        sla_breach_at=breach_time,
        sla_warning_sent=False,
        sla_breached=False,
    )
    session.add_all([cal, cust, ticket])
    await session.commit()

    worker = EscalationWorker(
        session_factory=session_factory,
        clock=clock,
        on_call_dispatcher=mock_on_call,
    )

    # 1. At 10:30 UTC (30 minutes elapsed, below 75%): No warning should fire
    clock.advance(timedelta(minutes=30))
    warned = await worker.scan_and_dispatch_sla_warnings_batch(session)
    assert len(warned) == 0

    await session.refresh(ticket)
    assert ticket.sla_warning_sent is False
    assert mock_on_call.dispatch.call_count == 0

    # 2. Advance to 10:46 UTC (46 minutes elapsed, >= 75% threshold): Warning MUST fire
    clock.advance(timedelta(minutes=16))
    warned = await worker.scan_and_dispatch_sla_warnings_batch(session)
    assert len(warned) == 1
    assert warned[0].ticket_number == "TCK-SLA-001"

    # Verify ticket state updated
    await session.refresh(ticket)
    assert ticket.sla_warning_sent is True
    assert mock_on_call.dispatch.call_count == 1

    call_kwargs = mock_on_call.dispatch.call_args[1]
    assert "TCK-SLA-001" in call_kwargs["summary"]
    assert call_kwargs["priority"] == "HIGH"
    assert call_kwargs["urgency_score"] == 3.8

    # Verify TicketEvent audit log
    event_stmt = select(TicketEvent).where(
        TicketEvent.ticket_id == ticket.id,
        TicketEvent.event_type == "SLA_WARNING_TRIGGERED",
    )
    event_res = await session.execute(event_stmt)
    events = event_res.scalars().all()
    assert len(events) == 1
    assert events[0].event_payload["ticket_number"] == "TCK-SLA-001"
    assert events[0].event_payload["warning_threshold_ratio"] == 0.75

    # 3. Idempotency: Consecutive run does NOT double-dispatch
    warned_again = await worker.scan_and_dispatch_sla_warnings_batch(session)
    assert len(warned_again) == 0
    assert mock_on_call.dispatch.call_count == 1


@pytest.mark.asyncio
async def test_sla_paused_ticket_skips_warning(async_db, mock_on_call: OnCallDispatcher):
    """Verifies that tickets whose SLA is currently paused (e.g., PENDING_CUSTOMER) skip warnings."""
    session, session_factory = async_db

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(start_time=t0)
    warning_time = t0 + timedelta(minutes=45)
    breach_time = t0 + timedelta(minutes=60)

    cal = SLACalendar(id=uuid.uuid4(), name="Test Paused Calendar")
    cust = Customer(id=uuid.uuid4(), email="cust_paused@example.com", full_name="Paused Customer")
    ticket = Ticket(
        id=uuid.uuid4(),
        ticket_number="TCK-PAUSED-001",
        customer_id=cust.id,
        sla_calendar_id=cal.id,
        title="Awaiting customer details",
        description="Pending customer response",
        masked_summary="Pending customer response",
        category="general",
        status="PENDING_CUSTOMER",
        priority="MEDIUM",
        priority_score=2.0,
        sla_target_minutes=60,
        sla_warning_at=warning_time,
        sla_breach_at=breach_time,
        sla_warning_sent=False,
        sla_paused_at=t0 + timedelta(minutes=20),  # Paused at 10:20
    )
    session.add_all([cal, cust, ticket])
    await session.commit()

    worker = EscalationWorker(
        session_factory=session_factory,
        clock=clock,
        on_call_dispatcher=mock_on_call,
    )

    # Advance beyond warning time to 10:50 UTC
    clock.advance(timedelta(minutes=50))
    warned = await worker.scan_and_dispatch_sla_warnings_batch(session)

    # Paused ticket must be ignored
    assert len(warned) == 0
    await session.refresh(ticket)
    assert ticket.sla_warning_sent is False
    assert mock_on_call.dispatch.call_count == 0


@pytest.mark.asyncio
async def test_sla_100_percent_breach_escalates_ticket_and_conversation(async_db, mock_on_call: OnCallDispatcher):
    """Verifies that crossing 100% SLA breach auto-escalates ticket, conversation, audit, and webhook idempotently."""
    session, session_factory = async_db

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(start_time=t0)

    cal = SLACalendar(id=uuid.uuid4(), name="Test Breach 24/7 Calendar")
    cust = Customer(id=uuid.uuid4(), email="breach_cust@example.com", full_name="Breach Customer")
    conv = Conversation(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        customer_id=cust.id,
        primary_language="en",
        status="OPEN",
        is_escalated=False,
    )
    ticket = Ticket(
        id=uuid.uuid4(),
        ticket_number="TCK-BREACH-001",
        conversation_id=conv.id,
        customer_id=cust.id,
        sla_calendar_id=cal.id,
        title="Unresolved critical billing error",
        description="Charge duplicated on account.",
        masked_summary="Charge duplicated on account.",
        category="payment_issue",
        status="OPEN",
        priority="HIGH",
        priority_score=3.8,
        assigned_team="Billing",
        sla_target_minutes=60,
        sla_warning_at=t0 + timedelta(minutes=45),
        sla_breach_at=t0 + timedelta(minutes=60),
        sla_warning_sent=True,
        sla_breached=False,
    )
    session.add_all([cal, cust, conv, ticket])
    await session.commit()

    worker = EscalationWorker(
        session_factory=session_factory,
        clock=clock,
        on_call_dispatcher=mock_on_call,
    )

    # Advance past 60m breach deadline
    clock.advance(timedelta(minutes=61))
    audits = await worker.run_once()

    assert len(audits) == 1
    assert audits[0].trigger_condition == "SLA_BREACH"
    assert audits[0].new_state == "ESCALATED"

    await session.refresh(ticket)
    await session.refresh(conv)
    assert ticket.sla_breached is True
    assert ticket.status == "ESCALATED"
    assert ticket.priority == "URGENT"
    assert conv.status == "ESCALATED"
    assert conv.is_escalated is True
    assert mock_on_call.dispatch.call_count == 1

    ev_res = await session.execute(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
    events = ev_res.scalars().all()
    assert any(ev.event_type == "SLA_BREACHED" for ev in events)

    # Idempotency check: second scan must not re-escalate or re-dispatch
    second_audits = await worker.scan_and_escalate_sla_breaches_batch(session)
    assert second_audits == []
    assert mock_on_call.dispatch.call_count == 1


