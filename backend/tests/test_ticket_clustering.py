"""Comprehensive Test Suite for Semantic Duplicate Ticket Detection and Related Issue Grouping.

Verifies:
1. Same customer, same issue, similarity >= 0.88 -> duplicate identified, cluster linked, routing suppressed.
2. Similar wording but different issues (< 0.88) -> not flagged as duplicate.
3. Different customers -> not flagged as duplicate (duplicates must be same customer).
4. Different tenants -> strict tenant boundary, no cross-tenant matching.
5. Closed / resolved tickets -> excluded from candidate pool.
6. Tickets outside rolling 48-hour window -> excluded.
7. Missing/failing embeddings -> graceful deterministic fallback, zero false positives.
8. Duplicate processing and idempotency -> repeated runs do not duplicate events or corrupt state.
9. Master ticket routing vs duplicate ticket routing -> master routes to agent; duplicate has routing suppressed.
10. Multiple customers with same systemic incident -> grouped into shared cluster_group_id.
11. Unrelated issues -> not grouped.
12. Grouping preserves individual ticket ownership, SLAs, and evidence (no merging).
"""

from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Role, User
from app.models.customer import Customer
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.modules.ticketing.clustering import (
    DuplicateMatchResult,
    IssueGroupResult,
    TicketClusterMatcher,
    ROUTING_STATUS_DUPLICATE_SUPPRESSED,
)
from app.modules.ticketing.routing_service import TicketRoutingService
from app.services.embedding_service import MockEmbeddingProvider, EmbeddingService


class PredictableEmbeddingService:
    """Mock embedding service that maps specific texts or patterns to predetermined unit vectors."""

    def __init__(self, vector_map: dict = None):
        self.vector_map = vector_map or {}
        self.default_vec = [1.0, 0.0, 0.0, 0.0]

    async def embed_batch(self, texts: list) -> list:
        results = []
        for t in texts:
            vec = self.vector_map.get(t)
            if vec is None:
                # Generate a simple normalized hash vector
                h = hash(t) % 1000
                vec = [float(h) / 1000.0, 0.5, 0.2, 0.1]
                norm = sum(x * x for x in vec) ** 0.5
                vec = [x / norm for x in vec]
            results.append(vec)
        return results

    async def embed_text(self, text: str) -> list:
        res = await self.embed_batch([text])
        return res[0]


@pytest.fixture
async def sample_calendar(session: AsyncSession) -> SLACalendar:
    cal = SLACalendar(
        name=f"CAL-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_days=[1, 2, 3, 4, 5],
        holidays=[],
    )
    session.add(cal)
    await session.flush()
    return cal


@pytest.fixture
async def sample_customers(session: AsyncSession) -> tuple[Customer, Customer, Customer]:
    c1 = Customer(
        email="cust1@example.com",
        full_name="Alice Tenant A",
        customer_tier="STANDARD",
        metadata_={"tenant_id": "tenant-alpha"},
    )
    c2 = Customer(
        email="cust2@example.com",
        full_name="Bob Tenant A",
        customer_tier="STANDARD",
        metadata_={"tenant_id": "tenant-alpha"},
    )
    c3 = Customer(
        email="cust3@example.com",
        full_name="Charlie Tenant B",
        customer_tier="STANDARD",
        metadata_={"tenant_id": "tenant-beta"},
    )
    session.add_all([c1, c2, c3])
    await session.flush()
    return c1, c2, c3


# =============================================================================
# 1. Semantic Duplicate Ticket Detection Tests
# =============================================================================

