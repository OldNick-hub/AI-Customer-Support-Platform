"""Comprehensive Adversarial Security Test Suite for Authentication & Multi-Tenant Isolation.

Verifies remediation of:
1. P0 (Critical): Broken Object Level Authorization (BOLA) in chat turn conversation adoption.
2. P1 (High): Staff cross-tenant support ticket exposure in ticket listings and detail views.
3. P2 (Medium): Evidence authorization fall-through for unlinked sessions and missing ownership.
"""

from datetime import datetime, time, timedelta, timezone
from typing import AsyncGenerator
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, status
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ResolvedAuthContext
from app.api.v1.conversations import get_conversation, get_conversation_messages, list_conversations
from app.api.v1.tickets import get_ticket_detail, list_tickets
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.evidence import EvidenceFile
from app.models.ticket import SLACalendar, Ticket
from app.modules.evidence.service import EvidenceAuthorizationError, EvidenceService
from app.modules.multilingual.lifecycle_manager import (
    CrossCustomerRestorationError,
    ConversationLifecycleManager,
)
from app.modules.multilingual.orchestrator import MultilingualOrchestrator


@pytest_asyncio.fixture
async def setup_two_tenants(session: AsyncSession):
    """Provisions two separate tenants (tenant-alpha and tenant-beta) with customers, sessions, conversations, and tickets."""
    now = datetime.now(timezone.utc)

    # SLA Calendar
    calendar = SLACalendar(
        name=f"CALENDAR-SEC-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(9, 0, 0),
        work_end_time=time(17, 0, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=[],
    )
    session.add(calendar)
    await session.flush()

    # Customer A (Tenant Alpha)
    cust_a = Customer(
        id=uuid.uuid4(),
        email="alice@tenant-alpha.com",
        full_name="Alice Alpha",
        customer_tier="STANDARD",
        metadata_={"tenant_id": "tenant-alpha"},
        created_at=now,
    )
    # Customer B (Tenant Beta)
    cust_b = Customer(
        id=uuid.uuid4(),
        email="bob@tenant-beta.com",
        full_name="Bob Beta",
        customer_tier="STANDARD",
        metadata_={"tenant_id": "tenant-beta"},
        created_at=now,
    )
    session.add_all([cust_a, cust_b])
    await session.flush()

    # Sessions
    sess_a = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_a.id,
        session_token=f"sess-a-{uuid.uuid4().hex}",
        status="ACTIVE",
        expires_at=now + timedelta(days=7),
        last_activity_at=now,
        created_at=now,
    )
    sess_b = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_b.id,
        session_token=f"sess-b-{uuid.uuid4().hex}",
        status="ACTIVE",
        expires_at=now + timedelta(days=7),
        last_activity_at=now,
        created_at=now,
    )
    session.add_all([sess_a, sess_b])
    await session.flush()

    # Conversations
    conv_a = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_a.id,
        session_id=sess_a.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    conv_b = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_b.id,
        session_id=sess_b.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add_all([conv_a, conv_b])
    await session.flush()

    # Initial message in Conv B (Victim)
    msg_b = Message(
        id=uuid.uuid4(),
        conversation_id=conv_b.id,
        sender_type="CUSTOMER",
        content="Confidential financial order inquiry #99901 for Customer B",
        detected_language="en",
        created_at=now - timedelta(minutes=5),
    )
    session.add(msg_b)

    # Tickets
    ticket_a = Ticket(
        ticket_number=f"TCK-ALPHA-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_a.id,
        title="Alpha Ticket",
        description="Confidential billing issue for Alpha",
        masked_summary="Billing issue Alpha",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    ticket_b = Ticket(
        ticket_number=f"TCK-BETA-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_b.id,
        title="Beta Ticket",
        description="Confidential secret issue for Beta",
        masked_summary="Secret issue Beta",
        priority="HIGH",
        status="OPEN",
        category="SECURITY",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=3),
        sla_breach_at=now + timedelta(hours=4),
        created_at=now,
    )
    session.add_all([ticket_a, ticket_b])
    await session.commit()

    return {
        "cust_a": cust_a,
        "cust_b": cust_b,
        "sess_a": sess_a,
        "sess_b": sess_b,
        "conv_a": conv_a,
        "conv_b": conv_b,
        "ticket_a": ticket_a,
        "ticket_b": ticket_b,
    }


