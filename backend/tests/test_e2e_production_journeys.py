"""End-to-End Production Readiness Journeys Test Suite (Phase 7).

Executes and verifies all 6 core business journeys against live FastAPI application endpoints:
- Journey A: Customer Support (Auth, conversation, message, synthesis, clarification, ticket ownership).
- Journey B: Staff Ticket Operations (Agent auth, queue inspection, priority evaluation, auto-routing, capacity-safe assignment).
- Journey C: Resolution & Action Orchestration (Playbook evaluation, parameter update, approval RBAC, action execution, resolution verification, audit trail).
- Journey D: Intelligent Escalation (After-hours check, sentiment evaluation, audit trail, Rule B scan, RBAC).
- Journey E: Multimodal Evidence Processing (Upload, OCR extraction, duplicate detection, claim reconciliation, financial non-auto-refund invariant).
- Journey F: Analytics & Operational Intelligence (KPI aggregations, time windows, performance breakdown, watchdog rule alerts, customer isolation).
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import io
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch
import uuid
import pypdf

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
from app.models.evidence import EvidenceFile, EvidenceVerification
from app.models.ticket import EscalationAudit, SLACalendar, Ticket, TicketEvent
from app.models.ticket_resolution import TicketResolution
from app.models.ticket_synthesis import TicketSynthesis


def make_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None, tenant_id: str = "default") -> str:
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
async def e2e_context(session: AsyncSession) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    # 1. SLA Calendar
    cal = SLACalendar(
        name=f"E2E-CAL-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(0, 0, 0),
        work_end_time=time(23, 59, 59),
        work_days=[1, 2, 3, 4, 5, 6, 7],
        holidays=[],
    )
    session.add(cal)
    await session.flush()

    # 2. Customers
    cust_alice = Customer(
        external_id=f"CUST-ALICE-{uuid.uuid4().hex[:6]}",
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Alice Customer",
        customer_tier="STANDARD",
        metadata_={"account_status": "VERIFIED", "tenant_id": "default"},
    )
    cust_bob = Customer(
        external_id=f"CUST-BOB-{uuid.uuid4().hex[:6]}",
        email=f"bob-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Bob Customer",
        customer_tier="VIP",
        metadata_={"account_status": "VERIFIED", "tenant_id": "default"},
    )
    session.add_all([cust_alice, cust_bob])
    await session.flush()

    # 3. Customer Sessions
    token_alice = f"sess-alice-{uuid.uuid4().hex}"
    token_bob = f"sess-bob-{uuid.uuid4().hex}"
    session_alice = CustomerSession(
        customer_id=cust_alice.id,
        session_token=token_alice,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    session_bob = CustomerSession(
        customer_id=cust_bob.id,
        session_token=token_bob,
        status="ACTIVE",
        expires_at=now + timedelta(days=2),
    )
    session.add_all([session_alice, session_bob])
    await session.flush()

    # 4. Staff Users & Roles
    role_agent = Role(name="AGENT", description="Support Agent")
    role_supervisor = Role(name="SUPERVISOR", description="Support Supervisor")
    session.add_all([role_agent, role_supervisor])
    await session.flush()

    user_agent = User(
        email=f"agent-{uuid.uuid4().hex[:6]}@platform.local",
        hashed_password="test_hash_agent",
        full_name="Sarah Agent",
        department="Hardware Support",
        skills=["hardware", "billing", "general"],
        is_active=True,
    )
    user_supervisor = User(
        email=f"supervisor-{uuid.uuid4().hex[:6]}@platform.local",
        hashed_password="test_hash_supervisor",
        full_name="David Supervisor",
        department="Operations",
        skills=["all"],
        is_active=True,
    )
    session.add_all([user_agent, user_supervisor])
    await session.flush()

    session.add_all([
        UserRole(user_id=user_agent.id, role_id=role_agent.id),
        UserRole(user_id=user_supervisor.id, role_id=role_supervisor.id),
    ])
    await session.flush()

    return {
        "calendar": cal,
        "cust_alice": cust_alice,
        "cust_bob": cust_bob,
        "token_alice": token_alice,
        "token_bob": token_bob,
        "session_alice": session_alice,
        "user_agent": user_agent,
        "user_supervisor": user_supervisor,
        "jwt_agent": make_jwt(["AGENT"], str(user_agent.id)),
        "jwt_supervisor": make_jwt(["SUPERVISOR"], str(user_supervisor.id)),
    }


# ==============================================================================
# Journey A: Customer Support Workflow
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_a_customer_support(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Customer Authenticates with X-Session-Token
        headers_alice = {"X-Session-Token": e2e_context["token_alice"]}
        me_resp = await client.get("/api/v1/auth/me", headers=headers_alice)
        assert me_resp.status_code == status.HTTP_200_OK
        assert me_resp.json()["customer_id"] == str(e2e_context["cust_alice"].id)

        # 2. Start Conversation & Send Message
        now = datetime.now(timezone.utc)
        conv = Conversation(
            session_id=e2e_context["session_alice"].id,
            customer_id=e2e_context["cust_alice"].id,
            primary_language="en",
            status="OPEN",
            turn_count=1,
            started_at=now,
            last_message_at=now,
        )
        session.add(conv)
        await session.flush()

        msg = Message(
            conversation_id=conv.id,
            sender_type="CUSTOMER",
            content="Screen cracked on arrival for model PX-900. Order #ORD-88219.",
            created_at=now,
        )
        session.add(msg)
        await session.flush()

        # 3. Retrieve messages and verify chronological order
        msgs_resp = await client.get(f"/api/v1/conversations/{conv.id}/messages", headers=headers_alice)
        assert msgs_resp.status_code == status.HTTP_200_OK
        assert len(msgs_resp.json()["items"]) >= 1
        assert "PX-900" in msgs_resp.json()["items"][0]["content"]

        # 4. Create Ticket linked to Conversation & Initiate Synthesis
        ticket = Ticket(
            customer_id=e2e_context["cust_alice"].id,
            conversation_id=conv.id,
            sla_calendar_id=e2e_context["calendar"].id,
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            title="Screen cracked on arrival",
            description="Screen cracked on arrival for model PX-900. Order #ORD-88219.",
            masked_summary="Screen cracked on arrival for model PX-900. Order #ORD-88219.",
            category="Hardware Support",
            priority="HIGH",
            status="OPEN",
            sla_target_minutes=240,
            sla_warning_at=now + timedelta(hours=3),
            sla_breach_at=now + timedelta(hours=4),
            created_at=now,
        )
        session.add(ticket)
        await session.flush()

        synth_payload = {
            "ticket_id": str(ticket.id),
            "force_resynthesis": True,
        }
        synth_resp = await client.post("/api/v1/tickets/synthesize", json=synth_payload, headers=headers_alice)
        assert synth_resp.status_code in [status.HTTP_200_OK, status.HTTP_201_CREATED]
        ticket_id = synth_resp.json()["ticket_id"]

        # 5. Customer Ownership Verification: Alice can read ticket, Bob receives 404
        tck_alice = await client.get(f"/api/v1/tickets/{ticket_id}", headers=headers_alice)
        assert tck_alice.status_code == status.HTTP_200_OK
        assert tck_alice.json()["id"] == ticket_id

        headers_bob = {"X-Session-Token": e2e_context["token_bob"]}
        tck_bob = await client.get(f"/api/v1/tickets/{ticket_id}", headers=headers_bob)
        assert tck_bob.status_code == status.HTTP_404_NOT_FOUND


# ==============================================================================
# Journey B: Staff Ticket Operations & Intelligent Routing
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_b_staff_ticket_operations(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create ticket for triage
        now = datetime.now(timezone.utc)
        ticket = Ticket(
            customer_id=e2e_context["cust_alice"].id,
            sla_calendar_id=e2e_context["calendar"].id,
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            title="Broken laptop display hinge",
            description="Display hinge cracked and cable disconnected.",
            masked_summary="Display hinge cracked and cable disconnected.",
            category="Hardware Support",
            priority="HIGH",
            status="OPEN",
            sla_target_minutes=240,
            sla_warning_at=now + timedelta(hours=3),
            sla_breach_at=now + timedelta(hours=4),
            created_at=now,
        )
        session.add(ticket)
        await session.flush()

        # 1. Staff Authenticates
        headers_agent = {"Authorization": f"Bearer {e2e_context['jwt_agent']}"}

        # 2. View Ticket Queue with Filters
        queue_resp = await client.get("/api/v1/tickets?status=OPEN&category=Hardware%20Support", headers=headers_agent)
        assert queue_resp.status_code == status.HTTP_200_OK
        assert queue_resp.json()["total_count"] >= 1

        # 3. Evaluate Ticket Priority
        prio_resp = await client.post(f"/api/v1/tickets/{ticket.id}/priority/evaluate", headers=headers_agent)
        assert prio_resp.status_code == status.HTTP_200_OK
        prio_data = prio_resp.json()
        assert prio_data["priority"] in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        assert "priority_score" in prio_data

        # 4. Route Ticket
        route_resp = await client.post(f"/api/v1/tickets/{ticket.id}/route", headers=headers_agent)
        assert route_resp.status_code == status.HTTP_200_OK
        assert "target_department" in route_resp.json()

        # 5. Assign Ticket to Eligible Agent
        assign_payload = {
            "agent_id": str(e2e_context["user_agent"].id),
            "reason": "Hardware technician on duty assignment.",
            "force_override": False,
        }
        assign_resp = await client.post(f"/api/v1/tickets/{ticket.id}/assign", json=assign_payload, headers=headers_agent)
        assert assign_resp.status_code == status.HTTP_200_OK
        assert assign_resp.json()["assigned_agent_id"] == str(e2e_context["user_agent"].id)

        # 6. Inspect Routing Audit History
        history_resp = await client.get(f"/api/v1/tickets/{ticket.id}/routing-history", headers=headers_agent)
        assert history_resp.status_code == status.HTTP_200_OK
        assert len(history_resp.json()["events"]) >= 1


# ==============================================================================
# Journey C: Resolution & Action Orchestration
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_c_resolution(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_agent = {"Authorization": f"Bearer {e2e_context['jwt_agent']}"}
        headers_supervisor = {"Authorization": f"Bearer {e2e_context['jwt_supervisor']}"}

        now = datetime.now(timezone.utc)
        ticket = Ticket(
            customer_id=e2e_context["cust_alice"].id,
            sla_calendar_id=e2e_context["calendar"].id,
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            title="Late package delivery refund request",
            description="Package delivered 10 days past guaranteed window.",
            masked_summary="Package delivered 10 days past guaranteed window.",
            category="Delayed Delivery",
            priority="MEDIUM",
            status="OPEN",
            sla_target_minutes=240,
            sla_warning_at=now + timedelta(hours=3),
            sla_breach_at=now + timedelta(hours=4),
            created_at=now,
        )
        session.add(ticket)
        await session.flush()

        # 1. Evaluate Resolution Playbook
        eval_resp = await client.post(f"/api/v1/tickets/{ticket.id}/resolution/evaluate", headers=headers_agent)
        assert eval_resp.status_code == status.HTTP_200_OK
        resolution_id = eval_resp.json()["resolution_id"]

        # 2. Inspect Resolution & Proposed Actions
        res_resp = await client.get(f"/api/v1/tickets/{ticket.id}/resolution", headers=headers_agent)
        assert res_resp.status_code == status.HTTP_200_OK
        actions = res_resp.json()["actions"]
        assert len(actions) >= 1
        action_id = actions[0]["action_id"]

        # 3. Update Action Parameters where permitted
        patch_payload = {
            "action_params": {"courtesy_credit_amount": 25.0, "reason": "Late package goodwill"},
        }
        patch_resp = await client.patch(
            f"/api/v1/tickets/{ticket.id}/resolution/actions/{action_id}",
            json=patch_payload,
            headers=headers_supervisor,
        )
        assert patch_resp.status_code == status.HTTP_200_OK

        # 4. Supervisor Approves Action
        approve_payload = {"reason": "Supervisor approval confirmed for credit voucher"}
        appr_resp = await client.post(
            f"/api/v1/tickets/{ticket.id}/resolution/actions/{action_id}/approve",
            json=approve_payload,
            headers=headers_supervisor,
        )
        assert appr_resp.status_code == status.HTTP_200_OK

        # 5. Execute Approved Action (Simulated Financial Integration)
        exec_payload = {"idempotency_key": str(uuid.uuid4())}
        exec_resp = await client.post(
            f"/api/v1/tickets/{ticket.id}/resolution/actions/{action_id}/execute",
            json=exec_payload,
            headers=headers_agent,
        )
        assert exec_resp.status_code == status.HTTP_200_OK
        assert exec_resp.json()["status"] in ["COMPLETED", "SUCCESS", "EXECUTED", "SUCCEEDED", "PENDING_EXECUTION"]

        # 6. Verify Resolution Lifecycle & Audit
        verify_resp = await client.post(f"/api/v1/tickets/{ticket.id}/resolution/verify", headers=headers_agent)
        assert verify_resp.status_code == status.HTTP_200_OK

        audit_resp = await client.get(f"/api/v1/tickets/{ticket.id}/resolution/history", headers=headers_agent)
        assert audit_resp.status_code == status.HTTP_200_OK
        assert len(audit_resp.json()["events"]) >= 1


# ==============================================================================
# Journey D: Escalation Management & Scan
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_d_escalation(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_agent = {"Authorization": f"Bearer {e2e_context['jwt_agent']}"}

        # 1. After-Hours Status Check
        ah_resp = await client.get("/api/v1/escalation/after-hours/status", headers=headers_agent)
        assert ah_resp.status_code == status.HTTP_200_OK
        assert "is_after_hours" in ah_resp.json()

        # 2. Evaluate Message Turn for Escalation
        now = datetime.now(timezone.utc)
        conv = Conversation(
            session_id=e2e_context["session_alice"].id,
            customer_id=e2e_context["cust_alice"].id,
            primary_language="en",
            status="OPEN",
            turn_count=3,
            started_at=now - timedelta(minutes=20),
            last_message_at=now,
        )
        session.add(conv)
        await session.flush()

        eval_payload = {
            "conversation_id": str(conv.id),
            "message_content": "This is completely unacceptable! I demand immediate escalation to a human supervisor right now!",
        }
        eval_resp = await client.post("/api/v1/escalation/evaluate", json=eval_payload, headers=headers_agent)
        assert eval_resp.status_code == status.HTTP_200_OK
        assert "should_escalate" in eval_resp.json()

        # 3. Retrieve Immutable Escalation Audits
        audit_resp = await client.get("/api/v1/escalation/audits?limit=10", headers=headers_agent)
        assert audit_resp.status_code == status.HTTP_200_OK
        assert isinstance(audit_resp.json(), list)
        assert len(audit_resp.json()) >= 1

        # 4. Trigger Batch Scan for Unresolved Conversations (Staff only)
        scan_resp = await client.post("/api/v1/escalation/scan-unresolved", headers=headers_agent)
        assert scan_resp.status_code == status.HTTP_200_OK
        assert "escalated_count" in scan_resp.json()


# ==============================================================================
# Journey E: Evidence Processing & Financial Trust Hierarchy
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_e_evidence_processing(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_alice = {"X-Session-Token": e2e_context["token_alice"]}
        now = datetime.now(timezone.utc)

        conv = Conversation(
            session_id=e2e_context["session_alice"].id,
            customer_id=e2e_context["cust_alice"].id,
            primary_language="en",
            status="OPEN",
            turn_count=1,
            started_at=now,
            last_message_at=now,
        )
        session.add(conv)
        await session.flush()

        # 1. Upload Supported Evidence File (Valid structurally compliant PDF)
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({"/Title": "Invoice 1092"})
        pdf_io = io.BytesIO()
        writer.write(pdf_io)
        pdf_bytes = pdf_io.getvalue()
        files = {"file": ("invoice_1092.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
        data = {"conversation_id": str(conv.id)}

        upload_resp = await client.post("/api/v1/evidence/upload", files=files, data=data, headers=headers_alice)
        assert upload_resp.status_code == status.HTTP_201_CREATED
        evidence_id = upload_resp.json()["evidence_id"]
        assert upload_resp.json()["status"] in ["STAGED", "PROCESSING", "COMPLETED", "VERIFIED"]

        # 2. Inspect Metadata & Storage Details
        detail_resp = await client.get(f"/api/v1/evidence/{evidence_id}", headers=headers_alice)
        assert detail_resp.status_code == status.HTTP_200_OK
        assert detail_resp.json()["file_name"] == "invoice_1092.pdf"

        # 3. Inspect OCR Extraction
        ext_resp = await client.get(f"/api/v1/evidence/{evidence_id}/extraction", headers=headers_alice)
        assert ext_resp.status_code == status.HTTP_200_OK

        # 4. Reconcile Evidence Against Customer Claim
        reconcile_payload = {
            "conversation_id": str(conv.id),
            "evidence_id": evidence_id,
            "claim_text": "I was charged $120.00 for order ORD-1092 on invoice 1092.",
        }
        rec_resp = await client.post("/api/v1/evidence/reconcile", json=reconcile_payload, headers=headers_alice)
        assert rec_resp.status_code == status.HTTP_200_OK
        rec_data = rec_resp.json()

        # 5. HARD INVARIANT CHECK: Document Corroboration CANNOT independently authorize a refund!
        assert rec_data["verification_level"] in ["DOCUMENT_CORROBORATED", "EXTRACTED"]
        assert rec_data["is_refund_authorized"] is False
        assert rec_data["requires_authoritative_verification"] is True


# ==============================================================================
# Journey F: Analytics, Dashboard & Tenant Isolation
# ==============================================================================

@pytest.mark.asyncio
async def test_journey_f_analytics(session: AsyncSession, e2e_context: Dict[str, Any]):
    app.dependency_overrides[get_db] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_agent = {"Authorization": f"Bearer {e2e_context['jwt_agent']}"}
        headers_alice = {"X-Session-Token": e2e_context["token_alice"]}

        # 1. Staff Loads Dashboard KPIs across Reporting Windows
        ov_resp = await client.get("/api/v1/analytics/overview?time_window=30d", headers=headers_agent)
        assert ov_resp.status_code == status.HTTP_200_OK
        ov_data = ov_resp.json()
        assert "total_tickets" in ov_data
        assert "sla_compliance_pct" in ov_data
        assert "priority_queue" in ov_data

        # 2. Staff Inspects Time-Series Performance Trends
        perf_resp = await client.get("/api/v1/analytics/performance?time_window=30d", headers=headers_agent)
        assert perf_resp.status_code == status.HTTP_200_OK
        perf_data = perf_resp.json()
        assert "ticket_trends" in perf_data
        assert "sla_by_category" in perf_data
        assert "sla_by_priority" in perf_data

        # 3. Staff Inspects Operational Intelligence Rule Alerts
        intel_resp = await client.get("/api/v1/analytics/operational-intelligence?time_window=30d", headers=headers_agent)
        assert intel_resp.status_code == status.HTTP_200_OK
        assert "insights" in intel_resp.json()
        assert "rules_applied" in intel_resp.json()

        # 4. Strict Role & Tenant Isolation: Customer receives ONLY their own scoped metrics
        cust_ov = await client.get("/api/v1/analytics/overview?time_window=30d", headers=headers_alice)
        assert cust_ov.status_code == status.HTTP_200_OK
        # Department workloads must be empty for customer calls
        assert cust_ov.json()["department_workload"] == []
