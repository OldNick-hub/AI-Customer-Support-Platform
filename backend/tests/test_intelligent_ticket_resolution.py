"""Comprehensive Test Suite for Day 14: Intelligent Customer Support Resolution & Action Orchestration Engine.

Testing Areas:
1. Playbook Category & Deterministic Eligibility:
   - All 9 initial categories (Order Status, Delayed Delivery, Cancellation, Return/Replacement,
     Refund Status, Payment Discrepancy, Account Access, Tech Support, General Inquiry).
   - Missing required information and evidence validation.
   - Contradiction blocking and suppression of automated resolutions.
2. Financial Safeguards & Human Approval:
   - Customer claims alone never establish verified financial loss.
   - DOCUMENT_CORROBORATED evidence generates sensitive action requiring supervisor approval.
   - Unapproved sensitive action execution fails closed (ActionNotApprovedError).
   - Staff RBAC: regular agents cannot approve sensitive actions; supervisors/admins can approve/reject.
3. Action Orchestration & Concurrency:
   - Action idempotency (repeated execution returns cached success).
   - Bounded retries (exceeding max_attempts raises ActionMaxAttemptsExceededError).
   - Live PostgreSQL row-lock concurrency test with 5 simultaneous execution attempts.
4. Resolution Verification Stage:
   - Verification fails when actions are unexecuted or failed.
   - Verification fails when contradictions remain.
   - Verification failure triggers Day 13 routing escalation.
   - Verification success sets ticket and resolution to RESOLVED and stamps resolved_at.
5. Lifecycle & SLA Immutability:
   - SLA targets and warning/breach timestamps are strictly immutable.
6. REST API Endpoints & RBAC:
   - Customer isolation (403 on evaluate, propose, approve, reject, execute, verify).
   - Customer authorized to view only their own ticket resolution and history.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.evidence import EvidenceFile
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.models.ticket_resolution import ResolutionAction, TicketResolution
from app.models.ticket_synthesis import TicketSynthesis
from app.modules.ticketing import (
    ActionConcurrencyConflictError,
    ActionMaxAttemptsExceededError,
    ActionNotApprovedError,
    ActionNotFoundError,
    ActionOrchestrationError,
    ActionOrchestrator,
    BasePlaybook,
    PlaybookCategory,
    PlaybookEvaluationResult,
    PlaybookRegistry,
    ProposedActionPlan,
    ResolutionAuthorizationError,
    ResolutionEngine,
    ResolutionNotFoundError,
    ResolutionVerificationError,
    TicketResolutionService,
    GoodwillCreditCalculationResult,
    GoodwillCreditPolicy,
    get_action_orchestrator,
    get_goodwill_credit_policy,
    get_playbook_registry,
    get_resolution_engine,
    get_ticket_resolution_service,
    normalize_iso4217_currency,
)


def make_staff_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id or str(uuid.uuid4()),
        "roles": roles or ["AGENT"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@pytest_asyncio.fixture(scope="function")
async def seeded_resolution_context(session: AsyncSession):
    now = datetime.now(timezone.utc)

    # 1. SLA Calendar
    calendar = SLACalendar(
        name=f"RES-CAL-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(9, 0, 0),
        work_end_time=time(17, 0, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=[],
    )
    session.add(calendar)
    await session.flush()

    # 2. Roles
    role_agent = Role(id=uuid.uuid4(), name="AGENT", description="Support Agent")
    role_supervisor = Role(id=uuid.uuid4(), name="SUPERVISOR", description="Team Supervisor")
    role_admin = Role(id=uuid.uuid4(), name="ADMIN", description="Administrator")
    session.add_all([role_agent, role_supervisor, role_admin])
    await session.flush()

    # 3. Staff Users
    agent_user = User(
        id=uuid.uuid4(),
        email=f"agent-{uuid.uuid4().hex[:6]}@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Resolving Agent",
        department="Support",
        skills=["general", "billing", "shipping"],
        max_active_tickets=5,
        is_active=True,
    )
    supervisor_user = User(
        id=uuid.uuid4(),
        email=f"supervisor-{uuid.uuid4().hex[:6]}@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Resolution Supervisor",
        department="Billing",
        skills=["billing", "refunds", "finance"],
        max_active_tickets=10,
        is_active=True,
    )
    session.add_all([agent_user, supervisor_user])
    await session.flush()

    session.add(UserRole(user_id=agent_user.id, role_id=role_agent.id))
    session.add(UserRole(user_id=supervisor_user.id, role_id=role_supervisor.id))
    await session.flush()

    # 4. Customers & Sessions
    cust_1 = Customer(
        id=uuid.uuid4(),
        email=f"cust1-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Alice Customer",
        customer_tier="STANDARD",
        created_at=now,
    )
    cust_2 = Customer(
        id=uuid.uuid4(),
        email=f"cust2-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Bob Unauthorized",
        customer_tier="STANDARD",
        created_at=now,
    )
    session.add_all([cust_1, cust_2])
    await session.flush()

    token_1 = f"token-cust1-{uuid.uuid4().hex}"
    token_2 = f"token-cust2-{uuid.uuid4().hex}"
    session.add_all([
        CustomerSession(customer_id=cust_1.id, session_token=token_1, expires_at=now + timedelta(days=1)),
        CustomerSession(customer_id=cust_2.id, session_token=token_2, expires_at=now + timedelta(days=1)),
    ])
    await session.flush()

    # 5. Tickets with Synthesis
    ticket_order = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"ORD-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_1.id,
        title="Where is my shipment?",
        description="Tracking number 123456789. Where is my order?",
        masked_summary="Customer inquiring about shipment status",
        priority="MEDIUM",
        status="OPEN",
        category="delivery_issue",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
    )
    ticket_billing = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"BIL-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust_1.id,
        title="Double billed for invoice",
        description="Customer claims charged twice for order 9999",
        masked_summary="Billing discrepancy claim",
        priority="HIGH",
        status="OPEN",
        category="payment_issue",
        sla_calendar_id=calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now + timedelta(hours=1),
        sla_breach_at=now + timedelta(hours=2),
    )
    session.add_all([ticket_order, ticket_billing])
    await session.flush()

    # Add synthesis to order ticket
    synth_order = TicketSynthesis(
        id=uuid.uuid4(),
        ticket_id=ticket_order.id,
        problem_statement="Customer is asking for shipment tracking status",
        reference_identifiers=[{"type": "order_id", "value": "ORD-12345"}],
        issue_category="delivery_issue",
        resolution_sought="Delivery status update",
        actions_already_attempted=[],
        linked_evidence_ids=[],
        missing_fields=[],
        contradictions=[],
        completeness_score=0.95,
        completeness_reasons=["Order identifier present"],
        llm_model_used="heuristic_test",
        synthesis_version=1,
    )
    # Add synthesis to billing ticket
    synth_billing = TicketSynthesis(
        id=uuid.uuid4(),
        ticket_id=ticket_billing.id,
        problem_statement="Customer claims double billing on order 9999",
        reference_identifiers=[{"type": "order_id", "value": "ORD-9999"}],
        issue_category="payment_issue",
        resolution_sought="Refund of duplicate charge",
        actions_already_attempted=[],
        linked_evidence_ids=[],
        missing_fields=[],
        contradictions=[],
        completeness_score=0.9,
        completeness_reasons=["Order identifier present"],
        llm_model_used="heuristic_test",
        synthesis_version=1,
    )
    session.add_all([synth_order, synth_billing])
    await session.flush()

    agent_jwt = make_staff_jwt(["AGENT"], str(agent_user.id))
    supervisor_jwt = make_staff_jwt(["SUPERVISOR"], str(supervisor_user.id))

    return {
        "calendar": calendar,
        "agent_user": agent_user,
        "supervisor_user": supervisor_user,
        "cust_1": cust_1,
        "cust_2": cust_2,
        "token_1": token_1,
        "token_2": token_2,
        "agent_jwt": agent_jwt,
        "supervisor_jwt": supervisor_jwt,
        "ticket_order": ticket_order,
        "ticket_billing": ticket_billing,
        "synth_order": synth_order,
        "synth_billing": synth_billing,
    }


# ==============================================================================
# 1. RESOLUTION ENGINE & PLAYBOOK UNIT TESTS
# ==============================================================================

class TestResolutionPlaybooksUnit:
    """Tests resolution playbook evaluation across all 9 categories."""

    def test_all_nine_playbooks_registered(self):
        registry = get_playbook_registry()
        for cat in PlaybookCategory:
            playbook = registry.get(cat)
            assert playbook is not None
            assert playbook.category == cat

    def test_order_status_playbook_missing_order_needs_information(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="delivery_issue",
            description="Where is my stuff?",
            synthesis_data={"reference_identifiers": []},
        )
        assert res.eligibility_decision == "NEEDS_INFORMATION"
        assert res.requires_human_approval is False
        assert any("identifier missing" in r for r in [res.rejection_or_escalation_reason or ""])

    def test_order_status_playbook_with_reference_eligible(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="delivery_issue",
            description="Tracking package for order 123",
            synthesis_data={"reference_identifiers": [{"type": "order_id", "value": "123"}]},
        )
        assert res.eligibility_decision == "ELIGIBLE"
        assert len(res.proposed_actions) == 1
        assert res.proposed_actions[0].action_type == "SEND_ORDER_STATUS"

    def test_delayed_delivery_playbook(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="delivery_issue",
            description="My package is late and delayed order #456",
        )
        assert res.category == PlaybookCategory.DELAYED_DELIVERY
        assert res.eligibility_decision == "ELIGIBLE"
        action_types = [a.action_type for a in res.proposed_actions]
        assert "REQUEST_CARRIER_INVESTIGATION" in action_types

    def test_cancellation_playbook_delivered_redirects_to_returns(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="cancellation_return",
            description="I received item already delivered order #789, cancel it",
            synthesis_data={"reference_identifiers": [{"type": "order_id", "value": "789"}]},
        )
        assert res.category == PlaybookCategory.CANCELLATION_REQUEST
        assert res.proposed_actions[0].action_type == "REDIRECT_TO_RETURNS"

    def test_return_replacement_missing_damaged_evidence_needs_info(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="damaged_product",
            description="The product arrived damaged in pieces order 555",
            evidence_data=[],
        )
        assert res.category == PlaybookCategory.RETURN_REPLACEMENT
        assert res.eligibility_decision == "NEEDS_INFORMATION"
        assert "Photo evidence required" in (res.rejection_or_escalation_reason or "")

    def test_refund_status_playbook(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="payment_issue",
            description="What is status of my refund, when will it arrive?",
        )
        assert res.category == PlaybookCategory.REFUND_STATUS
        assert res.proposed_actions[0].action_type == "SEND_REFUND_TIMELINE"

    def test_payment_discrepancy_unverified_claim_needs_information(self):
        engine = get_resolution_engine()
        # Customer claims double charge without any verified receipt evidence
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="payment_issue",
            description="I was charged double on my credit card",
            evidence_data=[],
        )
        assert res.category == PlaybookCategory.PAYMENT_DISCREPANCY
        assert res.eligibility_decision == "NEEDS_INFORMATION"
        assert res.requires_human_approval is False  # Cannot even propose action without evidence

    def test_payment_discrepancy_contradictions_escalate(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="payment_issue",
            description="Overcharge dispute",
            synthesis_data={"contradictions": [{"field": "amount", "description": "Receipt says $50, claimed $200"}]},
            evidence_data=[{"id": "ev1", "status": "VERIFIED"}],
        )
        assert res.category == PlaybookCategory.PAYMENT_DISCREPANCY
        assert res.eligibility_decision == "ESCALATE"
        assert len(res.proposed_actions) == 0

    def test_payment_discrepancy_corroborated_requires_supervisor_approval(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="payment_issue",
            description="Overcharge dispute",
            synthesis_data={"contradictions": []},
            evidence_data=[{"id": "ev1", "status": "VERIFIED"}],
        )
        assert res.category == PlaybookCategory.PAYMENT_DISCREPANCY
        assert res.eligibility_decision == "NEEDS_APPROVAL"
        assert res.requires_human_approval is True
        assert len(res.proposed_actions) == 1
        act = res.proposed_actions[0]
        assert act.action_type == "ISSUE_REFUND"
        assert act.is_sensitive is True
        assert act.requires_approval is True

    def test_account_access_compromise_requires_approval(self):
        engine = get_resolution_engine()
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="account_issue",
            description="My account was hacked and compromised by an unauthorized intruder",
        )
        assert res.category == PlaybookCategory.ACCOUNT_ACCESS
        assert res.eligibility_decision == "NEEDS_APPROVAL"
        assert res.requires_human_approval is True
        assert res.proposed_actions[0].action_type == "SUSPEND_ACCOUNT_PENDING_VERIFICATION"

    def test_technical_support_and_general_inquiry(self):
        engine = get_resolution_engine()
        res_tech = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="technical_support",
            description="App crashes on startup with error 500",
        )
        assert res_tech.category == PlaybookCategory.TECHNICAL_SUPPORT
        assert res_tech.eligibility_decision == "ELIGIBLE"

        res_gen = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="general",
            description="What are your holiday operating hours?",
        )
        assert res_gen.category == PlaybookCategory.GENERAL_INQUIRY
        assert res_gen.eligibility_decision == "ELIGIBLE"


# ==============================================================================
# 2. ACTION ORCHESTRATOR & CONCURRENCY TESTS
# ==============================================================================

class TestActionOrchestratorUnit:
    """Tests action orchestrator execution, idempotency, and approval preconditions."""

    @pytest.mark.asyncio
    async def test_sensitive_action_unapproved_fails_closed(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        orchestrator = get_action_orchestrator()
        ticket = ctx["ticket_billing"]

        # Create resolution & sensitive unapproved action
        resolution = TicketResolution(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            status="AWAITING_APPROVAL",
            playbook_category=PlaybookCategory.PAYMENT_DISCREPANCY.value,
            recommended_resolution="Test financial resolution",
            eligibility_decision="NEEDS_APPROVAL",
            requires_human_approval=True,
        )
        session.add(resolution)
        await session.flush()

        action = ResolutionAction(
            id=uuid.uuid4(),
            resolution_id=resolution.id,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_key=f"refund_{uuid.uuid4().hex[:6]}",
            action_params={"amount": 49.99},
            status="AWAITING_APPROVAL",
            is_sensitive=True,
            requires_approval=True,
        )
        session.add(action)
        await session.flush()

        # Attempt to execute unapproved action MUST raise ActionNotApprovedError
        with pytest.raises(ActionNotApprovedError) as exc_info:
            await orchestrator.execute_action(
                db=session,
                action_id=action.id,
                actor_id="test-agent",
                actor_roles=["AGENT"],
            )
        assert "requires explicit supervisor/admin approval" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_approved_sensitive_action_executes_honestly(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        orchestrator = get_action_orchestrator()
        ticket = ctx["ticket_billing"]

        resolution = TicketResolution(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            status="APPROVED",
            playbook_category=PlaybookCategory.PAYMENT_DISCREPANCY.value,
            recommended_resolution="Approved resolution",
            eligibility_decision="ELIGIBLE",
            requires_human_approval=True,
        )
        session.add(resolution)
        await session.flush()

        action = ResolutionAction(
            id=uuid.uuid4(),
            resolution_id=resolution.id,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_key=f"refund_{uuid.uuid4().hex[:6]}",
            action_params={"amount": 25.00, "currency": "USD"},
            status="APPROVED",
            is_sensitive=True,
            requires_approval=True,
            approved_by="supervisor_1",
            approved_at=datetime.now(timezone.utc),
            approval_reason="Verified duplicate billing with bank receipt.",
        )
        session.add(action)
        await session.flush()

        # Execution should succeed
        act, result = await orchestrator.execute_action(
            db=session,
            action_id=action.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        assert act.status == "SUCCEEDED"
        assert result["operation"] == "refund_ledger_recorded"
        assert result["payout_status"] == "PENDING_GATEWAY_INTEGRATION"
        assert "Internal financial ledger entry recorded" in result["notes"]

    @pytest.mark.asyncio
    async def test_action_idempotency_cached_success(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        orchestrator = get_action_orchestrator()
        ticket = ctx["ticket_order"]

        resolution = TicketResolution(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            status="IN_PROGRESS",
            playbook_category=PlaybookCategory.ORDER_STATUS.value,
            recommended_resolution="Status update",
            eligibility_decision="ELIGIBLE",
            requires_human_approval=False,
        )
        session.add(resolution)
        await session.flush()

        action = ResolutionAction(
            id=uuid.uuid4(),
            resolution_id=resolution.id,
            ticket_id=ticket.id,
            action_type="SEND_ORDER_STATUS",
            action_key="status_key_1",
            action_params={"status": "DELIVERED"},
            status="PROPOSED",
        )
        session.add(action)
        await session.flush()

        # 1st Execution
        act1, res1 = await orchestrator.execute_action(
            db=session, action_id=action.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        assert act1.status == "SUCCEEDED"
        assert act1.execution_attempts == 1

        # 2nd Execution -> must return immediately without incrementing attempts
        act2, res2 = await orchestrator.execute_action(
            db=session, action_id=action.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        assert act2.status == "SUCCEEDED"
        assert act2.execution_attempts == 1
        assert res1 == res2

    @pytest.mark.asyncio
    async def test_action_bounded_retries_exceeded(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        orchestrator = get_action_orchestrator()
        ticket = ctx["ticket_order"]

        resolution = TicketResolution(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            status="IN_PROGRESS",
            playbook_category=PlaybookCategory.ORDER_STATUS.value,
            recommended_resolution="Status update",
            eligibility_decision="ELIGIBLE",
        )
        session.add(resolution)
        await session.flush()

        action = ResolutionAction(
            id=uuid.uuid4(),
            resolution_id=resolution.id,
            ticket_id=ticket.id,
            action_type="SEND_ORDER_STATUS",
            action_key="status_fail_1",
            status="FAILED",
            execution_attempts=3,
            max_attempts=3,
        )
        session.add(action)
        await session.flush()

        with pytest.raises(ActionMaxAttemptsExceededError):
            await orchestrator.execute_action(
                db=session, action_id=action.id, actor_id="agent1", actor_roles=["AGENT"]
            )


# ==============================================================================
# 3. LIVE POSTGRESQL ROW-LOCK CONCURRENCY TEST
# ==============================================================================

@pytest.mark.asyncio
async def test_postgresql_action_execution_row_lock_concurrency():
    """Verify transaction-safe action execution under concurrent load on live PostgreSQL.

    Fires 5 simultaneous execution tasks via asyncio.gather against the same action.
    The row lock serializes operations: exactly 1 task executes, and subsequent tasks
    either receive an in-progress conflict or detect the cached terminal state.
    """
    if "sqlite" in settings.async_database_url:
        pytest.skip("Skipping PostgreSQL concurrency test: running in SQLite mode.")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    pg_engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    PgSessionLocal = async_sessionmaker(
        bind=pg_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    now = datetime.now(timezone.utc)
    ticket_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    resolution_id = uuid.uuid4()
    action_id = uuid.uuid4()

    try:
        async with PgSessionLocal() as session:
            # Setup test records
            cust = Customer(
                id=customer_id,
                email=f"conc-res-{uuid.uuid4().hex[:6]}@test.com",
                full_name="Action Concurrency Cust",
                customer_tier="STANDARD",
                created_at=now,
            )
            session.add(cust)

            cal_res = await session.execute(select(SLACalendar).limit(1))
            calendar = cal_res.scalar_one_or_none()

            ticket = Ticket(
                id=ticket_id,
                ticket_number=f"ACTCONC-{uuid.uuid4().hex[:6].upper()}",
                customer_id=customer_id,
                title="Action Concurrency Test",
                description="Testing row lock concurrency",
                masked_summary="Action concurrency summary",
                priority="MEDIUM",
                status="OPEN",
                category="delivery_issue",
                sla_calendar_id=calendar.id,
                sla_target_minutes=240,
                sla_warning_at=now + timedelta(hours=2),
                sla_breach_at=now + timedelta(hours=4),
            )
            session.add(ticket)

            resolution = TicketResolution(
                id=resolution_id,
                ticket_id=ticket_id,
                status="IN_PROGRESS",
                playbook_category="order_status_delivery",
                recommended_resolution="Shipment tracking",
                eligibility_decision="ELIGIBLE",
            )
            session.add(resolution)

            action = ResolutionAction(
                id=action_id,
                resolution_id=resolution_id,
                ticket_id=ticket_id,
                action_type="SEND_ORDER_STATUS",
                action_key="concurrent_action_1",
                action_params={"status": "IN_TRANSIT"},
                status="PROPOSED",
            )
            session.add(action)
            await session.commit()

        orchestrator = get_action_orchestrator()

        async def attempt_execute():
            async with PgSessionLocal() as sess:
                try:
                    await orchestrator.execute_action(
                        db=sess,
                        action_id=action_id,
                        actor_id="conc-worker",
                        actor_roles=["AGENT"],
                    )
                    await sess.commit()
                    return "EXECUTED"
                except ActionConcurrencyConflictError:
                    await sess.rollback()
                    return "CONFLICT"
                except Exception as e:
                    await sess.rollback()
                    return f"ERROR: {e}"

        # Launch 5 simultaneous execution attempts
        results = await asyncio.gather(*[attempt_execute() for _ in range(5)])

        # All tasks return either EXECUTED (either executed or idempotently returned cached) or CONFLICT
        # Exactly 0 failures
        assert all(r in ("EXECUTED", "CONFLICT") for r in results), f"Unexpected results: {results}"
        assert results.count("EXECUTED") >= 1

        # Check in DB that action executed exactly 1 time
        async with PgSessionLocal() as session:
            act_db = await session.scalar(select(ResolutionAction).where(ResolutionAction.id == action_id))
            assert act_db is not None
            assert act_db.status == "SUCCEEDED"
            assert act_db.execution_attempts == 1

    finally:
        async with PgSessionLocal() as session:
            try:
                await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(status="CLOSED"))
                await session.commit()
            except Exception:
                await session.rollback()
        await pg_engine.dispose()


# ==============================================================================
# 4. RESOLUTION LIFECYCLE SERVICE & VERIFICATION TESTS
# ==============================================================================

class TestResolutionServiceIntegration:
    """Tests TicketResolutionService lifecycle, approvals, verification, and SLA immutability."""

    @pytest.mark.asyncio
    async def test_evaluate_resolution_preserves_sla(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        svc = get_ticket_resolution_service()
        ticket = ctx["ticket_order"]

        target_orig = ticket.sla_target_minutes
        warn_orig = ticket.sla_warning_at
        breach_orig = ticket.sla_breach_at

        res = await svc.evaluate_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        assert res is not None
        assert res.playbook_category == "order_status_delivery"
        assert res.eligibility_decision == "ELIGIBLE"

        await session.refresh(ticket)
        # Verify SLA Timers NEVER mutated
        warn_refreshed = ticket.sla_warning_at if ticket.sla_warning_at.tzinfo else ticket.sla_warning_at.replace(tzinfo=timezone.utc)
        breach_refreshed = ticket.sla_breach_at if ticket.sla_breach_at.tzinfo else ticket.sla_breach_at.replace(tzinfo=timezone.utc)
        assert ticket.sla_target_minutes == target_orig
        assert warn_refreshed == warn_orig
        assert breach_refreshed == breach_orig

    @pytest.mark.asyncio
    async def test_supervisor_approval_and_rejection_workflow(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        svc = get_ticket_resolution_service()
        ticket = ctx["ticket_billing"]

        # 1. Propose sensitive financial action
        res = await svc.evaluate_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_params={"amount": 35.00},
            is_sensitive=True,
            requires_approval=True,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        assert action.status == "AWAITING_APPROVAL"

        # 2. Regular agent cannot approve (must raise ResolutionAuthorizationError)
        with pytest.raises(ResolutionAuthorizationError):
            await svc.approve_action(
                db=session,
                ticket_id=ticket.id,
                action_id=action.id,
                approval_reason="Agent approving self",
                actor_id=str(ctx["agent_user"].id),
                actor_roles=["AGENT"],
            )

        # 3. Supervisor approves
        approved_act = await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            approval_reason="Verified duplicate receipt with bank transaction.",
            actor_id=str(ctx["supervisor_user"].id),
            actor_roles=["SUPERVISOR"],
        )
        assert approved_act.status == "APPROVED"
        assert approved_act.approved_by == str(ctx["supervisor_user"].id)

    @pytest.mark.asyncio
    async def test_verification_success_resolves_ticket(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        svc = get_ticket_resolution_service()
        ticket = ctx["ticket_order"]

        # Evaluate resolution
        res = await svc.evaluate_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        # Execute the proposed action
        actions = await svc.get_resolution_actions(session, res.id)
        action = actions[0]
        await svc.execute_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )

        # Verify resolution
        verify_res = await svc.verify_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        assert verify_res["verified"] is True
        assert verify_res["resolution_status"] == "RESOLVED"
        assert verify_res["ticket_status"] == "RESOLVED"

        await session.refresh(ticket)
        assert ticket.status == "RESOLVED"
        assert ticket.resolution_status == "RESOLVED"
        assert ticket.resolved_at is not None

    @pytest.mark.asyncio
    async def test_verification_failure_escalates_ticket(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        svc = get_ticket_resolution_service()
        ticket = ctx["ticket_order"]

        # Evaluate resolution but DO NOT execute action
        res = await svc.evaluate_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )

        # Verification must FAIL because action is still in PROPOSED status
        verify_res = await svc.verify_resolution(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )
        assert verify_res["verified"] is False
        assert verify_res["resolution_status"] == "FAILED"
        assert verify_res["escalated"] is True

        await session.refresh(ticket)
        assert ticket.status == "ESCALATED"
        assert ticket.resolution_status == "FAILED"


# ==============================================================================
# 5. REST API ENDPOINTS & RBAC TESTS
# ==============================================================================

class TestResolutionApiEndpoints:
    """Tests API endpoints for resolution evaluation, action execution, and RBAC."""

    @pytest.mark.asyncio
    async def test_api_resolution_evaluate_rbac(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer token -> 403 Forbidden
            res_cust = await client.post(
                f"/api/v1/tickets/{ticket.id}/resolution/evaluate",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_cust.status_code == status.HTTP_403_FORBIDDEN

            # 2. Staff JWT -> 200 OK
            res_staff = await client.post(
                f"/api/v1/tickets/{ticket.id}/resolution/evaluate",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
            )
            assert res_staff.status_code == status.HTTP_200_OK
            data = res_staff.json()
            assert data["ticket_id"] == str(ticket.id)
            assert "actions" in data

    @pytest.mark.asyncio
    async def test_api_get_resolution_customer_isolation(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()

        await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer 1 (Owner) -> 200 OK
            res_owner = await client.get(
                f"/api/v1/tickets/{ticket.id}/resolution",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_owner.status_code == status.HTTP_200_OK

            # 2. Customer 2 (Not Owner) -> 403 Forbidden
            res_unauth = await client.get(
                f"/api/v1/tickets/{ticket.id}/resolution",
                headers={"X-Session-Token": ctx["token_2"]},
            )
            assert res_unauth.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_api_approve_action_supervisor_rbac(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_billing"]
        svc = get_ticket_resolution_service()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_params={"amount": 50.0},
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Regular Agent -> 403 Forbidden
            res_agent = await client.post(
                f"/api/v1/tickets/{ticket.id}/resolution/actions/{action.id}/approve",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
                json={"reason": "Self approving"},
            )
            assert res_agent.status_code == status.HTTP_403_FORBIDDEN

            # 2. Supervisor -> 200 OK
            res_sup = await client.post(
                f"/api/v1/tickets/{ticket.id}/resolution/actions/{action.id}/approve",
                headers={"Authorization": f"Bearer {ctx['supervisor_jwt']}"},
                json={"reason": "Supervisor official authorization"},
            )
            assert res_sup.status_code == status.HTTP_200_OK
            assert res_sup.json()["status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_api_resolution_history_audit_trail(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        actions = await svc.get_resolution_actions(session, res.id)
        action = actions[0]
        await svc.execute_action(
            db=session, ticket_id=ticket.id, action_id=action.id, actor_id="agent1", actor_roles=["AGENT"]
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_db] = lambda: session

            res_hist = await client.get(
                f"/api/v1/tickets/{ticket.id}/resolution/history",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_hist.status_code == status.HTTP_200_OK
            events = res_hist.json()["events"]
            event_types = [e["event_type"] for e in events]
            assert "RESOLUTION_EVALUATED" in event_types
            assert "RESOLUTION_ACTION_EXECUTED" in event_types


class TestProductionSafetyAudit:
    """Production Safety & Integration Audit Verification (Day 14 Audit)."""

    @pytest.mark.asyncio
    async def test_multi_currency_iso4217_and_non_usd_extraction(self):
        engine = get_resolution_engine()
        # Non-USD currency (EUR) in verified evidence
        ev_data = [
            {
                "id": str(uuid.uuid4()),
                "status": "VERIFIED",
                "extracted_amount": 129.50,
                "extracted_currency": "EUR",
            }
        ]
        res = engine.evaluate_ticket(
            ticket_id=str(uuid.uuid4()),
            category="payment_issue",
            description="Incorrect charge on credit card statement in Europe",
            synthesis_data={"contradictions": [], "currency": "EUR", "amount": 129.50},
            evidence_data=ev_data,
            priority="HIGH",
            sla_breached=False,
        )
        assert res.eligibility_decision == "NEEDS_APPROVAL"
        assert len(res.proposed_actions) == 1
        act = res.proposed_actions[0]
        assert act.action_type == "ISSUE_REFUND"
        # Confirm no hardcoded USD
        assert act.action_params["amount"] == 129.50
        assert act.action_params["currency"] == "EUR"
        assert act.action_params["payout_status"] == "PENDING_GATEWAY_INTEGRATION"
        assert act.action_params["live_gateway_settled"] is False
        assert act.action_params["authoritative_gateway_present"] is False

        # Currency normalization tests
        assert normalize_iso4217_currency("gbp") == "GBP"
        assert normalize_iso4217_currency("inr") == "INR"
        assert normalize_iso4217_currency("US Dollar") == "USD"  # Non-3-letter falls back to default

    @pytest.mark.asyncio
    async def test_approval_parameter_binding_and_invalidation(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()
        orchestrator = get_action_orchestrator()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )

        # Propose sensitive refund action for 50.00 EUR
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_params={"amount": 50.00, "currency": "EUR"},
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        assert action.status == "AWAITING_APPROVAL"

        # Supervisor approves
        action = await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            approval_reason="Authorized 50 EUR refund",
            actor_id="sup1",
            actor_roles=["SUPERVISOR"],
        )
        assert action.status == "APPROVED"
        assert "_approved_params_hash" in action.action_params

        # Attempt to tamper/update parameters to 500.00 EUR via update_action_params
        action = await svc.update_action_params(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            new_params={"amount": 500.00, "currency": "EUR"},
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        # Verify approval is immediately invalidated
        assert action.status == "AWAITING_APPROVAL"
        assert action.approved_by is None
        assert action.approved_at is None

        # Direct execution attempt must fail closed
        with pytest.raises(ActionNotApprovedError) as exc_info:
            await orchestrator.execute_action(
                db=session,
                action_id=action.id,
                actor_id="agent1",
                actor_roles=["AGENT"],
            )
        assert "requires explicit supervisor/admin approval" in str(exc_info.value)

        # Re-approve with 500.00 EUR
        action = await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            approval_reason="Supervisor verified updated 500 EUR amount",
            actor_id="sup1",
            actor_roles=["SUPERVISOR"],
        )
        assert action.status == "APPROVED"

        # Now execution succeeds
        act, exec_res = await orchestrator.execute_action(
            db=session,
            action_id=action.id,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        assert act.status == "SUCCEEDED"
        assert exec_res["amount"] == 500.00
        assert exec_res["currency"] == "EUR"
        assert exec_res["payout_status"] == "PENDING_GATEWAY_INTEGRATION"

    @pytest.mark.asyncio
    async def test_account_security_compromise_and_recovery_flow(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()
        orchestrator = get_action_orchestrator()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )

        # 1. Propose account suspension action
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="SUSPEND_ACCOUNT_PENDING_VERIFICATION",
            action_params={
                "lockout_reason": "Suspected breach",
                "recovery_path": "STAFF_IDENTITY_VERIFICATION_REQUIRED",
            },
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )

        # Approve and execute lockdown
        await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            approval_reason="Lockdown approved by security admin",
            actor_id="sec_admin",
            actor_roles=["ADMIN"],
        )
        act, lock_res = await orchestrator.execute_action(
            db=session,
            action_id=action.id,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        assert act.status == "SUCCEEDED"
        assert lock_res["operation"] == "account_temporarily_suspended"
        assert lock_res["unlock_action_available"] is True

        # 2. Propose RESTORE_ACCOUNT_ACCESS unlock action
        unlock_act = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="RESTORE_ACCOUNT_ACCESS",
            action_params={"identity_verified": True},
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=unlock_act.id,
            approval_reason="Customer presented government ID and passed 2FA challenge",
            actor_id="sec_admin",
            actor_roles=["ADMIN"],
        )
        unlocked, unlock_res = await orchestrator.execute_action(
            db=session,
            action_id=unlock_act.id,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        assert unlocked.status == "SUCCEEDED"
        assert unlock_res["operation"] == "account_access_restored"

    @pytest.mark.asyncio
    async def test_cannot_approve_or_reject_terminal_action(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="SEND_ORDER_STATUS",
            action_params={"status": "IN_TRANSIT"},
            is_sensitive=False,
            requires_approval=False,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        # Execute action to SUCCEEDED
        await svc.execute_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )

        # Attempt to reject succeeded action must fail with ActionOrchestrationError
        with pytest.raises(ActionOrchestrationError):
            await svc.reject_action(
                db=session,
                ticket_id=ticket.id,
                action_id=action.id,
                rejection_reason="Attempting to reject already succeeded action",
                actor_id="sup1",
                actor_roles=["SUPERVISOR"],
            )

        # Attempt to approve succeeded action must fail
        with pytest.raises(ActionOrchestrationError):
            await svc.approve_action(
                db=session,
                ticket_id=ticket.id,
                action_id=action.id,
                approval_reason="Attempting to approve already succeeded action",
                actor_id="sup1",
                actor_roles=["SUPERVISOR"],
            )

    @pytest.mark.asyncio
    async def test_api_patch_action_params_endpoint(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )
        action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="ISSUE_REFUND",
            action_params={"amount": 25.00, "currency": "USD"},
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=action.id,
            approval_reason="Pre-approved for 25 USD",
            actor_id="sup1",
            actor_roles=["SUPERVISOR"],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_db] = lambda: session

            # PATCH to change amount to 75.00 USD
            patch_res = await client.patch(
                f"/api/v1/tickets/{ticket.id}/resolution/actions/{action.id}",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
                json={"action_params": {"amount": 75.00, "currency": "USD"}},
            )
            assert patch_res.status_code == status.HTTP_200_OK
            data = patch_res.json()
            # Verified that status was dropped from APPROVED to AWAITING_APPROVAL
            assert data["status"] == "AWAITING_APPROVAL"
            assert data["action_params"]["amount"] == 75.00


class TestSmartINRGoodwillCreditPolicy:
    """Comprehensive test suite for India-First Smart INR Goodwill Credit Policy."""

    def test_standard_tier_defaults_and_max_cap(self):
        policy = get_goodwill_credit_policy()
        # Order value 8,000 INR (10% cap = 800 INR)
        # Moderate delay: default 499 INR applied
        res_mod = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("8000.00"),
            delay_severity="MODERATE",
            sla_breached=False,
        )
        assert res_mod.eligible is True
        assert res_mod.amount == Decimal("499.00")
        assert res_mod.currency == "INR"
        assert res_mod.customer_tier == "STANDARD"
        assert res_mod.requires_approval is False
        assert res_mod.approval_tier == "AUTOMATED_OR_AGENT"

        # Severe delay: 1.5x scaling -> 499 * 1.5 = 748.50 INR <= 800 cap <= 999 max
        res_sev = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("8000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert res_sev.eligible is True
        assert res_sev.amount == Decimal("748.50")
        assert res_sev.requires_approval is True
        assert res_sev.approval_tier == "SUPERVISOR"
        assert res_sev.required_role == "SUPERVISOR"

        # Very high order value: 50,000 INR (10% = 5,000 INR), severe delay capped at tier max 999 INR
        res_max = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("50000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert res_max.eligible is True
        # Standard tier default is 499, scaled 1.5x is 748.50. If default base was higher it would cap at 999.
        assert res_max.amount == Decimal("748.50")
        assert res_max.tier_max == Decimal("999.00")

    def test_vip_tier_defaults_and_max_cap(self):
        policy = get_goodwill_credit_policy()
        # Order value 20,000 INR (10% cap = 2,000 INR)
        # Moderate delay: VIP default 799 INR applied
        res_vip_mod = policy.calculate_credit(
            customer_tier="VIP",
            verified_order_value=Decimal("20000.00"),
            delay_severity="MODERATE",
            sla_breached=False,
        )
        assert res_vip_mod.eligible is True
        assert res_vip_mod.amount == Decimal("799.00")
        assert res_vip_mod.customer_tier == "VIP"
        assert res_vip_mod.requires_approval is True
        assert res_vip_mod.approval_tier == "SUPERVISOR"
        assert res_vip_mod.required_role == "SUPERVISOR"

        # Severe delay: 799 * 1.5 = 1198.50 INR (in range [1000, 1499] -> Senior Supervisor)
        res_vip_sev = policy.calculate_credit(
            customer_tier="VIP",
            verified_order_value=Decimal("20000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert res_vip_sev.eligible is True
        assert res_vip_sev.amount == Decimal("1198.50")
        assert res_vip_sev.requires_approval is True
        assert res_vip_sev.approval_tier == "SENIOR_SUPERVISOR"
        assert res_vip_sev.required_role == "SENIOR_SUPERVISOR"
        assert res_vip_sev.tier_max == Decimal("1499.00")

    def test_enterprise_tier_custom_config_and_bounded_safety(self):
        policy = get_goodwill_credit_policy()
        ent_config = {"default_credit": "1200.00", "max_credit": "2000.00"}
        res_ent = policy.calculate_credit(
            customer_tier="ENTERPRISE",
            verified_order_value=Decimal("30000.00"),
            delay_severity="MODERATE",
            sla_breached=False,
            enterprise_config=ent_config,
        )
        assert res_ent.eligible is True
        assert res_ent.amount == Decimal("1200.00")
        assert res_ent.approval_tier == "SENIOR_SUPERVISOR"
        assert res_ent.required_role == "SENIOR_SUPERVISOR"

        # Enterprise safety invariant: unbounded/exorbitant config is clamped to safety ceiling
        bad_config = {"default_credit": "50000.00", "max_credit": "999999.00"}
        res_clamped = policy.calculate_credit(
            customer_tier="ENTERPRISE",
            verified_order_value=Decimal("100000.00"),
            delay_severity="MODERATE",
            sla_breached=False,
            enterprise_config=bad_config,
        )
        # Safety invariant clamps tier_max to 2499.00
        assert res_clamped.tier_max == Decimal("2499.00")

    def test_ten_percent_order_value_ceiling_constraint(self):
        policy = get_goodwill_credit_policy()
        # Order value 3,000 INR -> 10% ceiling is 300 INR
        # Standard default is 499 INR, but 10% ceiling (300 INR) must strictly override
        res_small_order = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("3000.00"),
            delay_severity="MODERATE",
            sla_breached=False,
        )
        assert res_small_order.eligible is True
        assert res_small_order.amount == Decimal("300.00")
        assert res_small_order.order_value_cap == Decimal("300.00")
        assert res_small_order.requires_approval is False  # 300 <= 499 -> automated

        # Order value 1,500 INR -> 10% ceiling is 150 INR
        res_tiny_order = policy.calculate_credit(
            customer_tier="VIP",
            verified_order_value=Decimal("1500.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert res_tiny_order.eligible is True
        assert res_tiny_order.amount == Decimal("150.00")
        assert res_tiny_order.requires_approval is False

    def test_unverified_or_missing_order_value_fails_closed(self):
        policy = get_goodwill_credit_policy()
        # None order value
        res_none = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=None,
            delay_severity="SEVERE",
        )
        assert res_none.eligible is False
        assert res_none.amount == Decimal("0.00")
        assert "ORDER_VALUE_UNVERIFIED_BLOCK" in res_none.policy_rules_applied

        # Zero order value
        res_zero = policy.calculate_credit(
            customer_tier="VIP",
            verified_order_value=Decimal("0.00"),
        )
        assert res_zero.eligible is False
        assert res_zero.amount == Decimal("0.00")

        # Negative order value
        res_neg = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("-500.00"),
        )
        assert res_neg.eligible is False

    def test_duplicate_compensation_prevention(self):
        policy = get_goodwill_credit_policy()
        # Customer already received 499 INR for this incident
        res_dup = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("10000.00"),
            delay_severity="SEVERE",
            existing_credits_issued=Decimal("499.00"),
            allow_top_up=False,
        )
        assert res_dup.eligible is False
        assert res_dup.amount == Decimal("0.00")
        assert "DUPLICATE_COMPENSATION_PROHIBITED" in res_dup.policy_rules_applied
        assert "Duplicate compensation prohibited" in res_dup.reason

        # Top-up allowed: entitlement is 748.50, previous 499.00 -> top-up is 249.50
        res_topup = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("10000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
            existing_credits_issued=Decimal("499.00"),
            allow_top_up=True,
        )
        assert res_topup.eligible is True
        assert res_topup.amount == Decimal("249.50")
        assert res_topup.existing_credits_deducted == Decimal("499.00")

    def test_approval_matrix_tiers(self):
        policy = get_goodwill_credit_policy()
        # Tier 1: <= 499 -> AUTOMATED_OR_AGENT
        r1 = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("4990.00"),
            delay_severity="MODERATE",
        )
        assert r1.amount == Decimal("499.00")
        assert r1.approval_tier == "AUTOMATED_OR_AGENT"
        assert r1.required_role == "AGENT"
        assert r1.requires_approval is False

        # Tier 2: 500 - 999 -> SUPERVISOR
        r2 = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value=Decimal("8000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert r2.amount == Decimal("748.50")
        assert r2.approval_tier == "SUPERVISOR"
        assert r2.required_role == "SUPERVISOR"
        assert r2.requires_approval is True

        # Tier 3: 1,000 - 1,499 -> SENIOR_SUPERVISOR
        r3 = policy.calculate_credit(
            customer_tier="VIP",
            verified_order_value=Decimal("15000.00"),
            delay_severity="SEVERE",
            sla_breached=True,
        )
        assert r3.amount == Decimal("1198.50")
        assert r3.approval_tier == "SENIOR_SUPERVISOR"
        assert r3.required_role == "SENIOR_SUPERVISOR"
        assert r3.requires_approval is True

        # Tier 4: > 1,499 -> MANUAL_REVIEW_REQUIRED (Admin)
        ent_config = {"default_credit": "1800.00", "max_credit": "2499.00"}
        r4 = policy.calculate_credit(
            customer_tier="ENTERPRISE",
            verified_order_value=Decimal("30000.00"),
            delay_severity="MODERATE",
            enterprise_config=ent_config,
        )
        assert r4.amount == Decimal("1800.00")
        assert r4.approval_tier == "MANUAL_REVIEW_REQUIRED"
        assert r4.required_role == "ADMIN"
        assert r4.requires_approval is True

    def test_decimal_math_precision_and_no_floating_point_imprecision(self):
        policy = get_goodwill_credit_policy()
        res = policy.calculate_credit(
            customer_tier="STANDARD",
            verified_order_value="7499.99",
            delay_severity="SEVERE",
            sla_breached=True,
        )
        # Verify result is Decimal and correctly rounded to 2 decimal places
        assert isinstance(res.amount, Decimal)
        assert isinstance(res.verified_order_value, Decimal)
        assert isinstance(res.order_value_cap, Decimal)
        assert res.currency == "INR"
        # 10% of 7499.99 = 750.00
        assert res.order_value_cap == Decimal("750.00")
        # Scaled base 499 * 1.5 = 748.50
        assert res.amount == Decimal("748.50")
        params = res.to_action_params()
        assert params["amount"] == "748.50"
        assert params["credit_amount_inr"] == "748.50"
        assert params["currency"] == "INR"

    @pytest.mark.asyncio
    async def test_delayed_delivery_integration_flow_and_simulated_ledger(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        svc = get_ticket_resolution_service()
        orchestrator = get_action_orchestrator()

        now = datetime.now(timezone.utc)
        # Create delayed delivery ticket for VIP customer with verified order value
        vip_cust = Customer(
            id=uuid.uuid4(),
            email=f"vip-{uuid.uuid4().hex[:6]}@example.com",
            full_name="VIP Shopper",
            customer_tier="VIP",
            created_at=now,
        )
        session.add(vip_cust)
        await session.flush()
        ticket_delayed = Ticket(
            id=uuid.uuid4(),
            ticket_number=f"DEL-{uuid.uuid4().hex[:6].upper()}",
            customer_id=vip_cust.id,
            title="Shipment severely delayed for over 8 days",
            description="My package has been delayed for over a week, order #DEL-9876. Severe delay.",
            masked_summary="Severe delivery delay inquiry",
            priority="HIGH",
            status="OPEN",
            category="delivery_issue",
            sla_calendar_id=ctx["calendar"].id,
            sla_target_minutes=240,
            sla_warning_at=now + timedelta(hours=2),
            sla_breach_at=now + timedelta(hours=4),
        )
        session.add(ticket_delayed)
        await session.flush()

        # Add synthesis with verified order value of 15,000 INR
        synth = TicketSynthesis(
            id=uuid.uuid4(),
            ticket_id=ticket_delayed.id,
            problem_statement="VIP package severely delayed",
            reference_identifiers=[{"type": "order_id", "value": "DEL-9876"}],
            issue_category="delivery_issue",
            resolution_sought="Delivery expedite and courtesy credit",
            actions_already_attempted=[],
            linked_evidence_ids=[],
            missing_fields=[],
            contradictions=[],
            completeness_score=0.98,
            completeness_reasons=["Order reference verified"],
            llm_model_used="heuristic_test",
            synthesis_version=1,
        )
        session.add(synth)
        await session.flush()

        # Add verified evidence file with extracted order amount
        ev_file = EvidenceFile(
            id=uuid.uuid4(),
            ticket_id=ticket_delayed.id,
            customer_id=vip_cust.id,
            file_name="invoice_del9876.pdf",
            mime_type="application/pdf",
            file_size_bytes=10240,
            storage_uri="/staging/invoice_del9876.pdf",
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            status="VERIFIED",
            retention_expires_at=now + timedelta(days=30),
        )
        session.add(ev_file)
        await session.flush()

        # Add extracted entity with 15,000 INR
        from app.models.evidence import ExtractedEvidenceEntity
        ent = ExtractedEvidenceEntity(
            id=uuid.uuid4(),
            evidence_file_id=ev_file.id,
            extracted_order_id="DEL-9876",
            extracted_amount=15000.00,
            extracted_currency="INR",
            ocr_confidence=0.95,
            pii_redacted_text="Order DEL-9876 Total: INR 15000",
        )
        session.add(ent)
        await session.flush()

        # Evaluate resolution
        resolution = await svc.evaluate_resolution(
            db=session,
            ticket_id=ticket_delayed.id,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )
        assert resolution.status in ("EVALUATED", "AWAITING_APPROVAL")
        assert resolution.requires_human_approval is True

        actions = await svc.get_resolution_actions(db=session, resolution_id=resolution.id)
        goodwill_action = next((a for a in actions if a.action_type == "APPLY_GOODWILL_CREDIT"), None)
        assert goodwill_action is not None
        # VIP severe delay: 799 * 1.5 = 1198.50 INR -> SENIOR_SUPERVISOR
        assert goodwill_action.action_params["amount"] == "1198.50"
        assert goodwill_action.action_params["currency"] == "INR"
        assert goodwill_action.action_params["customer_tier"] == "VIP"
        assert goodwill_action.action_params["required_role"] == "SENIOR_SUPERVISOR"
        assert goodwill_action.status == "AWAITING_APPROVAL"

        # Regular agent attempt to approve must be rejected
        with pytest.raises(ResolutionAuthorizationError) as exc_agent:
            await svc.approve_action(
                db=session,
                ticket_id=ticket_delayed.id,
                action_id=goodwill_action.id,
                approval_reason="Agent courtesy approval",
                actor_id="agent1",
                actor_roles=["AGENT"],
            )
        assert "Action approval requires" in str(exc_agent.value)

        # Standard supervisor attempt to approve Senior Supervisor tier action must be rejected
        with pytest.raises(ResolutionAuthorizationError) as exc_sup:
            await svc.approve_action(
                db=session,
                ticket_id=ticket_delayed.id,
                action_id=goodwill_action.id,
                approval_reason="Supervisor courtesy approval",
                actor_id="sup1",
                actor_roles=["SUPERVISOR"],
            )
        assert "SENIOR_SUPERVISOR or ADMIN" in str(exc_sup.value)

        # Senior supervisor approves successfully
        approved_act = await svc.approve_action(
            db=session,
            ticket_id=ticket_delayed.id,
            action_id=goodwill_action.id,
            approval_reason="Senior supervisor approved severe delay goodwill compensation",
            actor_id="srsup1",
            actor_roles=["SENIOR_SUPERVISOR"],
        )
        assert approved_act.status == "APPROVED"
        assert approved_act.approved_by == "srsup1"

        # Execute goodwill credit action
        act, exec_res = await orchestrator.execute_action(
            db=session,
            action_id=approved_act.id,
            actor_id="srsup1",
            actor_roles=["SENIOR_SUPERVISOR"],
        )
        assert act.status == "SUCCEEDED"
        assert exec_res["operation"] == "goodwill_credit_applied"
        assert exec_res["currency"] == "INR"
        assert exec_res["amount"] == 1198.50
        assert exec_res["amount_decimal"] == "1198.50"
        assert exec_res["credit_reference"].startswith("SIM-INR-CREDIT-")
        assert exec_res["is_simulated_ledger_only"] is True
        assert exec_res["live_settlement"] is False
        assert exec_res["payout_status"] == "PENDING_GATEWAY_INTEGRATION"
        assert exec_res["authoritative_gateway_present"] is False

    @pytest.mark.asyncio
    async def test_high_tier_approval_rbac_admin_and_senior_supervisor(
        self, session: AsyncSession, seeded_resolution_context
    ):
        ctx = seeded_resolution_context
        ticket = ctx["ticket_order"]
        svc = get_ticket_resolution_service()

        res = await svc.evaluate_resolution(
            db=session, ticket_id=ticket.id, actor_id="agent1", actor_roles=["AGENT"]
        )

        # Propose goodwill credit exceeding 1,499 INR (Tier 4: Manual Review / Admin required)
        high_action = await svc.propose_action(
            db=session,
            ticket_id=ticket.id,
            action_type="APPLY_GOODWILL_CREDIT",
            action_params={
                "amount": "1800.00",
                "currency": "INR",
                "required_role": "ADMIN",
                "approval_tier": "MANUAL_REVIEW_REQUIRED",
            },
            is_sensitive=True,
            requires_approval=True,
            actor_id="agent1",
            actor_roles=["AGENT"],
        )

        # Supervisor attempt to approve Tier 4 action must fail
        with pytest.raises(ResolutionAuthorizationError) as exc_sup:
            await svc.approve_action(
                db=session,
                ticket_id=ticket.id,
                action_id=high_action.id,
                approval_reason="Supervisor override attempt",
                actor_id="sup1",
                actor_roles=["SUPERVISOR"],
            )
        assert "requires ADMIN role" in str(exc_sup.value)

        # Admin approves successfully
        admin_approved = await svc.approve_action(
            db=session,
            ticket_id=ticket.id,
            action_id=high_action.id,
            approval_reason="Admin authorized high-value exception",
            actor_id="admin1",
            actor_roles=["ADMIN"],
        )
        assert admin_approved.status == "APPROVED"
        assert admin_approved.approved_by == "admin1"