# ==============================================================================
# P0 BOLA Chat Turn Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_customer_cannot_adopt_foreign_conversation_chat_turn(
    session: AsyncSession, setup_two_tenants: dict
):
    """Verifies Customer A cannot submit a chat turn adopting Customer B's conversation (fails closed 404)."""
    ctx = setup_two_tenants
    cust_a = ctx["cust_a"]
    sess_a = ctx["sess_a"]
    conv_b = ctx["conv_b"]

    orchestrator = MultilingualOrchestrator()
    auth_ctx = ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        customer_id=str(cust_a.id),
        session_id=str(sess_a.id),
        roles=[],
    )

    with pytest.raises(HTTPException) as excinfo:
        await orchestrator.process_chat_turn(
            session=session,
            customer_id=cust_a.id,
            session_id=sess_a.id,
            message="Attacker Customer A attempting unauthorized conversation adoption",
            conversation_id=conv_b.id,
            auth_context=auth_ctx,
        )

    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_customer_cannot_retrieve_foreign_conversation_history(
    session: AsyncSession, setup_two_tenants: dict
):
    """Verifies Customer A cannot load Customer B's conversation history via lifecycle manager."""
    ctx = setup_two_tenants
    cust_a = ctx["cust_a"]
    sess_a = ctx["sess_a"]
    conv_b = ctx["conv_b"]

    lifecycle_mgr = ConversationLifecycleManager()

    with pytest.raises(CrossCustomerRestorationError) as excinfo:
        await lifecycle_mgr.get_recent_history(
            db=session,
            session_id=sess_a.id,
            authenticated_customer_id=str(cust_a.id),
            conversation_id=conv_b.id,
        )

    assert "forbidden" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_rejected_bola_turn_does_not_mutate_foreign_conversation(
    session: AsyncSession, setup_two_tenants: dict
):
    """Verifies that an unauthorized BOLA attempt does not persist any messages to the victim's conversation."""
    ctx = setup_two_tenants
    cust_a = ctx["cust_a"]
    sess_a = ctx["sess_a"]
    conv_b = ctx["conv_b"]

    orchestrator = MultilingualOrchestrator()
    auth_ctx = ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        customer_id=str(cust_a.id),
        session_id=str(sess_a.id),
        roles=[],
    )

    attacker_text = "MALICIOUS_INJECTION_TOKEN_BOLA_PAYLOAD"
    try:
        await orchestrator.process_chat_turn(
            session=session,
            customer_id=cust_a.id,
            session_id=sess_a.id,
            message=attacker_text,
            conversation_id=conv_b.id,
            auth_context=auth_ctx,
        )
    except HTTPException:
        pass

    # Verify Victim Conversation B was not mutated
    res = await session.execute(
        select(Message).where(Message.conversation_id == conv_b.id)
    )
    messages = res.scalars().all()
    assert len(messages) == 1
    assert attacker_text not in messages[0].content


# ==============================================================================
# Phase 4.3 — 16 Required Adversarial Security Tests
# ==============================================================================

# 1. Staff JWT without tenant ID
@pytest.mark.asyncio
async def test_01_staff_jwt_without_tenant_id_rejected(session: AsyncSession, setup_two_tenants: dict):
    auth_no_tenant = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@support.com",
        roles=["AGENT"],
        jwt_claims={},
    )
    with pytest.raises(HTTPException) as exc:
        await list_tickets(db=session, auth=auth_no_tenant)
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert "tenant_id" in exc.value.detail


# 2. Staff JWT with empty tenant ID
@pytest.mark.asyncio
async def test_02_staff_jwt_with_empty_tenant_id_rejected(session: AsyncSession, setup_two_tenants: dict):
    auth_empty_tenant = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@support.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": ""},
    )
    with pytest.raises(HTTPException) as exc:
        await list_tickets(db=session, auth=auth_empty_tenant)
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert "tenant_id" in exc.value.detail


# 3. Staff JWT with whitespace-only tenant ID
@pytest.mark.asyncio
async def test_03_staff_jwt_with_whitespace_tenant_id_rejected(session: AsyncSession, setup_two_tenants: dict):
    auth_whitespace = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@support.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "   "},
    )
    with pytest.raises(HTTPException) as exc:
        await list_tickets(db=session, auth=auth_whitespace)
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


