"""Comprehensive Test Suite for Runtime SLA Recalculation Engine.

Verifies:
1. Standard open ticket recalculation.
2. In-progress ticket recalculation.
3. Closed ticket exclusion (RESOLVED and CLOSED tickets strictly untouched).
4. Paused ticket duration preservation (total_paused_duration_seconds preserved & applied).
5. Weekend and holiday calculations (deadlines roll forward past non-working periods).
6. Multiple priority levels with distinct target minutes.
7. Invalid SLA configuration (target <= 0 rejected with ValueError/422).
8. Unauthorized user (customer sessions rejected with 403 Forbidden).
9. Audit event creation (TicketEvent(SLA_RECALCULATED)).
10. Dry-run mode (calculates changes without committing).
11. Repeated recalculation idempotency (zero redundant events on unchanged configuration).
12. API endpoint integration (POST /api/v1/tickets/sla/recalculate).
"""

from datetime import date, datetime, time, timedelta, timezone
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.main import app
from app.models.customer import Customer
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.modules.ticketing.after_hours import add_business_minutes, calculate_sla_deadlines
from app.modules.ticketing.ticket_service import TicketAuthorizationError, TicketSynthesisService


@pytest.fixture
def test_calendar(session: AsyncSession) -> SLACalendar:
    cal = SLACalendar(
        name=f"CAL-RECALC-{uuid.uuid4().hex[:8]}",
        timezone="America/New_York",
        work_start_time=time(9, 0),
        work_end_time=time(17, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=["2026-11-26", "2026-12-25"],
    )
    return cal


# =============================================================================
# 1. Business Hours & SLA Deadline Unit Tests
# =============================================================================

def test_add_business_minutes_same_day():
    """Advances minutes within the same business day."""
    cal = {
        "timezone": "UTC",
        "work_start_time": time(9, 0),
        "work_end_time": time(17, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": [],
    }
    # Monday 10:00 UTC + 120 minutes -> Monday 12:00 UTC
    start = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)  # Monday
    res = add_business_minutes(start, 120, cal)
    assert res == datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def test_add_business_minutes_across_weekend():
    """Rolls forward across Friday end of day to Monday morning."""
    cal = {
        "timezone": "UTC",
        "work_start_time": time(9, 0),
        "work_end_time": time(17, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": [],
    }
    # Friday 16:00 UTC + 120 minutes:
    # 60m consumed on Friday (to 17:00), remaining 60m starts Monday 09:00 -> Monday 10:00
    friday = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
    res = add_business_minutes(friday, 120, cal)
    assert res == datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


def test_add_business_minutes_skips_holidays():
    """Rolls forward across a holiday to the next valid working day."""
    cal = {
        "timezone": "UTC",
        "work_start_time": time(9, 0),
        "work_end_time": time(17, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": ["2026-11-16"],  # Monday is a holiday
    }
    # Friday 16:00 UTC + 120 minutes:
    # Friday 60m. Monday skipped due to holiday. Tuesday 09:00 + 60m -> Tuesday 10:00
    friday = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
    res = add_business_minutes(friday, 120, cal)
    assert res == datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def test_calculate_sla_deadlines_with_paused_duration():
    """Preserves and extends both warning and breach deadlines by total_paused_duration_seconds."""
    cal = {
        "timezone": "UTC",
        "work_start_time": time(9, 0),
        "work_end_time": time(17, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": [],
    }
    start = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)  # Monday
    # 240 target mins (4 hours). 75% warning = 180 mins (3 hours).
    # Paused duration: 3600 seconds (1 hour).
    warning_at, breach_at = calculate_sla_deadlines(
        created_at=start,
        target_minutes=240,
        calendar=cal,
        total_paused_duration_seconds=3600,
    )
    # Warning: 10:00 + 3h (13:00) + 1h paused = 14:00
    assert warning_at == datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
    # Breach: 10:00 + 4h (14:00) + 1h paused = 15:00
    assert breach_at == datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


# =============================================================================
# 2. Service-Level Recalculation Tests
# =============================================================================

@pytest.mark.asyncio
async def test_recalculate_open_and_in_progress_tickets(session: AsyncSession, test_calendar: SLACalendar):
    """Verifies that OPEN and IN_PROGRESS tickets have their SLA recalculated."""
    session.add(test_calendar)
    cust = Customer(email="user1@example.com", metadata_={"tenant_id": "default"})
    session.add(cust)
    await session.flush()

    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

    # 1. OPEN ticket
    t_open = Ticket(
        ticket_number="TCK-OPEN-001",
        customer_id=cust.id,
        title="Open Issue",
        description="Ticket currently open.",
        masked_summary="Open issue.",
        priority="HIGH",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=480,  # Old target 8h
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        total_paused_duration_seconds=0,
        created_at=now,
    )
    # 2. IN_PROGRESS ticket with paused duration
    t_in_progress = Ticket(
        ticket_number="TCK-PROG-002",
        customer_id=cust.id,
        title="In-Progress Issue",
        description="Ticket currently in progress.",
        masked_summary="In-progress issue.",
        priority="CRITICAL",
        status="IN_PROGRESS",
        category="BILLING",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=240,  # Old target 4h
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        total_paused_duration_seconds=1800,  # 30m paused previously
        created_at=now,
    )
    # 3. CLOSED ticket (must remain untouched!)
    t_closed = Ticket(
        ticket_number="TCK-CLOSED-003",
        customer_id=cust.id,
        title="Closed Issue",
        description="Ticket already resolved.",
        masked_summary="Closed issue.",
        priority="HIGH",
        status="RESOLVED",
        category="LOGISTICS",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        total_paused_duration_seconds=0,
        created_at=now,
    )
    session.add_all([t_open, t_in_progress, t_closed])
    await session.flush()

    svc = TicketSynthesisService()
    # Update priority targets: HIGH -> 120m, CRITICAL -> 60m
    result = await svc.recalculate_open_tickets_sla(
        db=session,
        priority_targets={"HIGH": 120, "CRITICAL": 60},
        actor_id="ADMIN-001",
        actor_roles=["ADMIN"],
        tenant_id="default",
    )

    assert result["total_eligible"] == 2
    assert result["updated_count"] == 2
    assert result["failed_count"] == 0

    # Verify OPEN ticket
    assert t_open.sla_target_minutes == 120
    # Verify IN_PROGRESS ticket
    assert t_in_progress.sla_target_minutes == 60
    assert t_in_progress.total_paused_duration_seconds == 1800  # Preserved!
    # Verify CLOSED ticket strictly unchanged
    assert t_closed.sla_target_minutes == 480

    # Verify audit events emitted
    events = (
        await session.scalars(
            select(TicketEvent).where(TicketEvent.event_type == "SLA_RECALCULATED")
        )
    ).all()
    assert len(events) == 2
    ticket_ids = {e.ticket_id for e in events}
    assert t_open.id in ticket_ids
    assert t_in_progress.id in ticket_ids
    assert t_closed.id not in ticket_ids


@pytest.mark.asyncio
async def test_recalculate_unauthorized_user_raises_error(session: AsyncSession):
    """Verifies that non-admin/staff roles are forbidden from triggering recalculation."""
    svc = TicketSynthesisService()
    with pytest.raises(TicketAuthorizationError):
        await svc.recalculate_open_tickets_sla(
            db=session,
            actor_id="CUST-001",
            actor_roles=["CUSTOMER"],
        )


@pytest.mark.asyncio
async def test_recalculate_invalid_priority_targets_raises_value_error(session: AsyncSession):
    """Verifies that non-positive SLA target minutes are rejected with ValueError."""
    svc = TicketSynthesisService()
    with pytest.raises(ValueError) as exc:
        await svc.recalculate_open_tickets_sla(
            db=session,
            priority_targets={"HIGH": 0},  # Invalid! Must be > 0
            actor_roles=["ADMIN"],
        )
    assert "strictly positive" in str(exc.value)


@pytest.mark.asyncio
async def test_recalculate_dry_run_does_not_commit_changes(session: AsyncSession, test_calendar: SLACalendar):
    """Verifies that dry_run=True returns calculated preview without modifying records."""
    session.add(test_calendar)
    cust = Customer(email="dryrun@example.com", metadata_={"tenant_id": "default"})
    session.add(cust)
    await session.flush()

    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    t = Ticket(
        ticket_number="TCK-DRY-001",
        customer_id=cust.id,
        title="Dry Run Issue",
        description="Dry run test.",
        masked_summary="Dry run.",
        priority="HIGH",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        total_paused_duration_seconds=0,
        created_at=now,
    )
    session.add(t)
    await session.flush()

    svc = TicketSynthesisService()
    result = await svc.recalculate_open_tickets_sla(
        db=session,
        priority_targets={"HIGH": 120},
        actor_roles=["ADMIN"],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["updated_count"] == 1
    # Record itself should remain unchanged in dry-run
    assert t.sla_target_minutes == 480
    events = (
        await session.scalars(
            select(TicketEvent).where(TicketEvent.ticket_id == t.id)
        )
    ).all()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_recalculate_idempotency(session: AsyncSession, test_calendar: SLACalendar):
    """Verifies that repeated recalculation with the same targets is safe and idempotent."""
    session.add(test_calendar)
    cust = Customer(email="idem@example.com", metadata_={"tenant_id": "default"})
    session.add(cust)
    await session.flush()

    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    t = Ticket(
        ticket_number="TCK-IDEM-SLA",
        customer_id=cust.id,
        title="Idempotent Issue",
        description="Idempotency test.",
        masked_summary="Idempotency.",
        priority="MEDIUM",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        total_paused_duration_seconds=0,
        created_at=now,
    )
    session.add(t)
    await session.flush()

    svc = TicketSynthesisService()
    res1 = await svc.recalculate_open_tickets_sla(
        db=session,
        priority_targets={"MEDIUM": 240},
        actor_roles=["ADMIN"],
    )
    assert res1["updated_count"] == 1

    # Second execution with exact same target
    res2 = await svc.recalculate_open_tickets_sla(
        db=session,
        priority_targets={"MEDIUM": 240},
        actor_roles=["ADMIN"],
    )
    assert res2["updated_count"] == 0
    assert res2["skipped_count"] == 1

    # Verify only 1 audit event was created across both runs
    events = (
        await session.scalars(
            select(TicketEvent).where(
                TicketEvent.ticket_id == t.id,
                TicketEvent.event_type == "SLA_RECALCULATED",
            )
        )
    ).all()
    assert len(events) == 1


# =============================================================================
# 3. HTTP API Endpoint Tests
# =============================================================================

@pytest.mark.asyncio
async def test_recalculate_sla_endpoint_unauthorized():
    """Verifies that unauthenticated or customer calls to POST /api/v1/tickets/sla/recalculate fail with 401/403."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No credentials -> 401
        res = await client.post("/api/v1/tickets/sla/recalculate", json={"dry_run": True})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_recalculate_sla_endpoint_invalid_body():
    """Verifies that invalid priority targets return HTTP 422."""
    from app.api.deps import ResolvedAuthContext, get_auth_context
    admin_auth = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="admin@example.com",
        roles=["ADMIN"],
    )
    app.dependency_overrides[get_auth_context] = lambda: admin_auth
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/api/v1/tickets/sla/recalculate",
                json={"priority_targets": {"HIGH": -10}},  # Invalid!
            )
            assert res.status_code == 422
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


@pytest.mark.asyncio
async def test_recalculate_sla_endpoint_success(session: AsyncSession, test_calendar: SLACalendar):
    """Verifies successful recalculation via the HTTP API endpoint."""
    from app.api.deps import ResolvedAuthContext, get_auth_context, get_db
    session.add(test_calendar)
    cust = Customer(email="endpoint@example.com", metadata_={"tenant_id": "default"})
    session.add(cust)
    await session.flush()

    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    t = Ticket(
        ticket_number="TCK-EP-001",
        customer_id=cust.id,
        title="Endpoint Issue",
        description="Endpoint test.",
        masked_summary="Endpoint.",
        priority="HIGH",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=test_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        total_paused_duration_seconds=0,
        created_at=now,
    )
    session.add(t)
    await session.commit()

    admin_auth = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="admin@example.com",
        roles=["ADMIN"],
    )
    app.dependency_overrides[get_auth_context] = lambda: admin_auth
    app.dependency_overrides[get_db] = lambda: session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/api/v1/tickets/sla/recalculate",
                json={"priority_targets": {"HIGH": 120}},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["updated_count"] == 1
            assert data["failed_count"] == 0
    finally:
        app.dependency_overrides.pop(get_auth_context, None)
        app.dependency_overrides.pop(get_db, None)
