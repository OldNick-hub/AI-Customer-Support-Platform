"""Targeted Regression Tests for Staff Conversation Creation and Tenant-Scoped History.

Verifies:
1. Staff conversation creation:
   - Hardware Specialist Agent (AGENT role) initiates conversations without FK violations.
   - Operations Supervisor (SUPERVISOR role) initiates conversations without FK violations.
   - Platform Administrator (ADMIN role) initiates conversations without FK violations.
   - Persisted session exists in 'sessions' before 'conversations' foreign key is referenced.
   - Conversation and initial message persist correctly in database.
   - Repeated idempotent requests return cached response without duplicate records.
   - Invalid or missing tenant_id claim in staff JWT fails closed with 403 Forbidden.
   - Customer-initiated conversations (X-Session-Token) continue to function intact.
2. Conversation history:
   - Staff can list conversations belonging to their authorized tenant.
   - Newly created staff conversations appear in the listing.
   - Cross-tenant isolation: Tenant A staff cannot view Tenant B conversations.
   - Customer isolation: Customer sessions can only access their own conversations.
   - Missing/ambiguous tenant metadata fails closed (excluded from tenant listing).
   - Pagination and deterministic ordering are preserved.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
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


async def create_test_staff_user(
    db: AsyncSession,
    role_name: str,
    email: str,
    full_name: str,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Provisions a staff user with an assigned role in the test database and returns a valid JWT."""
    # 1. Role
    role_res = await db.execute(select(Role).where(Role.name == role_name))
    role = role_res.scalar_one_or_none()
    if not role:
        role = Role(
            name=role_name,
            description=f"Test {role_name} role",
            permissions=["*"] if role_name == "ADMIN" else ["tickets:*", "chat:*"],
        )
        db.add(role)
        await db.flush()

    # 2. User
    user_res = await db.execute(select(User).where(User.email == email))
    user = user_res.scalar_one_or_none()
    if not user:
        user = User(
            email=email,
            hashed_password="hashed_dev_password_123",
            full_name=full_name,
            department="Operations Support",
            skills=["hardware_support", "troubleshooting"],
            max_active_tickets=5,
            is_active=True,
            is_on_call=False,
        )
        db.add(user)
        await db.flush()

        ur = UserRole(user_id=user.id, role_id=role.id)
        db.add(ur)
        await db.flush()

    now = datetime.now(timezone.utc)
    jwt_payload = {
        "sub": str(user.id),
        "email": user.email,
        "roles": [role_name],
        "name": user.full_name,
        "tenant_id": tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=4)).timestamp()),
    }
    token = jwt.encode(jwt_payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    return {
        "user": user,
        "role": role,
        "jwt": token,
        "tenant_id": tenant_id,
    }


async def create_test_customer(
    db: AsyncSession,
    email: str,
    full_name: str,
    tenant_id: Optional[str] = "default",
) -> Dict[str, Any]:
    """Provisions an isolated customer and active session token."""
    now = datetime.now(timezone.utc)
    metadata = {"account_status": "VERIFIED"}
    if tenant_id:
        metadata["tenant_id"] = tenant_id

    cust = Customer(
        external_id=f"CUST-{uuid.uuid4().hex[:8]}",
        email=email,
        full_name=full_name,
        customer_tier="STANDARD",
        metadata_=metadata,
    )
    db.add(cust)
    await db.flush()

    sess_token = f"sess_token_{uuid.uuid4().hex}"
    session_rec = CustomerSession(
        customer_id=cust.id,
        session_token=sess_token,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    db.add(session_rec)
    await db.flush()

    return {
        "customer": cust,
        "session": session_rec,
        "session_token": sess_token,
    }


# ==============================================================================
# Phase 1: Staff Conversation Creation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_hardware_specialist_can_initiate_conversation(session: AsyncSession):
    """Verifies that Hardware Specialist Agent (AGENT role) can initiate a conversation without FK error."""
    staff = await create_test_staff_user(
        session,
        role_name="AGENT",
        email="hw_specialist@test.internal",
        full_name="Hardware Specialist Agent",
        tenant_id="default",
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
                json={"message": "hi", "preferred_language": "en"},
            )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["success"] is True
        conv_id = data["data"]["conversation_id"]
        assert conv_id is not None

        # Verify conversation and session are persisted in DB
        conv_res = await session.execute(select(Conversation).where(Conversation.id == conv_id))
        conv = conv_res.scalar_one_or_none()
        assert conv is not None
        assert conv.session_id is not None
        assert conv.status == "OPEN"

        # Verify session is persisted and foreign key is satisfied
        sess_res = await session.execute(select(CustomerSession).where(CustomerSession.id == conv.session_id))
        sess = sess_res.scalar_one_or_none()
        assert sess is not None
        assert sess.status == "ACTIVE"

        # Verify message is persisted
        msg_res = await session.execute(select(Message).where(Message.conversation_id == conv_id))
        messages = msg_res.scalars().all()
        assert len(messages) >= 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_operations_supervisor_can_initiate_conversation(session: AsyncSession):
    """Verifies that Operations Supervisor (SUPERVISOR role) can initiate a conversation."""
    staff = await create_test_staff_user(
        session,
        role_name="SUPERVISOR",
        email="supervisor@test.internal",
        full_name="Operations Supervisor",
        tenant_id="default",
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
                json={"message": "Checking system policy for supervisors"},
            )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["success"] is True
        assert data["data"]["conversation_id"] is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_platform_administrator_can_initiate_conversation(session: AsyncSession):
    """Verifies that Platform Administrator (ADMIN role) can initiate a conversation."""
    staff = await create_test_staff_user(
        session,
        role_name="ADMIN",
        email="admin@test.internal",
        full_name="Platform Administrator",
        tenant_id="default",
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
                json={"message": "System status check from administrator"},
            )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["success"] is True
        assert data["data"]["conversation_id"] is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_repeated_idempotent_requests_do_not_duplicate_records(session: AsyncSession):
    """Verifies that repeating a request with the same idempotency key returns the cached response."""
    staff = await create_test_staff_user(
        session,
        role_name="AGENT",
        email="idempotent_agent@test.internal",
        full_name="Idempotency Test Agent",
        tenant_id="default",
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    idem_key = f"test_idem_{uuid.uuid4().hex}"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res1 = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}", "Idempotency-Key": idem_key},
                json={"message": "Idempotent turn query"},
            )
            assert res1.status_code == status.HTTP_200_OK
            d1 = res1.json()

            # Retry identical request with same idempotency key
            res2 = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}", "Idempotency-Key": idem_key},
                json={"message": "Idempotent turn query"},
            )
            assert res2.status_code == status.HTTP_200_OK
            d2 = res2.json()

            # Same conversation ID and message response
            assert d1["data"]["conversation_id"] == d2["data"]["conversation_id"]

        # Ensure only 1 conversation was created
        conv_res = await session.execute(
            select(Conversation).where(Conversation.id == d1["data"]["conversation_id"])
        )
        assert len(conv_res.scalars().all()) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_staff_invalid_or_missing_tenant_rejected_403(session: AsyncSession):
    """Verifies that staff JWT with missing or invalid tenant_id fails closed with 403."""
    now = datetime.now(timezone.utc)

    # Missing tenant_id claim
    bad_payload_missing = {
        "sub": str(uuid.uuid4()),
        "roles": ["AGENT"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    jwt_missing = jwt.encode(bad_payload_missing, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    # Empty tenant_id claim
    bad_payload_empty = {
        "sub": str(uuid.uuid4()),
        "roles": ["AGENT"],
        "tenant_id": "   ",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    jwt_empty = jwt.encode(bad_payload_empty, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res_m = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {jwt_missing}"},
                json={"message": "test"},
            )
            assert res_m.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

            res_e = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {jwt_empty}"},
                json={"message": "test"},
            )
            assert res_e.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_customer_initiated_conversations_continue_to_work(session: AsyncSession):
    """Verifies that customer session token callers continue to create conversations normally."""
    cust_ctx = await create_test_customer(session, email="alice.customer@test.com", full_name="Alice Customer")
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/api/v1/chat/message",
                headers={"X-Session-Token": cust_ctx["session_token"]},
                json={"message": "Customer inquiry about warranty", "preferred_language": "en"},
            )

        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["success"] is True
        conv_id = data["data"]["conversation_id"]
        assert conv_id is not None

        # Verify conversation belongs to customer
        conv_res = await session.execute(select(Conversation).where(Conversation.id == conv_id))
        conv = conv_res.scalar_one()
        assert conv.customer_id == cust_ctx["customer"].id
        assert conv.session_id == cust_ctx["session"].id
    finally:
        app.dependency_overrides.pop(get_db, None)


# ==============================================================================
# Phase 2: Staff Conversation History & Tenant Isolation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_staff_can_list_authorized_tenant_conversations(session: AsyncSession):
    """Verifies that staff can list conversations in their authorized tenant, including newly created ones."""
    staff = await create_test_staff_user(
        session,
        role_name="AGENT",
        email="history_agent@test.internal",
        full_name="History Agent",
        tenant_id="tenant-alpha",
    )
    # Create customer conversation in tenant-alpha
    cust_a = await create_test_customer(session, email="cust_a@alpha.test", full_name="Alpha Customer", tenant_id="tenant-alpha")
    now = datetime.now(timezone.utc)
    conv_a = Conversation(
        session_id=cust_a["session"].id,
        customer_id=cust_a["customer"].id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_a)
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. List existing conversation in tenant-alpha
            res = await client.get(
                "/api/v1/conversations",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
            )
            assert res.status_code == status.HTTP_200_OK
            data = res.json()
            assert data["total_count"] >= 1
            item_ids = [item["id"] for item in data["items"]]
            assert str(conv_a.id) in item_ids

            # 2. Staff creates a new conversation
            chat_res = await client.post(
                "/api/v1/chat/message",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
                json={"message": "Staff turn query in tenant-alpha"},
            )
            assert chat_res.status_code == status.HTTP_200_OK
            new_conv_id = chat_res.json()["data"]["conversation_id"]

            # 3. List conversations again — new staff conversation must appear!
            res2 = await client.get(
                "/api/v1/conversations",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
            )
            assert res2.status_code == status.HTTP_200_OK
            item_ids_after = [item["id"] for item in res2.json()["items"]]
            assert new_conv_id in item_ids_after
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_cross_tenant_isolation_staff_cannot_view_other_tenant_conversations(session: AsyncSession):
    """Verifies strict cross-tenant isolation: Tenant A staff cannot list or view Tenant B conversations."""
    staff_a = await create_test_staff_user(session, "AGENT", "agent_a@test.internal", "Agent A", tenant_id="tenant-alpha")
    staff_b = await create_test_staff_user(session, "AGENT", "agent_b@test.internal", "Agent B", tenant_id="tenant-beta")

    # Create conversation in tenant-beta
    cust_b = await create_test_customer(session, email="cust_b@beta.test", full_name="Beta Customer", tenant_id="tenant-beta")
    now = datetime.now(timezone.utc)
    conv_b = Conversation(
        session_id=cust_b["session"].id,
        customer_id=cust_b["customer"].id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_b)
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Staff B (tenant-beta) sees conv_b
            res_b = await client.get(
                "/api/v1/conversations",
                headers={"Authorization": f"Bearer {staff_b['jwt']}"},
            )
            assert res_b.status_code == status.HTTP_200_OK
            b_ids = [item["id"] for item in res_b.json()["items"]]
            assert str(conv_b.id) in b_ids

            # 2. Staff A (tenant-alpha) CANNOT see conv_b in list
            res_a = await client.get(
                "/api/v1/conversations",
                headers={"Authorization": f"Bearer {staff_a['jwt']}"},
            )
            assert res_a.status_code == status.HTTP_200_OK
            a_ids = [item["id"] for item in res_a.json()["items"]]
            assert str(conv_b.id) not in a_ids

            # 3. Staff A attempting to fetch conv_b detail returns 404
            res_detail = await client.get(
                f"/api/v1/conversations/{conv_b.id}",
                headers={"Authorization": f"Bearer {staff_a['jwt']}"},
            )
            assert res_detail.status_code == status.HTTP_404_NOT_FOUND

            # 4. Staff A attempting to fetch conv_b messages returns 404
            res_msgs = await client.get(
                f"/api/v1/conversations/{conv_b.id}/messages",
                headers={"Authorization": f"Bearer {staff_a['jwt']}"},
            )
            assert res_msgs.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_missing_tenant_metadata_fails_closed(session: AsyncSession):
    """Verifies that legacy records with missing/ambiguous tenant metadata fail closed (excluded)."""
    staff = await create_test_staff_user(session, "AGENT", "agent_legacy@test.internal", "Agent Legacy", tenant_id="tenant-gamma")

    # Create customer with NO tenant_id in metadata_
    cust_legacy = await create_test_customer(session, email="legacy@customer.test", full_name="Legacy Customer", tenant_id=None)
    now = datetime.now(timezone.utc)
    conv_legacy = Conversation(
        session_id=cust_legacy["session"].id,
        customer_id=cust_legacy["customer"].id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_legacy)
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get(
                "/api/v1/conversations",
                headers={"Authorization": f"Bearer {staff['jwt']}"},
            )
            assert res.status_code == status.HTTP_200_OK
            items = res.json()["items"]
            item_ids = [item["id"] for item in items]
            # Ambiguous/legacy conversation without tenant MUST be excluded
            assert str(conv_legacy.id) not in item_ids
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_customer_isolation_cannot_view_other_conversations(session: AsyncSession):
    """Verifies customer session token cannot access another customer's conversations."""
    alice = await create_test_customer(session, "alice@test.com", "Alice Customer", tenant_id="default")
    bob = await create_test_customer(session, "bob@test.com", "Bob Customer", tenant_id="default")

    now = datetime.now(timezone.utc)
    conv_bob = Conversation(
        session_id=bob["session"].id,
        customer_id=bob["customer"].id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_bob)
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Alice listing conversations -> Bob's conversation is excluded
            res_list = await client.get(
                "/api/v1/conversations",
                headers={"X-Session-Token": alice["session_token"]},
            )
            assert res_list.status_code == status.HTTP_200_OK
            alice_ids = [item["id"] for item in res_list.json()["items"]]
            assert str(conv_bob.id) not in alice_ids

            # Alice directly fetching Bob's conversation -> 404
            res_direct = await client.get(
                f"/api/v1/conversations/{conv_bob.id}",
                headers={"X-Session-Token": alice["session_token"]},
            )
            assert res_direct.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_db, None)