# 4. Tenant A ADMIN cannot list Tenant B tickets
@pytest.mark.asyncio
async def test_04_tenant_a_admin_cannot_list_tenant_b_tickets(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_a = ctx["ticket_a"]
    auth_admin_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="admin_a@tenant-alpha.com",
        roles=["ADMIN"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    res = await list_tickets(db=session, auth=auth_admin_a)
    assert res.total_count == 1
    assert len(res.items) == 1
    assert res.items[0].id == str(ticket_a.id)


# 5. Tenant A ADMIN cannot retrieve Tenant B ticket details
@pytest.mark.asyncio
async def test_05_tenant_a_admin_cannot_get_tenant_b_ticket_detail(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_b = ctx["ticket_b"]
    auth_admin_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="admin_a@tenant-alpha.com",
        roles=["ADMIN"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(HTTPException) as exc:
        await get_ticket_detail(ticket_id=ticket_b.id, db=session, auth=auth_admin_a)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


# 6. Tenant A AGENT cannot access Tenant B evidence
@pytest.mark.asyncio
async def test_06_tenant_a_agent_cannot_access_tenant_b_evidence(setup_two_tenants: dict):
    ctx = setup_two_tenants
    cust_b = ctx["cust_b"]
    evidence_b = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=cust_b.id,
        file_name="invoice_b.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/tenant-beta/evidence/invoice_b.pdf",
        status="PROCESSED",
    )
    evidence_b.customer = cust_b
    evidence_svc = EvidenceService()
    auth_agent_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(EvidenceAuthorizationError) as exc:
        evidence_svc._check_evidence_access(evidence_b, auth_agent_a)
    assert "permission" in str(exc.value).lower() or "tenant" in str(exc.value).lower()


# 7. Tenant A STAFF cannot access Tenant B evidence
@pytest.mark.asyncio
async def test_07_tenant_a_staff_cannot_access_tenant_b_evidence(setup_two_tenants: dict):
    ctx = setup_two_tenants
    cust_b = ctx["cust_b"]
    evidence_b = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=cust_b.id,
        file_name="invoice_b.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/tenant-beta/evidence/invoice_b.pdf",
        status="PROCESSED",
    )
    evidence_b.customer = cust_b
    evidence_svc = EvidenceService()
    auth_staff_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="staff@tenant-alpha.com",
        roles=["STAFF"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(EvidenceAuthorizationError) as exc:
        evidence_svc._check_evidence_access(evidence_b, auth_staff_a)
    assert "permission" in str(exc.value).lower() or "tenant" in str(exc.value).lower()


# 8. Evidence with missing customer ownership is rejected
@pytest.mark.asyncio
async def test_08_evidence_with_missing_ownership_rejected(setup_two_tenants: dict):
    evidence_orphan = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=None,
        file_name="orphan.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/tenant-alpha/evidence/orphan.pdf",
        status="PROCESSED",
    )
    evidence_svc = EvidenceService()
    auth_staff = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(EvidenceAuthorizationError) as exc:
        evidence_svc._check_evidence_access(evidence_orphan, auth_staff)
    assert "ownership" in str(exc.value).lower()


# 9. Evidence with missing customer tenant metadata is rejected for tenant-scoped staff
@pytest.mark.asyncio
async def test_09_evidence_missing_customer_tenant_metadata_rejected(setup_two_tenants: dict):
    cust_unassigned = Customer(
        id=uuid.uuid4(),
        email="unassigned@example.com",
        full_name="Unassigned User",
        metadata_={},
    )
    evidence = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=cust_unassigned.id,
        file_name="doc.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/default/evidence/doc.pdf",
        status="PROCESSED",
    )
    evidence.customer = cust_unassigned
    evidence_svc = EvidenceService()
    auth_staff = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(EvidenceAuthorizationError) as exc:
        evidence_svc._check_evidence_access(evidence, auth_staff)
    assert "metadata" in str(exc.value).lower()


# 10. GLOBAL_ADMIN retains explicitly authorized cross-tenant access
@pytest.mark.asyncio
async def test_10_global_admin_retains_cross_tenant_access(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_a = ctx["ticket_a"]
    ticket_b = ctx["ticket_b"]
    auth_global = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="superadmin@platform.com",
        roles=["GLOBAL_ADMIN"],
        jwt_claims={},
    )
    # Tickets listing
    list_res = await list_tickets(db=session, auth=auth_global)
    assert list_res.total_count == 2
    # Ticket detail
    detail_b = await get_ticket_detail(ticket_id=ticket_b.id, db=session, auth=auth_global)
    assert detail_b.id == str(ticket_b.id)
    # Evidence access
    cust_b = ctx["cust_b"]
    evidence_b = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=cust_b.id,
        file_name="evidence_b.pdf",
        mime_type="application/pdf",
        file_size_bytes=512,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/tenant-beta/evidence/b.pdf",
        status="PROCESSED",
    )
    evidence_b.customer = cust_b
    evidence_svc = EvidenceService()
    # Does not raise
    evidence_svc._check_evidence_access(evidence_b, auth_global)


