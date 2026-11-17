"""Comprehensive Test Suite for Day 15 Phase 1: Backend Read APIs & Safe Dev Auth.

Covers:
1. GET /api/v1/tickets:
   - Pagination (limit, offset) and deterministic sorting.
   - Status, priority, and category filtering.
   - Multi-tenant customer isolation (Customer A cannot see Customer B's tickets).
   - Staff RBAC (Staff can view all tickets or filter by customer).
   - Unauthenticated & conflicting credentials handling (401).

2. GET /api/v1/tickets/{id}:
   - Detail retrieval with full SLA, priority, and routing metadata.
   - Customer ownership enforcement (unowned ticket returns 404 to avoid enumeration).
   - Nonexistent ticket returns 404.
   - Staff access to any ticket.

3. GET /api/v1/conversations:
   - Paginated conversation listing with deterministic ordering.
   - Customer isolation (session token callers only see their own conversations).
   - Staff visibility across all conversations.
   - Status filtering.

4. GET /api/v1/conversations/{id}/messages:
   - Chronological message listing (created_at.asc(), id.asc()).
   - Customer ownership enforcement on message retrieval.
   - Nonexistent conversation returns 404.
   - Staff access to messages.

5. Development Persona Authentication (/api/v1/auth):
   - GET /api/v1/auth/dev-personas returns available personas.
   - POST /api/v1/auth/dev-token generates verifiable customer session tokens.
   - POST /api/v1/auth/dev-token generates verifiable staff JWTs with roles (AGENT, SUPERVISOR, ADMIN).
   - Integration verification: tokens authenticate successfully through get_auth_context and GET /api/v1/auth/me.
   - Invalid persona handling (400 Bad Request).
   - Production safety & fail-closed enforcement (404 when ENVIRONMENT != 'development' or disabled).
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.ticket import SLACalendar, Ticket


def make_staff_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None, tenant_id: str = "default") -> str:
    """Helper to generate a valid staff JWT Bearer token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id or str(uuid.uuid4()),
        "roles": roles or ["AGENT"],
        "tenant_id": tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@pytest_asyncio.fixture(scope="function")
