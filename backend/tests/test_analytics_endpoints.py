"""Test Suite for Phase 6: Analytics, Dashboard & Operational Intelligence APIs.

Verifies:
1. GET /api/v1/analytics/overview:
   - Aggregated KPIs (total, open, resolved, escalated, urgent, compliance %).
   - Real priority incident queue sorted by priority_score desc, sla_breach_at asc.
   - Customer data isolation (customers only see aggregations for their own tickets).
   - Staff visibility across all teams and departments.
2. GET /api/v1/analytics/performance:
   - Time-series daily trend bucketing.
   - Categorical SLA breakdown with compliance percentages.
   - Evidence vault status and reconciliation match statistics.
   - Multilingual conversation breakdown.
3. GET /api/v1/analytics/operational-intelligence:
   - Deterministic rule evaluations: capacity threshold, imminent SLA breach, safety alarms.
   - Grounded supporting metrics and navigation routes.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import jwt
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Session as CustomerSession
from app.models.evidence import EvidenceFile, EvidenceVerification
from app.models.ticket import EscalationAudit, SLACalendar, Ticket


def make_staff_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None) -> str:
    """Helper to generate a valid staff JWT Bearer token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id or str(uuid.uuid4()),
        "roles": roles or ["ADMIN"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@pytest_asyncio.fixture(scope="function")
async def seeded_analytics_context(session: AsyncSession) -> Dict[str, Any]:
    """Provisions isolated test data: customers, tickets with various priorities/SLAs, conversations, evidence, and staff."""
    now = datetime.now(timezone.utc)

    # 1. SLA Calendar
    calendar = SLACalendar(
        name=f"ANALYTICS-CAL-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(0, 0, 0),
        work_end_time=time(23, 59, 59),
        work_days=[1, 2, 3, 4, 5, 6, 7],
        holidays=[],
    )
    session.add(calendar)
    await session.flush()

    # 2. Customers
    cust_1 = Customer(
        external_id=f"CUST-ANALYTICS-1-{uuid.uuid4().hex[:6]}",
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Alice Customer",
        customer_tier="STANDARD",
        metadata_={"account_status": "VERIFIED"},
    )
    cust_2 = Customer(
        external_id=f"CUST-ANALYTICS-2-{uuid.uuid4().hex[:6]}",
        email=f"bob-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Bob Enterprise",
        customer_tier="ENTERPRISE",
        metadata_={"account_status": "VERIFIED"},
    )
    session.add_all([cust_1, cust_2])
    await session.flush()

    # 3. Sessions
    sess_token_1 = f"token-analytics-alice-{uuid.uuid4().hex}"
    sess_1 = CustomerSession(
        customer_id=cust_1.id,
        session_token=sess_token_1,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    session.add(sess_1)
    await session.flush()

    # 4. Conversations
    conv_1 = Conversation(
        session_id=sess_1.id,
        customer_id=cust_1.id,
        primary_language="en",
        status="OPEN",
        overall_sentiment_score=-0.75,
        turn_count=4,
        is_escalated=True,
        started_at=now - timedelta(days=2),
        last_message_at=now - timedelta(hours=1),
    )
    conv_2 = Conversation(
        session_id=sess_1.id,
        customer_id=cust_2.id,
        primary_language="hi",
        status="RESOLVED",
        overall_sentiment_score=0.4,
        turn_count=2,
        is_escalated=False,
        started_at=now - timedelta(days=1),
        last_message_at=now - timedelta(hours=5),
    )
    session.add_all([conv_1, conv_2])
    await session.flush()

    # 5. Tickets
    t_critical = Ticket(
        ticket_number=f"TCK-ANALYTICS-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        title="Immediate server outage and database failure",
        description="Critical outage impacting 1000 users",
        masked_summary="Immediate server outage reported.",
        priority="CRITICAL",
        priority_score=4.9,
        status="OPEN",
        category="Technical",
        assigned_team="Technical",
        sla_calendar_id=calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now - timedelta(minutes=15),
        sla_breach_at=now + timedelta(minutes=15),  # Imminent breach!
        sla_warning_sent=True,
        sla_breached=False,
        created_at=now - timedelta(minutes=45),
    )
    t_resolved = Ticket(
        ticket_number=f"TCK-ANALYTICS-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_2.id,
        conversation_id=conv_2.id,
        title="Billing question regarding annual discount",
        description="Customer asked for invoice explanation",
        masked_summary="Billing invoice question.",
        priority="LOW",
        priority_score=1.2,
        status="RESOLVED",
        category="Billing",
        assigned_team="Billing",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now - timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=1),
        sla_warning_sent=False,
        sla_breached=False,
        created_at=now - timedelta(hours=2),
        resolved_at=now - timedelta(hours=1),
    )
    t_breached = Ticket(
        ticket_number=f"TCK-ANALYTICS-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_2.id,
        title="Damaged hardware package received",
        description="Box arrived crushed with broken glass",
        masked_summary="Damaged hardware received.",
        priority="HIGH",
        priority_score=3.8,
        status="ESCALATED",
        category="Logistics",
        assigned_team="Logistics",
        sla_calendar_id=calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now - timedelta(hours=4),
        sla_breach_at=now - timedelta(hours=1),  # Breached!
        sla_warning_sent=True,
        sla_breached=True,
        created_at=now - timedelta(hours=3),
    )
    session.add_all([t_critical, t_resolved, t_breached])
    await session.flush()

    # 6. Escalation Audit (Safety Alarm)
    audit = EscalationAudit(
        ticket_id=t_critical.id,
        conversation_id=conv_1.id,
        trigger_condition="HIGH_RISK_SAFETY",
        previous_state="OPEN",
        new_state="ESCALATED",
        escalation_reason="Severe emergency threat flagged by customer.",
        escalation_payload={"keyword": "outage_catastrophe"},
        triggered_at=now - timedelta(minutes=30),
    )
    session.add(audit)

    # 7. Evidence File & Verification
    ev_file = EvidenceFile(
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        ticket_id=t_critical.id,
        file_name="receipt_outage.pdf",
        storage_uri="/tmp/receipt_outage.pdf",
        mime_type="application/pdf",
        file_size_bytes=45000,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        status="VERIFIED",
        retention_expires_at=now + timedelta(days=30),
        created_at=now - timedelta(hours=2),
    )
    session.add(ev_file)
    await session.flush()

    verification = EvidenceVerification(
        evidence_file_id=ev_file.id,
        conversation_id=conv_1.id,
        claim_text="Customer claims refund of INR 4500",
        overall_match_status="CONFLICT",
        reconciliation_status="CONFLICT",
        overall_confidence=0.88,
        created_at=now - timedelta(hours=1),
    )
    session.add(verification)

    # 8. Staff User
    role_admin = Role(name=f"ADMIN-{uuid.uuid4().hex[:6]}", description="Admin Role", permissions=["*"])
    session.add(role_admin)
    await session.flush()

    staff_user = User(
        email=f"admin-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Admin Analytics Tester",
        department="Operations",
        skills=["analytics"],
        is_active=True,
    )
    session.add(staff_user)
    await session.flush()

    ur = UserRole(user_id=staff_user.id, role_id=role_admin.id)
    session.add(ur)
    await session.commit()

    staff_jwt = make_staff_jwt(roles=["ADMIN"], user_id=str(staff_user.id))

    return {
        "calendar_id": calendar.id,
        "cust_1": cust_1,
        "cust_2": cust_2,
        "sess_token_1": sess_token_1,
        "t_critical": t_critical,
        "t_resolved": t_resolved,
        "t_breached": t_breached,
        "staff_jwt": staff_jwt,
    }


@pytest.mark.asyncio
async def test_overview_dashboard_staff(session: AsyncSession, seeded_analytics_context: Dict[str, Any]):
    """Verifies that staff callers receive full platform overview KPIs and priority queue."""
    ctx = seeded_analytics_context
    app.dependency_overrides[get_db] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/v1/analytics/overview?time_window=30d",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Assert KPIs
        assert data["total_tickets"] >= 3
        assert data["open_tickets"] >= 1
        assert data["resolved_tickets"] >= 1
        assert data["escalated_tickets"] >= 1
        assert data["sla_breached_count"] >= 1
        assert data["sla_warning_count"] >= 1
        assert 0.0 <= data["sla_compliance_pct"] <= 100.0
        assert data["avg_resolution_time_minutes"] > 0

        # Assert Priority Queue (real tickets)
        assert len(data["priority_queue"]) >= 1
        top_incident = data["priority_queue"][0]
        assert top_incident["priority"] == "CRITICAL"
        assert top_incident["priority_score"] >= 4.5
        assert "remaining" in top_incident["sla_remaining"] or "BREACHED" in top_incident["sla_remaining"]

        # Assert distributions
        assert "CRITICAL" in data["priority_distribution"]
        assert "LOW" in data["priority_distribution"]


@pytest.mark.asyncio
async def test_overview_dashboard_customer_isolation(
    session: AsyncSession, seeded_analytics_context: Dict[str, Any]
):
    """Verifies that customer sessions only receive metrics for tickets they own."""
    ctx = seeded_analytics_context
    app.dependency_overrides[get_db] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/v1/analytics/overview?time_window=30d",
            headers={"X-Session-Token": ctx["sess_token_1"]},
        )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Customer 1 owns only 1 ticket (t_critical)
        assert data["total_tickets"] == 1
        assert data["open_tickets"] == 1
        assert data["resolved_tickets"] == 0
        assert data["sla_breached_count"] == 0
        assert data["priority_distribution"]["CRITICAL"] == 1
        assert data["priority_distribution"]["LOW"] == 0  # Belongs to Customer 2!