# 11. Unknown JWT role cannot access ticket endpoints
@pytest.mark.asyncio
async def test_11_unknown_jwt_role_cannot_access_ticket_endpoints(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_a = ctx["ticket_a"]
    auth_unknown = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="intruder@example.com",
        roles=["UNKNOWN_ROLE"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(HTTPException) as exc1:
        await list_tickets(db=session, auth=auth_unknown)
    assert exc1.value.status_code == status.HTTP_403_FORBIDDEN
    assert "role" in exc1.value.detail.lower()

    with pytest.raises(HTTPException) as exc2:
        await get_ticket_detail(ticket_id=ticket_a.id, db=session, auth=auth_unknown)
    assert exc2.value.status_code == status.HTTP_403_FORBIDDEN
    assert "role" in exc2.value.detail.lower()


# 12. Missing role cannot access ticket endpoints
@pytest.mark.asyncio
async def test_12_missing_role_cannot_access_ticket_endpoints(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_a = ctx["ticket_a"]
    auth_no_role = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="intruder@example.com",
        roles=[],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(HTTPException) as exc1:
        await list_tickets(db=session, auth=auth_no_role)
    assert exc1.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as exc2:
        await get_ticket_detail(ticket_id=ticket_a.id, db=session, auth=auth_no_role)
    assert exc2.value.status_code == status.HTTP_403_FORBIDDEN


# 13. Tenant A staff cannot list Tenant B conversations
@pytest.mark.asyncio
async def test_13_tenant_a_staff_cannot_list_tenant_b_conversations(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    conv_a = ctx["conv_a"]
    auth_staff_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    res = await list_conversations(db=session, auth=auth_staff_a)
    assert res.total_count == 1
    assert len(res.items) == 1
    assert res.items[0].id == str(conv_a.id)


# 14. Tenant A staff cannot retrieve Tenant B conversation details
@pytest.mark.asyncio
async def test_14_tenant_a_staff_cannot_get_tenant_b_conversation_detail(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    conv_b = ctx["conv_b"]
    auth_staff_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(HTTPException) as exc:
        await get_conversation(conversation_id=conv_b.id, db=session, auth=auth_staff_a)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


# 15. Tenant A staff cannot retrieve Tenant B conversation messages
@pytest.mark.asyncio
async def test_15_tenant_a_staff_cannot_get_tenant_b_conversation_messages(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    conv_b = ctx["conv_b"]
    auth_staff_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    with pytest.raises(HTTPException) as exc:
        await get_conversation_messages(conversation_id=conv_b.id, db=session, auth=auth_staff_a)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


# 16. Legitimate same-tenant access continues working
@pytest.mark.asyncio
async def test_16_legitimate_same_tenant_access_succeeds(session: AsyncSession, setup_two_tenants: dict):
    ctx = setup_two_tenants
    ticket_a = ctx["ticket_a"]
    conv_a = ctx["conv_a"]
    cust_a = ctx["cust_a"]

    auth_staff_a = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent@tenant-alpha.com",
        roles=["AGENT"],
        jwt_claims={"tenant_id": "tenant-alpha"},
    )
    # Tickets
    t_list = await list_tickets(db=session, auth=auth_staff_a)
    assert t_list.total_count == 1
    t_detail = await get_ticket_detail(ticket_id=ticket_a.id, db=session, auth=auth_staff_a)
    assert t_detail.id == str(ticket_a.id)

    # Conversations
    c_list = await list_conversations(db=session, auth=auth_staff_a)
    assert c_list.total_count == 1
    c_detail = await get_conversation(conversation_id=conv_a.id, db=session, auth=auth_staff_a)
    assert c_detail.id == str(conv_a.id)
    c_msgs = await get_conversation_messages(conversation_id=conv_a.id, db=session, auth=auth_staff_a)
    assert c_msgs.conversation_id == str(conv_a.id)

    # Evidence
    evidence_a = EvidenceFile(
        id=uuid.uuid4(),
        customer_id=cust_a.id,
        file_name="receipt_a.png",
        mime_type="image/png",
        file_size_bytes=2048,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="tenants/tenant-alpha/evidence/receipt_a.png",
        status="PROCESSED",
    )
    evidence_a.customer = cust_a
    evidence_svc = EvidenceService()
    # Does not raise
    evidence_svc._check_evidence_access(evidence_a, auth_staff_a)
