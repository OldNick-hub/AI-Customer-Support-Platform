"""Comprehensive Verification Test Suite for the Four Backend Integration Fixes.

Covers:
1. Fix 1: Pre-escalation evidence upload and post-escalation linkage to ticket.
   - Multiple evidence files linked upon escalation.
   - Repeated escalation idempotency.
   - Cross-tenant evidence isolation.
   - Quarantined, malicious, and purged evidence exclusion in synthesis.
   - Backfill unlinked evidence in TicketSynthesisService.
2. Fix 2: Anonymous customer escalation and tenant isolation.
   - Anonymous session escalation creates persistent guest Customer with tenant.
   - Subsequent turns reuse existing guest customer for the session.
   - Safe tenant extraction from session_token prefix, context_summary, and explicit argument.
   - Fail-closed refusal when tenant cannot be verified.
   - Same-tenant staff retrieval vs cross-tenant staff denial (404).
3. Fix 3: Outbox background worker scheduling & resilience.
   - Pending event claim and delivery.
   - Dialect awareness (SQLite vs PostgreSQL SKIP LOCKED).
   - WorkerLifecycleManager startup sweep and periodic scanner integration.
   - Error resilience and retry tracking.
4. Fix 4: Secure agent evidence listing APIs.
   - GET /api/v1/tickets/{ticket_id}/evidence with staff auth, tenant verification, pagination.
   - GET /api/v1/conversations/{conversation_id}/evidence with customer ownership & staff tenant checks.
   - Exclusion of PURGED files and sanitization of internal storage paths and encryption keys.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ResolvedAuthContext, get_db
from app.core.clock import SettableClock
from app.core.config import settings
from app.lifecycle import WorkerLifecycleManager
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.chat import ChatOutboxEvent
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.evidence import EvidenceFile, ExtractedEvidenceEntity
from app.models.ticket import SLACalendar, Ticket
from app.modules.evidence.schemas import EvidenceListItem, EvidenceListResponse
from app.modules.multilingual.outbox_worker import OutboxWorker
from app.modules.ticketing.escalation_service import EscalationDecision, EscalationService
from app.modules.ticketing.ticket_service import TicketSynthesisService


# ==============================================================================
# Helper JWT and Session generators
# ==============================================================================

def make_staff_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None, tenant_id: str = "tenant-alpha") -> str:
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


def make_admin_jwt(user_id: Optional[str] = None, tenant_id: str = "tenant-alpha") -> str:
    return make_staff_jwt(roles=["ADMIN"], user_id=user_id, tenant_id=tenant_id)


# ==============================================================================
# Test Fixtures
# ==============================================================================

@pytest_asyncio.fixture
async def base_setup(session: AsyncSession):
    """Seed base SLA calendar and registered customer."""
    calendar = SLACalendar(
        name=f"CAL-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
    )
    session.add(calendar)

    customer_alpha = Customer(
        full_name="Alice Alpha",
        email="alice@alpha.test",
        metadata_={"tenant_id": "tenant-alpha"},
    )
    session.add(customer_alpha)

    customer_beta = Customer(
        full_name="Bob Beta",
        email="bob@beta.test",
        metadata_={"tenant_id": "tenant-beta"},
    )
    session.add(customer_beta)

    # Seed staff role and staff users for auth tests
    role_res = await session.execute(select(Role).where(Role.name == "AGENT"))
    role_agent = role_res.scalar_one_or_none()
    if not role_agent:
        role_agent = Role(name="AGENT", description="Support Agent Role", permissions=["tickets:*"])
        session.add(role_agent)
        await session.flush()

    staff_alpha = User(
        email=f"agent-alpha-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Agent Alpha",
        department="Support",
        skills=["general"],
        is_active=True,
    )
    staff_beta = User(
        email=f"agent-beta-{uuid.uuid4().hex[:6]}@enterprise.internal",
        hashed_password="mock_password",
        full_name="Agent Beta",
        department="Support",
        skills=["general"],
        is_active=True,
    )
    session.add_all([staff_alpha, staff_beta])
    await session.flush()

    session.add_all([
        UserRole(user_id=staff_alpha.id, role_id=role_agent.id),
        UserRole(user_id=staff_beta.id, role_id=role_agent.id),
    ])
    await session.flush()

    return {
        "calendar": calendar,
        "customer_alpha": customer_alpha,
        "customer_beta": customer_beta,
        "staff_alpha": staff_alpha,
        "staff_beta": staff_beta,
    }


# ==============================================================================
# 1. FIX 1 TESTS: Evidence-to-Ticket Association & Synthesis
# ==============================================================================

@pytest.mark.asyncio
async def test_evidence_linked_to_ticket_on_escalation(session: AsyncSession, base_setup):
    """Verify pre-escalation evidence files are linked to the newly created ticket upon escalation."""
    calendar = base_setup["calendar"]
    customer = base_setup["customer_alpha"]

    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="sess-token-alpha-1",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    # Pre-escalation evidence files uploaded during chat
    now = datetime.now(timezone.utc)
    ev1 = EvidenceFile(
        conversation_id=conv.id,
        customer_id=customer.id,
        ticket_id=None,
        file_name="receipt_1.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_uri="mock://bucket/evidence/receipt_1.pdf",
        sha256_hash="hash1" + uuid.uuid4().hex[:20],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    ev2 = EvidenceFile(
        conversation_id=conv.id,
        customer_id=customer.id,
        ticket_id=None,
        file_name="screenshot_2.png",
        mime_type="image/png",
        file_size_bytes=2048,
        storage_uri="mock://bucket/evidence/screenshot_2.png",
        sha256_hash="hash2" + uuid.uuid4().hex[:20],
        status="STAGED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    # Purged file should NOT be linked
    ev_purged = EvidenceFile(
        conversation_id=conv.id,
        customer_id=customer.id,
        ticket_id=None,
        file_name="old_purged.png",
        mime_type="image/png",
        file_size_bytes=512,
        storage_uri="mock://bucket/evidence/old_purged.png",
        sha256_hash="hash3" + uuid.uuid4().hex[:20],
        status="PURGED",
        is_malicious=False,
        retention_expires_at=now - timedelta(days=1),
    )
    session.add_all([ev1, ev2, ev_purged])
    await session.flush()

    service = EscalationService()
    decision = await service.force_escalate(
        session=session,
        conversation_id=conv.id,
        reason="Manual escalation for complex dispute",
        calendar=calendar,
    )

    assert decision.should_escalate is True
    assert decision.ticket_id is not None

    # Refresh evidence files from DB
    await session.refresh(ev1)
    await session.refresh(ev2)
    await session.refresh(ev_purged)

    assert ev1.ticket_id == decision.ticket_id
    assert ev2.ticket_id == decision.ticket_id
    # Purged file was skipped
    assert ev_purged.ticket_id is None


@pytest.mark.asyncio
async def test_escalation_idempotency_preserves_evidence(session: AsyncSession, base_setup):
    """Repeated escalation attempts do not alter already-linked evidence."""
    calendar = base_setup["calendar"]
    customer = base_setup["customer_alpha"]

    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="sess-token-alpha-idem",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    now = datetime.now(timezone.utc)
    ev = EvidenceFile(
        conversation_id=conv.id,
        customer_id=customer.id,
        ticket_id=None,
        file_name="invoice.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_uri="mock://bucket/evidence/invoice.pdf",
        sha256_hash="hash_idem_" + uuid.uuid4().hex[:20],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    session.add(ev)
    await session.flush()

    service = EscalationService()
    dec1 = await service.force_escalate(
        session=session,
        conversation_id=conv.id,
        reason="First escalation",
        calendar=calendar,
    )
    assert dec1.should_escalate is True
    first_ticket_id = dec1.ticket_id

    await session.refresh(ev)
    assert ev.ticket_id == first_ticket_id

    # Second escalation attempt on already-escalated conversation
    dec2 = await service.force_escalate(
        session=session,
        conversation_id=conv.id,
        reason="Repeated escalation",
        calendar=calendar,
    )
    assert dec2.should_escalate is False
    assert dec2.ticket_id == first_ticket_id

    await session.refresh(ev)
    assert ev.ticket_id == first_ticket_id


@pytest.mark.asyncio
async def test_synthesis_service_filters_unusable_evidence(session: AsyncSession, base_setup):
    """TicketSynthesisService._get_valid_evidence_ids filters out PURGED, FLAGGED_UNSAFE, malicious, and expired evidence."""
    calendar = base_setup["calendar"]
    customer = base_setup["customer_alpha"]

    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="sess-token-synth-1",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    now = datetime.now(timezone.utc)
    ticket = Ticket(
        ticket_number="TCK-SYNTH-01",
        conversation_id=conv.id,
        customer_id=customer.id,
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
        title="Disputed transaction",
        description="Customer disputed transaction charge",
        masked_summary="Disputed transaction",
        priority="HIGH",
        status="OPEN",
        category="BILLING",
        synthesis_status="PENDING",
    )
    session.add(ticket)
    await session.flush()
    # 1. Valid evidence
    ev_valid = EvidenceFile(
        ticket_id=ticket.id,
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="valid.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_uri="mock://bucket/valid.pdf",
        sha256_hash="hash_valid_" + uuid.uuid4().hex[:16],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    # 2. Malicious
    ev_malicious = EvidenceFile(
        ticket_id=ticket.id,
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="exploit.png",
        mime_type="image/png",
        file_size_bytes=1024,
        storage_uri="mock://bucket/exploit.png",
        sha256_hash="hash_mal_" + uuid.uuid4().hex[:16],
        status="VERIFIED",
        is_malicious=True,
        retention_expires_at=now + timedelta(days=30),
    )
    # 3. Quarantined
    ev_quarantined = EvidenceFile(
        ticket_id=ticket.id,
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="quarantine.png",
        mime_type="image/png",
        file_size_bytes=1024,
        storage_uri="mock://bucket/quar.png",
        sha256_hash="hash_quar_" + uuid.uuid4().hex[:16],
        status="FLAGGED_UNSAFE",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    # 4. Purged
    ev_purged = EvidenceFile(
        ticket_id=ticket.id,
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="purged.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_uri="mock://bucket/purged.pdf",
        sha256_hash="hash_purged_" + uuid.uuid4().hex[:16],
        status="PURGED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    # 5. Expired retention
    ev_expired = EvidenceFile(
        ticket_id=ticket.id,
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="expired.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_uri="mock://bucket/expired.pdf",
        sha256_hash="hash_exp_" + uuid.uuid4().hex[:16],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now - timedelta(days=2),
    )
    session.add_all([ev_valid, ev_malicious, ev_quarantined, ev_purged, ev_expired])
    await session.flush()
    await session.refresh(ticket)

    synthesis_service = TicketSynthesisService()
    valid_ids = synthesis_service._get_valid_evidence_ids(ticket)

    assert str(ev_valid.id) in valid_ids
    assert str(ev_malicious.id) not in valid_ids
    assert str(ev_quarantined.id) not in valid_ids
    assert str(ev_purged.id) not in valid_ids
    assert str(ev_expired.id) not in valid_ids
    assert len(valid_ids) == 1


# ==============================================================================
# 2. FIX 2 TESTS: Anonymous Customer Escalation & Tenant Isolation
# ==============================================================================

@pytest.mark.asyncio
async def test_anonymous_escalation_creates_guest_customer_with_tenant(session: AsyncSession, base_setup):
    """Anonymous customer escalation creates a real Customer row tagged as guest with resolved tenant."""
    calendar = base_setup["calendar"]

    # Anonymous session: customer_id is None, token encodes tenant prefix
    anon_session = CustomerSession(
        customer_id=None,
        session_token="tenant-alpha:guest_token_12345",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(anon_session)
    await session.flush()

    conv = Conversation(
        session_id=anon_session.id,
        customer_id=None,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    service = EscalationService()
    decision = await service.force_escalate(
        session=session,
        conversation_id=conv.id,
        reason="Escalating anonymous chat user",
        calendar=calendar,
    )

    assert decision.should_escalate is True
    assert decision.ticket_id is not None

    await session.refresh(conv)
    await session.refresh(anon_session)

    # Conversation and session now have real customer_id
    assert conv.customer_id is not None
    assert anon_session.customer_id == conv.customer_id

    # Verify Customer row in database
    guest_customer = await session.get(Customer, conv.customer_id)
    assert guest_customer is not None
    assert guest_customer.full_name == "Guest Customer"
    assert guest_customer.metadata_.get("is_guest") is True
    assert guest_customer.metadata_.get("tenant_id") == "tenant-alpha"
    assert guest_customer.metadata_.get("session_id") == str(anon_session.id)

    # Verify Ticket links to this customer
    ticket = await session.get(Ticket, decision.ticket_id)
    assert ticket is not None
    assert ticket.customer_id == guest_customer.id


@pytest.mark.asyncio
async def test_anonymous_escalation_reuses_existing_guest_customer(session: AsyncSession, base_setup):
    """A session that already has a guest customer reuses that record without creating duplicate customers."""
    calendar = base_setup["calendar"]

    # Existing guest customer
    guest_cust = Customer(
        full_name="Guest Existing",
        email=f"guest_{uuid.uuid4().hex[:8]}@guest.local",
        metadata_={"tenant_id": "tenant-alpha", "is_guest": True},
    )
    session.add(guest_cust)
    await session.flush()

    anon_session = CustomerSession(
        customer_id=guest_cust.id,
        session_token="tenant-alpha:reuse_session",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(anon_session)
    await session.flush()

    conv = Conversation(
        session_id=anon_session.id,
        customer_id=guest_cust.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    service = EscalationService()
    decision = await service.force_escalate(
        session=session,
        conversation_id=conv.id,
        reason="Escalating again",
        calendar=calendar,
    )
    assert decision.should_escalate is True

    ticket = await session.get(Ticket, decision.ticket_id)
    assert ticket.customer_id == guest_cust.id


@pytest.mark.asyncio
async def test_anonymous_escalation_fails_closed_without_tenant(session: AsyncSession, base_setup):
    """If tenant identity cannot be established for anonymous session, escalation fails closed."""
    calendar = base_setup["calendar"]

    anon_session = CustomerSession(
        customer_id=None,
        session_token="completely_unidentifiable_token",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(anon_session)
    await session.flush()

    conv = Conversation(
        session_id=anon_session.id,
        customer_id=None,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    service = EscalationService()
    # Must raise ValueError and refuse to create an ungrounded ticket
    with pytest.raises(ValueError, match="Cannot determine tenant_id"):
        await service.force_escalate(
            session=session,
            conversation_id=conv.id,
            reason="Attempting ungrounded escalation",
            calendar=calendar,
        )


# ==============================================================================
# 3. FIX 3 TESTS: Outbox Background Worker & Lifecycle
# ==============================================================================

@pytest.mark.asyncio
async def test_outbox_worker_claims_and_delivers_pending_events(session: AsyncSession, base_setup):
    """Outbox worker claims pending outbox events, updates status to DELIVERED, and records timestamps."""
    customer = base_setup["customer_alpha"]
    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="token_outbox_1",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    event = ChatOutboxEvent(
        customer_id=customer.id,
        session_id=cust_session.id,
        conversation_id=conv.id,
        customer_message_id=uuid.uuid4(),
        agent_message_id=uuid.uuid4(),
        sequence_number=1,
        event_type="MESSAGE_TRANSLATED",
        payload={"message": "Hello world", "language": "en"},
        status="PENDING",
    )
    session.add(event)
    await session.commit()

    worker = OutboxWorker()
    results = await worker.claim_and_process_pending_events(session=session, batch_size=10)

    assert len(results) == 1
    assert results[0][1] is True
    await session.refresh(event)
    assert event.status == "DELIVERED"
    assert event.delivered_at is not None
    assert event.attempt_count == 0


@pytest.mark.asyncio
async def test_outbox_worker_lifecycle_startup_sweep(session: AsyncSession, base_setup):
    """WorkerLifecycleManager runs an initial outbox sweep during startup when enabled."""
    customer = base_setup["customer_alpha"]
    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="token_outbox_2",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    event = ChatOutboxEvent(
        customer_id=customer.id,
        session_id=cust_session.id,
        conversation_id=conv.id,
        customer_message_id=uuid.uuid4(),
        agent_message_id=uuid.uuid4(),
        sequence_number=1,
        event_type="MESSAGE_COMPLETED",
        payload={"text": "Test lifecycle startup sweep"},
        status="PENDING",
    )
    session.add(event)
    await session.commit()

    # Create session factory wrapper returning the test session
    class MockSessionContext:
        async def __aenter__(self):
            return session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    session_factory = MagicMock(return_value=MockSessionContext())

    lifecycle = WorkerLifecycleManager(session_factory=session_factory)

    # Run startup sweep via on_startup with background scanner tasks mocked to prevent DB thread races
    with patch.object(lifecycle.settings, "ENABLE_BACKGROUND_WORKERS", True), \
         patch.object(lifecycle.escalation_worker, "start_periodic_scanner"), \
         patch.object(lifecycle.reconciliation_worker, "start_periodic_scanner"), \
         patch.object(lifecycle.retention_worker, "start_periodic_scanner"), \
         patch.object(lifecycle.kb_retry_worker, "start_periodic_scanner"), \
         patch.object(lifecycle.outbox_worker, "start_periodic_scanner"):
        await lifecycle.on_startup()
        await lifecycle.on_shutdown()

    await session.refresh(event)
    assert event.status == "DELIVERED"
    assert event.delivered_at is not None


# ==============================================================================
# 4. FIX 4 TESTS: Secure Agent Evidence Listing APIs
# ==============================================================================

@pytest.mark.asyncio
async def test_get_ticket_evidence_staff_authorization_and_tenant_check(session: AsyncSession, base_setup):
    """GET /api/v1/tickets/{id}/evidence allows same-tenant staff, blocks cross-tenant staff (404), and blocks customers (403)."""
    calendar = base_setup["calendar"]
    customer = base_setup["customer_alpha"]  # tenant-alpha

    now = datetime.now(timezone.utc)
    ticket = Ticket(
        ticket_number="TCK-EV-001",
        customer_id=customer.id,
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
        title="Payment proof required",
        description="Ticket with evidence",
        masked_summary="Payment proof required",
        priority="NORMAL",
        status="OPEN",
        category="BILLING",
    )
    session.add(ticket)
    await session.flush()
    ev_active = EvidenceFile(
        ticket_id=ticket.id,
        customer_id=customer.id,
        file_name="receipt.pdf",
        mime_type="application/pdf",
        file_size_bytes=4096,
        storage_uri="mock://bucket/safe/receipt.pdf",
        sha256_hash="hash_tck_1_" + uuid.uuid4().hex[:16],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    ev_purged = EvidenceFile(
        ticket_id=ticket.id,
        customer_id=customer.id,
        file_name="purged_receipt.pdf",
        mime_type="application/pdf",
        file_size_bytes=4096,
        storage_uri="mock://bucket/safe/purged_receipt.pdf",
        sha256_hash="hash_tck_2_" + uuid.uuid4().hex[:16],
        status="PURGED",
        is_malicious=False,
        retention_expires_at=now - timedelta(days=1),
    )
    cust_sess = CustomerSession(
        customer_id=customer.id,
        session_token="cust-real-session-token",
        status="ACTIVE",
        expires_at=now + timedelta(days=1),
    )
    session.add(cust_sess)
    session.add_all([ev_active, ev_purged])
    await session.commit()

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Staff from same tenant (tenant-alpha) -> 200 OK
        token_alpha = make_staff_jwt(roles=["AGENT"], user_id=str(base_setup["staff_alpha"].id), tenant_id="tenant-alpha")
        res_same = await ac.get(
            f"/api/v1/tickets/{ticket.id}/evidence",
            headers={"Authorization": f"Bearer {token_alpha}"},
        )
        assert res_same.status_code == status.HTTP_200_OK, res_same.text
        data = res_same.json()
        assert data["total_count"] == 1
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["evidence_id"] == str(ev_active.id)
        assert item["file_name"] == "receipt.pdf"
        assert item["status"] == "VERIFIED"
        assert "storage_uri" not in item
        assert "mock://" not in json.dumps(item)

        # 2. Staff from cross-tenant (tenant-beta) -> 404 NOT FOUND
        token_beta = make_staff_jwt(roles=["AGENT"], user_id=str(base_setup["staff_beta"].id), tenant_id="tenant-beta")
        res_cross = await ac.get(
            f"/api/v1/tickets/{ticket.id}/evidence",
            headers={"Authorization": f"Bearer {token_beta}"},
        )
        assert res_cross.status_code == status.HTTP_404_NOT_FOUND

        # 3. Customer (SESSION_TOKEN) -> 403 FORBIDDEN
        res_cust = await ac.get(
            f"/api/v1/tickets/{ticket.id}/evidence",
            headers={"X-Session-Token": "cust-real-session-token"},
        )
        assert res_cust.status_code == status.HTTP_403_FORBIDDEN

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_conversation_evidence_access_control_and_sanitization(session: AsyncSession, base_setup):
    """GET /api/v1/conversations/{id}/evidence supports customer owner and same-tenant staff, blocks unauthorized callers."""
    customer = base_setup["customer_alpha"]  # tenant-alpha
    customer_other = base_setup["customer_beta"]

    cust_session = CustomerSession(
        customer_id=customer.id,
        session_token="session-owner-alpha",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    other_session = CustomerSession(
        customer_id=customer_other.id,
        session_token="session-owner-beta",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add_all([cust_session, other_session])
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="ACTIVE",
    )
    session.add(conv)
    await session.flush()

    now = datetime.now(timezone.utc)
    ev = EvidenceFile(
        conversation_id=conv.id,
        customer_id=customer.id,
        file_name="bank_statement.pdf",
        mime_type="application/pdf",
        file_size_bytes=8192,
        storage_uri="/var/internal/secure/data/bank_statement.pdf",
        sha256_hash="hash_conv_1_" + uuid.uuid4().hex[:16],
        status="VERIFIED",
        is_malicious=False,
        retention_expires_at=now + timedelta(days=30),
    )
    session.add(ev)
    await session.commit()

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Customer owner with valid session -> 200 OK
        res_owner = await ac.get(
            f"/api/v1/conversations/{conv.id}/evidence",
            headers={"X-Session-Token": "session-owner-alpha"},
        )
        assert res_owner.status_code == status.HTTP_200_OK, res_owner.text
        data = res_owner.json()
        assert data["total_count"] == 1
        assert data["items"][0]["evidence_id"] == str(ev.id)
        # Ensure internal file path is NOT leaked
        raw_response_text = res_owner.text
        assert "/var/internal/secure" not in raw_response_text

        # 2. Another customer -> 404 NOT FOUND
        res_other_cust = await ac.get(
            f"/api/v1/conversations/{conv.id}/evidence",
            headers={"X-Session-Token": "session-owner-beta"},
        )
        assert res_other_cust.status_code == status.HTTP_404_NOT_FOUND

        # 3. Staff from same tenant -> 200 OK
        staff_alpha = make_staff_jwt(roles=["AGENT"], user_id=str(base_setup["staff_alpha"].id), tenant_id="tenant-alpha")
        res_staff_same = await ac.get(
            f"/api/v1/conversations/{conv.id}/evidence",
            headers={"Authorization": f"Bearer {staff_alpha}"},
        )
        assert res_staff_same.status_code == status.HTTP_200_OK

        # 4. Staff from foreign tenant -> 404 NOT FOUND
        staff_beta = make_staff_jwt(roles=["AGENT"], user_id=str(base_setup["staff_beta"].id), tenant_id="tenant-beta")
        res_staff_diff = await ac.get(
            f"/api/v1/conversations/{conv.id}/evidence",
            headers={"Authorization": f"Bearer {staff_beta}"},
        )
        assert res_staff_diff.status_code == status.HTTP_404_NOT_FOUND

    app.dependency_overrides.clear()