@pytest.mark.asyncio
async def test_performance_analytics(session: AsyncSession, seeded_analytics_context: Dict[str, Any]):
    """Verifies performance trends, SLA breakdowns by category/priority, and multilingual stats."""
    ctx = seeded_analytics_context
    app.dependency_overrides[get_db] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/v1/analytics/performance?time_window=7d",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Assert Daily Trends
        assert len(data["ticket_trends"]) >= 7
        assert any(t["created"] > 0 for t in data["ticket_trends"])

        # Assert Category & Priority Distributions
        assert len(data["category_distribution"]) > 0
        assert len(data["sla_by_category"]) > 0
        assert len(data["sla_by_priority"]) > 0

        # Assert Multilingual Distribution
        assert len(data["language_distribution"]) >= 2
        lang_codes = [l["code"] for l in data["language_distribution"]]
        assert "en" in lang_codes
        assert "hi" in lang_codes

        # Assert Evidence Stats
        assert data["evidence_analytics"]["total_uploaded"] >= 1
        assert data["evidence_analytics"]["verified_count"] >= 1


@pytest.mark.asyncio
async def test_operational_intelligence_rules(session: AsyncSession, seeded_analytics_context: Dict[str, Any]):
    """Verifies that deterministic operational intelligence rules trigger on live conditions."""
    ctx = seeded_analytics_context
    app.dependency_overrides[get_db] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/v1/analytics/operational-intelligence?time_window=30d",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        assert len(data["insights"]) >= 2
        rule_names = [i["rule_name"] for i in data["insights"]]

        # 1. Imminent SLA breach rule should trigger because t_critical breaches within 15m!
        assert "RULE_SLA_IMMINENT_BREACH_WINDOW" in rule_names

        # 2. Safety alarm rule should trigger because EscalationAudit had HIGH_RISK_SAFETY!
        assert "RULE_HIGH_RISK_SAFETY_ALARMS" in rule_names

        # 3. Evidence conflict rule should trigger because EvidenceVerification had CONFLICT!
        assert "RULE_RECONCILIATION_CONFLICT_SURGE" in rule_names

        # Verify insight metadata
        for item in data["insights"]:
            assert item["severity"] in ("CRITICAL", "WARNING", "INFO")
            assert item["target_route"].startswith("/")
            assert len(item["supporting_metric"]) > 0
            assert len(item["suggested_action"]) > 0