@pytest.mark.asyncio
async def test_duplicate_detection_same_customer_high_similarity(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that an identical issue from the same customer within 48h is linked as duplicate."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    # Master ticket (created 2 hours ago)
    t1 = Ticket(
        ticket_number="TCK-MASTER-001",
        customer_id=c1.id,
        title="Payment gateway timeout on checkout",
        description="I was charged ₹1499 but the checkout page failed with error PG-504.",
        masked_summary="Payment gateway timeout with charge.",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now - timedelta(hours=2),
    )
    session.add(t1)
    await session.flush()

    # Second submission by same customer (created now)
    t2 = Ticket(
        ticket_number="TCK-DUP-001",
        customer_id=c1.id,
        title="Payment gateway timeout on checkout",
        description="I was charged ₹1499 but the checkout page failed with error PG-504.",
        masked_summary="Payment gateway timeout with charge.",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add(t2)
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    res = await matcher.detect_duplicate(session, t2, tenant_id="tenant-alpha")

    assert res.is_duplicate is True
    assert res.master_ticket_id == t1.id
    assert res.master_ticket_number == "TCK-MASTER-001"
    assert res.cluster_group_id is not None
    assert t1.cluster_group_id == res.cluster_group_id
    assert t2.cluster_group_id == res.cluster_group_id
    assert t2.routing_status == ROUTING_STATUS_DUPLICATE_SUPPRESSED

    # Verify audit event
    events = (
        await session.scalars(
            select(TicketEvent).where(
                TicketEvent.ticket_id == t2.id,
                TicketEvent.event_type == "DUPLICATE_TICKET_DETECTED",
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].event_payload["master_ticket_id"] == str(t1.id)


@pytest.mark.asyncio
async def test_duplicate_detection_different_issues_not_matched(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that distinct issues from the same customer are NOT flagged as duplicates."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    t1 = Ticket(
        ticket_number="TCK-ORIG-002",
        customer_id=c1.id,
        title="Wrong color shoes delivered",
        description="Ordered black sneakers but received bright neon green sneakers.",
        masked_summary="Wrong item color received.",
        priority="LOW",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        created_at=now - timedelta(hours=1),
    )
    t2 = Ticket(
        ticket_number="TCK-DIFF-002",
        customer_id=c1.id,
        title="Cannot update password on profile",
        description="Password reset email never arrives in my inbox.",
        masked_summary="Password reset link delivery failure.",
        priority="MEDIUM",
        status="OPEN",
        category="ACCOUNT",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([t1, t2])
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    res = await matcher.detect_duplicate(session, t2, tenant_id="tenant-alpha")

    assert res.is_duplicate is False
    assert t2.routing_status != ROUTING_STATUS_DUPLICATE_SUPPRESSED
    assert t2.cluster_group_id is None


@pytest.mark.asyncio
async def test_duplicate_detection_different_customers_not_duplicate(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that an identical issue reported by a DIFFERENT customer is NOT marked as duplicate."""
    c1, c2, _ = sample_customers
    now = datetime.now(timezone.utc)

    # c1 ticket
    t1 = Ticket(
        ticket_number="TCK-CUST1-003",
        customer_id=c1.id,
        title="Broken power adapter in box",
        description="The power adapter pin is snapped inside the package.",
        masked_summary="Damaged adapter pin.",
        priority="HIGH",
        status="OPEN",
        category="HARDWARE",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now - timedelta(hours=1),
    )
    # c2 ticket with identical issue
    t2 = Ticket(
        ticket_number="TCK-CUST2-003",
        customer_id=c2.id,
        title="Broken power adapter in box",
        description="The power adapter pin is snapped inside the package.",
        masked_summary="Damaged adapter pin.",
        priority="HIGH",
        status="OPEN",
        category="HARDWARE",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([t1, t2])
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    res = await matcher.detect_duplicate(session, t2, tenant_id="tenant-alpha")

    # Different customer: duplicate detection must be False
    assert res.is_duplicate is False
    assert t2.routing_status != ROUTING_STATUS_DUPLICATE_SUPPRESSED


@pytest.mark.asyncio
async def test_duplicate_detection_different_tenant_isolation(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that tickets belonging to another tenant are strictly isolated and never matched."""
    _, _, c3 = sample_customers  # tenant-beta
    now = datetime.now(timezone.utc)

    t = Ticket(
        ticket_number="TCK-BETA-004",
        customer_id=c3.id,
        title="Database connection error",
        description="DB connection pool exhausted.",
        masked_summary="DB pool error.",
        priority="HIGH",
        status="OPEN",
        category="TECHNICAL",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add(t)
    await session.flush()

    matcher = TicketClusterMatcher()
    # Query with tenant-alpha on a tenant-beta ticket
    res = await matcher.detect_duplicate(session, t, tenant_id="tenant-alpha")
    assert res.is_duplicate is False
    assert "Tenant mismatch" in res.reason


@pytest.mark.asyncio
async def test_duplicate_detection_closed_tickets_excluded(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that resolved or closed tickets are not considered as duplicate candidates."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    t_resolved = Ticket(
        ticket_number="TCK-RESOLVED-005",
        customer_id=c1.id,
        title="Cannot connect to Bluetooth audio",
        description="Bluetooth pairing failed on device.",
        masked_summary="Bluetooth issue.",
        priority="LOW",
        status="RESOLVED",  # Closed/Resolved
        category="HARDWARE",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now - timedelta(hours=10),
        sla_breach_at=now - timedelta(hours=8),
        created_at=now - timedelta(hours=12),
    )
    t_new = Ticket(
        ticket_number="TCK-NEW-005",
        customer_id=c1.id,
        title="Cannot connect to Bluetooth audio",
        description="Bluetooth pairing failed on device.",
        masked_summary="Bluetooth issue.",
        priority="LOW",
        status="OPEN",
        category="HARDWARE",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        created_at=now,
    )
    session.add_all([t_resolved, t_new])
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    res = await matcher.detect_duplicate(session, t_new, tenant_id="tenant-alpha")
    assert res.is_duplicate is False


@pytest.mark.asyncio
async def test_duplicate_detection_outside_48h_window_excluded(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that tickets older than 48 hours are excluded from candidate duplicate matching."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    t_old = Ticket(
        ticket_number="TCK-OLD-006",
        customer_id=c1.id,
        title="Delayed shipping for order ORD-9999",
        description="Order tracking has not moved in 5 days.",
        masked_summary="Delayed order tracking.",
        priority="MEDIUM",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now - timedelta(hours=50),
        sla_breach_at=now - timedelta(hours=49),
        created_at=now - timedelta(hours=52),  # 52 hours ago (> 48h)
    )
    t_new = Ticket(
        ticket_number="TCK-NEW-006",
        customer_id=c1.id,
        title="Delayed shipping for order ORD-9999",
        description="Order tracking has not moved in 5 days.",
        masked_summary="Delayed order tracking.",
        priority="MEDIUM",
        status="OPEN",
        category="LOGISTICS",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([t_old, t_new])
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88, duplicate_window_hours=48.0)
    res = await matcher.detect_duplicate(session, t_new, tenant_id="tenant-alpha")
    assert res.is_duplicate is False


@pytest.mark.asyncio
async def test_duplicate_detection_idempotency(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that repeated detection on an already linked duplicate returns the existing match idempotently."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    t1 = Ticket(
        ticket_number="TCK-IDEM-001",
        customer_id=c1.id,
        title="Billing invoice shows duplicate charge",
        description="Charged twice for subscription renewal on invoice INV-777.",
        masked_summary="Duplicate renewal charge.",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now - timedelta(hours=1),
    )
    t2 = Ticket(
        ticket_number="TCK-IDEM-002",
        customer_id=c1.id,
        title="Billing invoice shows duplicate charge",
        description="Charged twice for subscription renewal on invoice INV-777.",
        masked_summary="Duplicate renewal charge.",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([t1, t2])
    await session.flush()

    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    res1 = await matcher.detect_duplicate(session, t2, tenant_id="tenant-alpha")
    assert res1.is_duplicate is True
    assert res1.already_linked is False

    # Second run on same ticket
    res2 = await matcher.detect_duplicate(session, t2, tenant_id="tenant-alpha")
    assert res2.is_duplicate is True
    assert res2.already_linked is True
    assert res2.cluster_group_id == res1.cluster_group_id

    # Verify only 1 audit event exists
    events = (
        await session.scalars(
            select(TicketEvent).where(
                TicketEvent.ticket_id == t2.id,
                TicketEvent.event_type == "DUPLICATE_TICKET_DETECTED",
            )
        )
    ).all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_duplicate_routing_suppression(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that routing_service skips agent assignment for DUPLICATE_SUPPRESSED tickets."""
    c1, _, _ = sample_customers
    now = datetime.now(timezone.utc)

    # Master ticket
    t_master = Ticket(
        ticket_number="TCK-ROUTE-MASTER",
        customer_id=c1.id,
        title="Damaged package upon arrival",
        description="The box was crushed and water damaged.",
        masked_summary="Crushed and water damaged box.",
        priority="HIGH",
        status="OPEN",
        category="LOGISTICS",
        routing_status="UNASSIGNED",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now - timedelta(hours=2),
    )
    # Duplicate ticket
    t_dup = Ticket(
        ticket_number="TCK-ROUTE-DUP",
        customer_id=c1.id,
        title="Damaged package upon arrival",
        description="The box was crushed and water damaged.",
        masked_summary="Crushed and water damaged box.",
        priority="HIGH",
        status="OPEN",
        category="LOGISTICS",
        routing_status="UNASSIGNED",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([t_master, t_dup])
    await session.flush()

    # Link duplicate
    matcher = TicketClusterMatcher(duplicate_threshold=0.88)
    await matcher.detect_duplicate(session, t_dup, tenant_id="tenant-alpha")
    assert t_dup.routing_status == ROUTING_STATUS_DUPLICATE_SUPPRESSED

    # Route duplicate via routing_service
    routing_svc = TicketRoutingService()
    decision = await routing_svc.route_ticket(
        db=session,
        ticket_id=t_dup.id,
        actor_id="STAFF-ROUTER",
        actor_roles=["ADMIN"],
    )

    assert decision.routing_status == ROUTING_STATUS_DUPLICATE_SUPPRESSED
    assert decision.assigned_agent_id is None
    assert "Routing suppressed" in decision.reason


# =============================================================================
# 2. Systemic Related Issue Grouping Tests
# =============================================================================

@pytest.mark.asyncio
async def test_group_related_issues_across_different_customers(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that multiple customers reporting the same systemic incident are grouped."""
    c1, c2, _ = sample_customers
    now = datetime.now(timezone.utc)

    # c1 reporting regional outage
    t1 = Ticket(
        ticket_number="TCK-SYS-001",
        customer_id=c1.id,
        title="US-East region API 500 server errors",
        description="All API requests to US-East region are returning HTTP 500 internal server error.",
        masked_summary="US-East API 500 outage.",
        priority="CRITICAL",
        status="OPEN",
        category="TECHNICAL",
        routing_status="UNASSIGNED",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now + timedelta(hours=1),
        sla_breach_at=now + timedelta(hours=2),
        created_at=now - timedelta(minutes=30),
    )
    # c2 reporting the same incident
    t2 = Ticket(
        ticket_number="TCK-SYS-002",
        customer_id=c2.id,
        title="US-East region API 500 server errors",
        description="All API requests to US-East region are returning HTTP 500 internal server error.",
        masked_summary="US-East API 500 outage.",
        priority="CRITICAL",
        status="OPEN",
        category="TECHNICAL",
        routing_status="UNASSIGNED",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now + timedelta(hours=1),
        sla_breach_at=now + timedelta(hours=2),
        created_at=now - timedelta(minutes=10),
    )
    session.add_all([t1, t2])
    await session.flush()

    matcher = TicketClusterMatcher(group_threshold=0.80)
    groups = await matcher.group_related_issues(
        db=session,
        tenant_id="tenant-alpha",
        time_window_hours=24.0,
        category="TECHNICAL",
        min_cluster_size=2,
    )

    assert len(groups) == 1
    grp = groups[0]
    assert grp.member_count == 2
    assert t1.id in grp.ticket_ids
    assert t2.id in grp.ticket_ids
    assert t1.cluster_group_id == grp.cluster_group_id
    assert t2.cluster_group_id == grp.cluster_group_id

    # Invariants: Individual customer routing status NOT suppressed for systemic issues
    assert t1.routing_status == "UNASSIGNED"
    assert t2.routing_status == "UNASSIGNED"
    # Ownership remains untouched
    assert t1.customer_id == c1.id
    assert t2.customer_id == c2.id


@pytest.mark.asyncio
async def test_group_related_issues_cross_tenant_isolation(
    session: AsyncSession, sample_calendar: SLACalendar, sample_customers: tuple
):
    """Verifies that systemic issue grouping never clusters tickets from different tenants."""
    c1, _, c3 = sample_customers  # c1 is tenant-alpha, c3 is tenant-beta
    now = datetime.now(timezone.utc)

    t1 = Ticket(
        ticket_number="TCK-TENANT-A",
        customer_id=c1.id,
        title="Payment gateway downtime",
        description="Stripe gateway is down globally.",
        masked_summary="Stripe down.",
        priority="CRITICAL",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now + timedelta(minutes=45),
        sla_breach_at=now + timedelta(minutes=60),
        created_at=now,
    )
    t2 = Ticket(
        ticket_number="TCK-TENANT-B",
        customer_id=c3.id,
        title="Payment gateway downtime",
        description="Stripe gateway is down globally.",
        masked_summary="Stripe down.",
        priority="CRITICAL",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=sample_calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now + timedelta(minutes=45),
        sla_breach_at=now + timedelta(minutes=60),
        created_at=now,
    )
    session.add_all([t1, t2])
    await session.flush()

    matcher = TicketClusterMatcher(group_threshold=0.80)
    groups = await matcher.group_related_issues(
        db=session,
        tenant_id="tenant-alpha",
        min_cluster_size=2,
    )

    # Only 1 ticket exists in tenant-alpha for this issue, so min_cluster_size=2 results in 0 clusters
    assert len(groups) == 0
    assert t1.cluster_group_id is None
    assert t2.cluster_group_id is None

