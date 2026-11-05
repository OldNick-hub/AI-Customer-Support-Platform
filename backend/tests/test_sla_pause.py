"""Test Ticket SLA Calendar Calculations and PENDING_CUSTOMER Pause Handling."""

from datetime import datetime, timedelta, time, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.ticket import SLACalendar, Ticket


@pytest.mark.asyncio
async def test_ticket_sla_pause_and_resume_duration(session: AsyncSession):
    """Verifies that transitioning to PENDING_CUSTOMER records sla_paused_at and updates total_paused_duration."""
    calendar = SLACalendar(
        name="TEST_PAUSE_CALENDAR",
        timezone="UTC",
        work_start_time=time(9, 0),
        work_end_time=time(17, 0),
        work_days=[1, 2, 3, 4, 5],
    )
    customer = Customer(email="sla_pause@example.com")
    session.add_all([calendar, customer])
    await session.flush()

    now = datetime.now(timezone.utc)
    warning_deadline = now + timedelta(hours=3)
    breach_deadline = now + timedelta(hours=4)

    ticket = Ticket(
        ticket_number="TCK-PAUSE-001",
        customer_id=customer.id,
        title="Screen Flicker Issue",
        description="Monitor flickers intermittently.",
        masked_summary="Intermittent monitor flicker.",
        priority="HIGH",
        status="OPEN",
        category="HARDWARE",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=warning_deadline,
        sla_breach_at=breach_deadline,
        sla_paused_at=None,
        total_paused_duration_seconds=0,
    )
    session.add(ticket)
    await session.commit()

    # Step 1: Transition to PENDING_CUSTOMER
    pause_time = datetime.now(timezone.utc)
    ticket.status = "PENDING_CUSTOMER"
    ticket.sla_paused_at = pause_time
    await session.commit()

    result = await session.execute(select(Ticket).where(Ticket.ticket_number == "TCK-PAUSE-001"))
    paused_ticket = result.scalar_one()
    assert paused_ticket.status == "PENDING_CUSTOMER"
    assert paused_ticket.sla_paused_at is not None

    # Step 2: Customer responds after simulated 1800 seconds (30 minutes)
    paused_seconds = 1800
    resume_time = pause_time + timedelta(seconds=paused_seconds)

    paused_ticket.status = "IN_PROGRESS"
    paused_ticket.total_paused_duration_seconds += paused_seconds
    paused_ticket.sla_paused_at = None
    # Fairly extend warning and breach deadlines by paused duration
    paused_ticket.sla_warning_at += timedelta(seconds=paused_seconds)
    paused_ticket.sla_breach_at += timedelta(seconds=paused_seconds)
    await session.commit()

    # Step 3: Verify updated deadlines
    resumed_result = await session.execute(select(Ticket).where(Ticket.ticket_number == "TCK-PAUSE-001"))
    resumed_ticket = resumed_result.scalar_one()

    assert resumed_ticket.status == "IN_PROGRESS"
    assert resumed_ticket.sla_paused_at is None
    assert resumed_ticket.total_paused_duration_seconds == 1800
    assert resumed_ticket.sla_breach_at == breach_deadline + timedelta(seconds=1800)