@pytest.mark.asyncio
async def test_analytics_tenant_isolation_and_sla_integrity(session: AsyncSession):
    """Verifies that analytics and overview dashboard enforce tenant isolation and authoritative SLA calculations."""
    now = datetime.now(timezone.utc)
    app.dependency_overrides[get_db] = lambda: session

    # 1. Calendar
    cal = SLACalendar(
        name=f"ISO-CAL-{uuid.uuid4().hex[:6]}",
        timezone="UTC",
        work_start_time=time(0, 0, 0),
        work_end_time=time(23, 59, 59),
        work_days=[1, 2, 3, 4, 5, 6, 7],
        holidays=[],
    )
    session.add(cal)
    await session.flush()

    # 2. Two customers belonging to different tenants
    cust_a = Customer(
        external_id=f"CUST-A-{uuid.uuid4().hex[:6]}",
        email=f"tenantA-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Tenant A Customer",
        metadata_={"tenant_id": "tenant-alpha-123"},
    )
    cust_b = Customer(
        external_id=f"CUST-B-{uuid.uuid4().hex[:6]}",
        email=f"tenantB-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Tenant B Customer",
        metadata_={"tenant_id": "tenant-beta-456"},
    )
    session.add_all([cust_a, cust_b])
    await session.flush()

    # 3. Create ticket for Customer A (Breached SLA)
    tck_a = Ticket(
        ticket_number=f"TCK-A-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_a.id,
        title="Urgent Database Outage",
        description="Core database failing transactions",
        masked_summary="Urgent DB outage",
        priority="CRITICAL",
        priority_score=4.9,
        status="OPEN",
        category="Database",
        assigned_team="Technical",
        sla_calendar_id=cal.id,
        sla_target_minutes=60,
        sla_warning_at=now - timedelta(minutes=40),
        sla_breach_at=now - timedelta(minutes=15),  # 15m overdue!
        sla_warning_sent=True,
        sla_breached=True,
        created_at=now - timedelta(minutes=75),
    )
    session.add(tck_a)
    await session.commit()

    # 4. Create Staff Users in database
    staff_user_a = User(
        email=f"staff-a-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Staff A Tester",
        department="Technical",
        is_active=True,
    )
    staff_user_b = User(
        email=f"staff-b-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Staff B Tester",
        department="Technical",
        is_active=True,
    )
    session.add_all([staff_user_a, staff_user_b])
    await session.commit()

    # Create Staff JWT tokens scoped to each tenant
    def make_tenant_jwt(user_id: str, tenant_id: str) -> str:
        payload = {
            "sub": user_id,
            "roles": ["SUPPORT_AGENT"],
            "tenant_id": tenant_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=2)).timestamp()),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    jwt_a = make_tenant_jwt(str(staff_user_a.id), "tenant-alpha-123")
    jwt_b = make_tenant_jwt(str(staff_user_b.id), "tenant-beta-456")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Tenant A staff requests overview
        res_a = await client.get(
            "/api/v1/analytics/overview?time_window=today",
            headers={"Authorization": f"Bearer {jwt_a}"},
        )
        assert res_a.status_code == status.HTTP_200_OK
        data_a = res_a.json()

        # Should find Tenant A's ticket
        assert data_a["total_tickets"] == 1
        assert len(data_a["priority_queue"]) == 1
        incident = data_a["priority_queue"][0]
        assert incident["ticket_id"] == str(tck_a.id)
        assert incident["is_breached"] is True
        assert incident["is_overdue"] is True
        assert incident["sla_target_minutes"] == 60
        assert "overdue" in (incident["overdue_duration"] or "")

        # Tenant B staff requests overview: should see 0 tickets (clean empty state)
        res_b = await client.get(
            "/api/v1/analytics/overview?time_window=today",
            headers={"Authorization": f"Bearer {jwt_b}"},
        )
        assert res_b.status_code == status.HTTP_200_OK
        data_b = res_b.json()
        assert data_b["total_tickets"] == 0
        assert data_b["open_tickets"] == 0
        assert len(data_b["priority_queue"]) == 0