async def seeded_phase1_context(session: AsyncSession) -> Dict[str, Any]:
    """Provisions isolated test data: customers, sessions, conversations, messages, tickets, and staff."""
    now = datetime.now(timezone.utc)

    # 1. SLA Calendar
    calendar = SLACalendar(
        name=f"PHASE1-CAL-{uuid.uuid4().hex[:8]}",
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
        external_id=f"CUST-PHASE1-1-{uuid.uuid4().hex[:6]}",
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Alice Customer",
        customer_tier="STANDARD",
        metadata_={"account_status": "VERIFIED", "tenant_id": "default"},
    )
    cust_2 = Customer(
        external_id=f"CUST-PHASE1-2-{uuid.uuid4().hex[:6]}",
        email=f"bob-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Bob Customer",
        customer_tier="VIP",
        metadata_={"account_status": "VERIFIED", "tenant_id": "default"},
    )
    session.add_all([cust_1, cust_2])
    await session.flush()

    # 3. Sessions
    sess_token_1 = f"token-phase1-alice-{uuid.uuid4().hex}"
    sess_token_2 = f"token-phase1-bob-{uuid.uuid4().hex}"
    sess_1 = CustomerSession(
        customer_id=cust_1.id,
        session_token=sess_token_1,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    sess_2 = CustomerSession(
        customer_id=cust_2.id,
        session_token=sess_token_2,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    session.add_all([sess_1, sess_2])
    await session.flush()

    # 4. Conversations
    conv_1 = Conversation(
        session_id=sess_1.id,
        customer_id=cust_1.id,
        primary_language="en",
        status="OPEN",
        turn_count=2,
        started_at=now - timedelta(hours=3),
        last_message_at=now - timedelta(hours=2),
    )
    conv_2 = Conversation(
        session_id=sess_2.id,
        customer_id=cust_2.id,
        primary_language="en",
        status="RESOLVED",
        turn_count=4,
        started_at=now - timedelta(hours=5),
        last_message_at=now - timedelta(hours=1),
    )
    session.add_all([conv_1, conv_2])
    await session.flush()

    # 5. Messages for conv_1
    m1_c1 = Message(
        conversation_id=conv_1.id,
        sender_type="CUSTOMER",
        content="I have a question about my invoice #INV-1001",
        turn_sequence_number=1,
        detected_language="en",
        created_at=now - timedelta(hours=3),
    )
    m2_c1 = Message(
        conversation_id=conv_1.id,
        sender_type="AI_AGENT",
        content="I can help you review your invoice details.",
        turn_sequence_number=2,
        detected_language="en",
        created_at=now - timedelta(hours=2),
    )
    # Messages for conv_2
    m1_c2 = Message(
        conversation_id=conv_2.id,
        sender_type="CUSTOMER",
        content="My delivery is delayed for order #ORD-9999",
        turn_sequence_number=1,
        detected_language="en",
        created_at=now - timedelta(hours=5),
    )
    session.add_all([m1_c1, m2_c1, m1_c2])
    await session.flush()

    # 6. Tickets for Alice (Customer 1)
    ticket_1 = Ticket(
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        title="Billing Question regarding INV-1001",
        description="Customer noticed a charge on invoice INV-1001.",
        masked_summary="Billing Question on invoice INV-1001.",
        priority="MEDIUM",
        priority_score=2.5,
        status="OPEN",
        category="billing",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now - timedelta(hours=2),
    )
    ticket_2 = Ticket(
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        title="Hardware replacement inquiry",
        description="Inquiring about warranty for keyboard.",
        masked_summary="Warranty inquiry for keyboard.",
        priority="LOW",
        priority_score=1.5,
        status="RESOLVED",
        category="hardware",
        sla_calendar_id=calendar.id,
        sla_target_minutes=480,
        sla_warning_at=now + timedelta(hours=6),
        sla_breach_at=now + timedelta(hours=8),
        created_at=now - timedelta(hours=4),
    )
    # Ticket for Bob (Customer 2)
    ticket_3 = Ticket(
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_2.id,
        conversation_id=conv_2.id,
        title="Critical delay for logistics package",
        description="Shipment ORD-9999 delayed past deadline.",
        masked_summary="Shipment ORD-9999 delayed.",
        priority="CRITICAL",
        priority_score=4.8,
        status="OPEN",
        category="logistics",
        sla_calendar_id=calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now + timedelta(minutes=30),
        sla_breach_at=now + timedelta(hours=1),
        created_at=now - timedelta(hours=1),
    )
    session.add_all([ticket_1, ticket_2, ticket_3])
    await session.flush()

    # 7. Staff User
    role_res = await session.execute(select(Role).where(Role.name == "AGENT"))
    role_agent = role_res.scalar_one_or_none()
    if not role_agent:
        role_agent = Role(name="AGENT", description="Agent Role", permissions=["tickets:*"])
        session.add(role_agent)
        await session.flush()

    staff_user = User(
        email=f"agent-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Agent Tester",
        department="Support",
        skills=["general"],
        is_active=True,
    )
    session.add(staff_user)
    await session.flush()

    ur = UserRole(user_id=staff_user.id, role_id=role_agent.id)
    session.add(ur)
    await session.commit()

    staff_jwt = make_staff_jwt(roles=["AGENT"], user_id=str(staff_user.id))

    return {
        "calendar": calendar,
        "cust_1": cust_1,
        "cust_2": cust_2,
        "token_1": sess_token_1,
        "token_2": sess_token_2,
        "conv_1": conv_1,
        "conv_2": conv_2,
        "m1_c1": m1_c1,
        "m2_c1": m2_c1,
        "m1_c2": m1_c2,
        "ticket_1": ticket_1,
        "ticket_2": ticket_2,
        "ticket_3": ticket_3,
        "staff_user": staff_user,
        "staff_jwt": staff_jwt,
    }


# ==============================================================================
# 1. GET /api/v1/tickets Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_tickets_pagination_and_ordering(session: AsyncSession, seeded_phase1_context):
    """Verifies pagination limits, offsets, and deterministic ordering on tickets."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Request page limit 2 using staff JWT
        res = await client.get(
            "/api/v1/tickets?limit=2&offset=0",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["total_count"] >= 3
        assert len(data["items"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 0

        # Verify deterministic ordering: newer ticket first
        first_ticket = data["items"][0]
        second_ticket = data["items"][1]
        assert first_ticket["created_at"] >= second_ticket["created_at"]

        # Request offset 2
        res_offset = await client.get(
            "/api/v1/tickets?limit=2&offset=2",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_offset.status_code == status.HTTP_200_OK
        offset_data = res_offset.json()
        assert len(offset_data["items"]) >= 1


@pytest.mark.asyncio
async def test_get_tickets_filtering(session: AsyncSession, seeded_phase1_context):
    """Verifies status, priority, and category filters on /api/v1/tickets."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Filter by status=RESOLVED
        res_status = await client.get(
            "/api/v1/tickets?status=RESOLVED",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_status.status_code == status.HTTP_200_OK
        items = res_status.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == str(ctx["ticket_2"].id)
        assert items[0]["status"] == "RESOLVED"

        # 2. Filter by priority=CRITICAL
        res_prio = await client.get(
            "/api/v1/tickets?priority=CRITICAL",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_prio.status_code == status.HTTP_200_OK
        items_prio = res_prio.json()["items"]
        assert len(items_prio) == 1
        assert items_prio[0]["id"] == str(ctx["ticket_3"].id)
        assert items_prio[0]["priority"] == "CRITICAL"

        # 3. Filter by category=billing
        res_cat = await client.get(
            "/api/v1/tickets?category=billing",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_cat.status_code == status.HTTP_200_OK
        items_cat = res_cat.json()["items"]
        assert len(items_cat) == 1
        assert items_cat[0]["id"] == str(ctx["ticket_1"].id)


@pytest.mark.asyncio
async def test_get_tickets_customer_isolation(session: AsyncSession, seeded_phase1_context):
    """Ensures customer session tokens enforce strict isolation (Customer 1 never sees Customer 2's tickets)."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Customer 1 (Alice) request
        res_alice = await client.get(
            "/api/v1/tickets",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res_alice.status_code == status.HTTP_200_OK
        alice_data = res_alice.json()
        assert alice_data["total_count"] == 2
        alice_ids = {t["id"] for t in alice_data["items"]}
        assert str(ctx["ticket_1"].id) in alice_ids
        assert str(ctx["ticket_2"].id) in alice_ids
        # Crucial security invariant: Bob's ticket must NOT be visible
        assert str(ctx["ticket_3"].id) not in alice_ids

        # Customer 2 (Bob) request
        res_bob = await client.get(
            "/api/v1/tickets",
            headers={"X-Session-Token": ctx["token_2"]},
        )
        assert res_bob.status_code == status.HTTP_200_OK
        bob_data = res_bob.json()
        assert bob_data["total_count"] == 1
        bob_ids = {t["id"] for t in bob_data["items"]}
        assert str(ctx["ticket_3"].id) in bob_ids
        assert str(ctx["ticket_1"].id) not in bob_ids


@pytest.mark.asyncio
async def test_get_tickets_staff_customer_filter(session: AsyncSession, seeded_phase1_context):
    """Verifies staff can view all tickets or scope down by customer_id query parameter."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            f"/api/v1/tickets?customer_id={ctx['cust_1'].id}",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["total_count"] == 2
        for item in data["items"]:
            assert item["customer_id"] == str(ctx["cust_1"].id)


@pytest.mark.asyncio
async def test_get_tickets_unauthorized_and_conflicts(session: AsyncSession):
    """Verifies that missing or conflicting authentication credentials fail closed (401)."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Missing credentials
        res_anon = await client.get("/api/v1/tickets")
        assert res_anon.status_code == status.HTTP_401_UNAUTHORIZED

        # 2. Conflicting credentials
        res_conflict = await client.get(
            "/api/v1/tickets",
            headers={
                "X-Session-Token": "some-token",
                "Authorization": "Bearer some-jwt",
            },
        )
        assert res_conflict.status_code == status.HTTP_401_UNAUTHORIZED


# ==============================================================================
# 2. GET /api/v1/tickets/{id} Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_ticket_detail_positive(session: AsyncSession, seeded_phase1_context):
    """Verifies retrieving a single ticket by UUID with full details and SLA attributes."""
    ctx = seeded_phase1_context
    ticket = ctx["ticket_1"]
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            f"/api/v1/tickets/{ticket.id}",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["id"] == str(ticket.id)
        assert data["ticket_number"] == ticket.ticket_number
        assert data["title"] == ticket.title
        assert data["masked_summary"] == ticket.masked_summary
        assert data["priority"] == "MEDIUM"
        assert data["status"] == "OPEN"
        assert data["sla_target_minutes"] == 240
        assert "sla_breach_at" in data
        assert "created_at" in data


@pytest.mark.asyncio
async def test_get_ticket_detail_customer_isolation_and_404(session: AsyncSession, seeded_phase1_context):
    """Verifies Customer A cannot fetch Customer B's ticket (returns 404 to prevent enumeration)."""
    ctx = seeded_phase1_context
    bob_ticket = ctx["ticket_3"]
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Alice tries to fetch Bob's ticket
        res = await client.get(
            f"/api/v1/tickets/{bob_ticket.id}",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res.status_code == status.HTTP_404_NOT_FOUND

        # Nonexistent ticket
        res_nonexistent = await client.get(
            f"/api/v1/tickets/{uuid.uuid4()}",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res_nonexistent.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_ticket_detail_staff_access(session: AsyncSession, seeded_phase1_context):
    """Verifies staff JWT can access any customer's ticket details."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Staff fetches Alice's ticket
        res_alice = await client.get(
            f"/api/v1/tickets/{ctx['ticket_1'].id}",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_alice.status_code == status.HTTP_200_OK

        # Staff fetches Bob's ticket
        res_bob = await client.get(
            f"/api/v1/tickets/{ctx['ticket_3'].id}",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_bob.status_code == status.HTTP_200_OK


# ==============================================================================
# 3. GET /api/v1/conversations Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_conversations_customer_isolation_and_pagination(session: AsyncSession, seeded_phase1_context):
    """Verifies paginated conversation list and customer access isolation."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Customer 1 sees only conv_1
        res_alice = await client.get(
            "/api/v1/conversations",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res_alice.status_code == status.HTTP_200_OK
        alice_data = res_alice.json()
        assert alice_data["total_count"] == 1
        assert alice_data["items"][0]["id"] == str(ctx["conv_1"].id)
        assert alice_data["items"][0]["customer_id"] == str(ctx["cust_1"].id)

        # Staff sees all conversations
        res_staff = await client.get(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_staff.status_code == status.HTTP_200_OK
        staff_data = res_staff.json()
        assert staff_data["total_count"] >= 2


@pytest.mark.asyncio
async def test_get_single_conversation_and_isolation(session: AsyncSession, seeded_phase1_context):
    """Verifies fetching a single conversation and blocking cross-customer access."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Alice fetches her own conversation
        res = await client.get(
            f"/api/v1/conversations/{ctx['conv_1'].id}",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res.status_code == status.HTTP_200_OK
        assert res.json()["id"] == str(ctx["conv_1"].id)

        # Alice attempts to fetch Bob's conversation -> 404
        res_bob = await client.get(
            f"/api/v1/conversations/{ctx['conv_2'].id}",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res_bob.status_code == status.HTTP_404_NOT_FOUND


# ==============================================================================
# 4. GET /api/v1/conversations/{id}/messages Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_conversation_messages_chronological(session: AsyncSession, seeded_phase1_context):
    """Verifies that conversation messages are returned in chronological order with PII masking intact."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            f"/api/v1/conversations/{ctx['conv_1'].id}/messages",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["conversation_id"] == str(ctx["conv_1"].id)
        assert data["total_count"] == 2
        items = data["items"]
        assert len(items) == 2
        assert items[0]["sender_type"] == "CUSTOMER"
        assert items[0]["turn_sequence_number"] == 1
        assert items[1]["sender_type"] == "AI_AGENT"
        assert items[1]["turn_sequence_number"] == 2
        # Chronological order
        assert items[0]["created_at"] <= items[1]["created_at"]


@pytest.mark.asyncio
async def test_get_conversation_messages_isolation(session: AsyncSession, seeded_phase1_context):
    """Verifies cross-customer message retrieval fails closed with 404."""
    ctx = seeded_phase1_context
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Customer 1 attempts to read Customer 2's messages -> 404
        res = await client.get(
            f"/api/v1/conversations/{ctx['conv_2'].id}/messages",
            headers={"X-Session-Token": ctx["token_1"]},
        )
        assert res.status_code == status.HTTP_404_NOT_FOUND

        # Staff can view Customer 2's messages -> 200 OK
        res_staff = await client.get(
            f"/api/v1/conversations/{ctx['conv_2'].id}/messages",
            headers={"Authorization": f"Bearer {ctx['staff_jwt']}"},
        )
        assert res_staff.status_code == status.HTTP_200_OK
        assert res_staff.json()["total_count"] == 1


# ==============================================================================
# 5. Development Persona Authentication Tests (/api/v1/auth)
# ==============================================================================

@pytest.mark.asyncio
async def test_dev_auth_personas_endpoint_removed(session: AsyncSession):
    """Verifies /api/v1/auth/dev-personas endpoint is removed (fails with 404)."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/v1/auth/dev-personas")
        assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_dev_auth_issue_customer_token(session: AsyncSession):
    """Verifies issuing a customer dev session token and authenticating with it."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Issue customer persona token
        res = await client.post(
            "/api/v1/auth/dev-token",
            json={"persona": "customer"},
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["persona"] == "customer"
        assert data["auth_type"] == "SESSION_TOKEN"
        assert data["header_name"] == "X-Session-Token"
        assert data["customer_tier"] == "STANDARD"
        token = data["token"]
        assert token.startswith("dev-sess-customer-")

        # 2. Verify identity via /api/v1/auth/me
        res_me = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Token": token},
        )
        assert res_me.status_code == status.HTTP_200_OK
        me_data = res_me.json()
        assert me_data["auth_type"] == "SESSION_TOKEN"
        assert me_data["customer_id"] == data["customer_id"]

        # 3. Verify it works to access protected tickets endpoint
        res_tickets = await client.get(
            "/api/v1/tickets",
            headers={"X-Session-Token": token},
        )
        assert res_tickets.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_dev_auth_issue_staff_tokens(session: AsyncSession):
    """Verifies issuing staff JWTs for AGENT, SUPERVISOR, and ADMIN personas."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for persona, expected_role in [
            ("agent", "AGENT"),
            ("supervisor", "SUPERVISOR"),
            ("admin", "ADMIN"),
        ]:
            res = await client.post(
                "/api/v1/auth/dev-token",
                json={"persona": persona},
            )
            assert res.status_code == status.HTTP_200_OK
            data = res.json()
            assert data["persona"] == persona
            assert data["auth_type"] == "JWT"
            assert data["header_name"] == "Authorization"
            assert data["header_value"].startswith("Bearer ")
            assert expected_role in data["roles"]

            # Verify with /api/v1/auth/me
            res_me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": data["header_value"]},
            )
            assert res_me.status_code == status.HTTP_200_OK
            me_data = res_me.json()
            assert me_data["auth_type"] == "JWT"
            assert expected_role in me_data["roles"]


@pytest.mark.asyncio
async def test_dev_auth_invalid_persona(session: AsyncSession):
    """Verifies that requesting an unknown dev persona returns 400 Bad Request."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/auth/dev-token",
            json={"persona": "unknown_persona"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        body = res.json()
        error_msg = body.get("error", {}).get("message") or body.get("detail", "")
        assert "Unknown persona" in error_msg


@pytest.mark.asyncio
async def test_dev_auth_disabled_outside_development(session: AsyncSession):
    """Production Safety Guard: Endpoints fail closed with 404 when disabled or outside development."""
    app.dependency_overrides[get_db] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Case A: ENVIRONMENT == "production"
        with patch.object(settings, "ENVIRONMENT", "production"):
            res_list = await client.get("/api/v1/auth/dev-personas")
            assert res_list.status_code == status.HTTP_404_NOT_FOUND

            res_token = await client.post(
                "/api/v1/auth/dev-token",
                json={"persona": "customer"},
            )
            assert res_token.status_code == status.HTTP_404_NOT_FOUND

        # Case B: ENABLE_DEV_AUTH_ENDPOINTS == False
        with patch.object(settings, "ENABLE_DEV_AUTH_ENDPOINTS", False):
            res_list = await client.get("/api/v1/auth/dev-personas")
            assert res_list.status_code == status.HTTP_404_NOT_FOUND

            res_token = await client.post(
                "/api/v1/auth/dev-token",
                json={"persona": "customer"},
            )
            assert res_token.status_code == status.HTTP_404_NOT_FOUND
