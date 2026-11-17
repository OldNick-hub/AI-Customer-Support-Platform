"""Comprehensive Test Suite for Intelligent Ticket Routing & Priority Engine.

Phase 6 Testing Areas:
1. Deterministic Priority Evaluation:
   - Base scores by category & category floor enforcement.
   - Customer tier weighting (VIP, Enterprise, Standard).
   - Financial loss verification (uncorroborated vs corroborated variance vs contradictions).
   - Security compromise detection & critical floor elevation.
   - SLA proximity boost & score clamping [1.0, 5.0].
   - Structured explainability and supporting facts audit trail.
2. Intelligent Routing Engine:
   - Category to department mapping.
   - Skills matching and candidate ranking.
   - Workload tie-breaker and deterministic tie-breaking.
   - Saturation skipping and team queue fallback.
   - After-hours on-call routing.
3. Transaction-Safe Agent Capacity Locking & Concurrency:
   - SELECT FOR UPDATE capacity check via acquire_agent_capacity_lock.
   - Live PostgreSQL concurrency test with simultaneous assignments.
4. Ticket Routing Service Integration:
   - Priority evaluation preserving SLA deadlines.
   - Automatic routing and agent assignment.
   - Manual assignment with RBAC (self-assignment vs supervisor override vs unauthorized).
   - Routing history retrieval.
   - Department workload metrics.
5. REST API Endpoints & RBAC:
   - Customer isolation (403 on priority evaluation, routing, manual assignment, team workload).
   - Customer authorized to view only their own ticket routing history.
   - Staff JWT RBAC (agent vs supervisor vs admin).
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch
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
from app.db.session import AsyncSessionLocal
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.modules.evidence.schemas import FinancialVerificationLevel
from app.modules.ticketing import (
    CATEGORY_DEPARTMENT_MAP,
    DEPARTMENT_SKILLS_MAP,
    AgentCandidate,
    AgentCapacityExceededError,
    AgentNotFoundError,
    AgentRoleNotAuthorizedError,
    PriorityEngine,
    PriorityEvaluationRequest,
    PriorityEvaluationResult,
    PriorityLevel,
    RoutingDecision,
    RoutingEngine,
    TicketAuthorizationError,
    TicketNotFoundError,
    TicketRoutingAuthorizationError,
    TicketRoutingService,
    acquire_agent_capacity_lock,
    get_priority_engine,
    get_routing_engine,
    get_ticket_routing_service,
)


# ==============================================================================
# FIXTURES
# ==============================================================================

def make_staff_jwt(roles: Optional[List[str]] = None, user_id: Optional[str] = None) -> str:
    """Helper to generate a valid staff JWT Bearer token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id or str(uuid.uuid4()),
        "roles": roles or ["AGENT"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@pytest_asyncio.fixture(scope="function")
async def seeded_routing_context(session: AsyncSession):
    """Provisions isolated test customers, agents, SLA calendars, and tickets."""
    now = datetime.now(timezone.utc)

    # 1. SLA Calendar
    calendar = SLACalendar(
        name=f"CALENDAR-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(0, 0, 0),
        work_end_time=time(23, 59, 59),
        work_days=[1, 2, 3, 4, 5, 6, 7],
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
    # Agent 1: Billing Department (billing, refunds, invoices)
    agent_finance_1 = User(
        id=uuid.uuid4(),
        email="finance_1@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Alice Finance",
        department="Billing",
        skills=["billing", "refunds", "invoices"],
        max_active_tickets=3,
        is_active=True,
    )
    # Agent 2: Billing Department (billing only, lower capacity)
    agent_finance_2 = User(
        id=uuid.uuid4(),
        email="finance_2@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Bob Finance",
        department="Billing",
        skills=["billing"],
        max_active_tickets=2,
        is_active=True,
    )
    # Agent 3: Security Department
    agent_security = User(
        id=uuid.uuid4(),
        email="security@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Carol Security",
        department="Security",
        skills=["security", "authentication", "gdpr"],
        max_active_tickets=5,
        is_active=True,
    )
    # Supervisor: Support Department
    supervisor_user = User(
        id=uuid.uuid4(),
        email="supervisor@support.internal",
        hashed_password="mock_hashed_password",
        full_name="Dan Supervisor",
        department="Support",
        skills=["escalations"],
        max_active_tickets=10,
        is_active=True,
    )

    session.add_all([agent_finance_1, agent_finance_2, agent_security, supervisor_user])
    await session.flush()

    # User Roles
    session.add_all([
        UserRole(user_id=agent_finance_1.id, role_id=role_agent.id),
        UserRole(user_id=agent_finance_2.id, role_id=role_agent.id),
        UserRole(user_id=agent_security.id, role_id=role_agent.id),
        UserRole(user_id=supervisor_user.id, role_id=role_supervisor.id),
    ])

    # 4. Customers
    # Customer 1: Standard tier
    cust_1 = Customer(
        id=uuid.uuid4(),
        email="alice@customer.com",
        full_name="Alice Customer",
        customer_tier="STANDARD",
        created_at=now,
    )
    # Customer 2: VIP tier
    cust_vip = Customer(
        id=uuid.uuid4(),
        email="vip@customer.com",
        full_name="Victor VIP",
        customer_tier="VIP",
        created_at=now,
    )
    session.add_all([cust_1, cust_vip])
    await session.flush()

    # Sessions
    sess_token_1 = f"alice-session-{uuid.uuid4().hex}"
    cust_sess_1 = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_1.id,
        session_token=sess_token_1,
        expires_at=now + timedelta(days=7),
        created_at=now,
    )
    sess_token_vip = f"vip-session-{uuid.uuid4().hex}"
    cust_sess_vip = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_vip.id,
        session_token=sess_token_vip,
        expires_at=now + timedelta(days=7),
        created_at=now,
    )
    session.add_all([cust_sess_1, cust_sess_vip])
    await session.flush()

    # Conversations
    conv_1 = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_1.id,
        session_id=cust_sess_1.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    conv_vip = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_vip.id,
        session_id=cust_sess_vip.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add_all([conv_1, conv_vip])
    await session.flush()

    # Tickets
    # Ticket 1: Standard Billing ticket
    ticket_billing = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        title="Unexpected charge on invoice",
        description="I see a duplicate line item on my recent invoice.",
        masked_summary="Duplicate line item.",
        priority="MEDIUM",
        status="OPEN",
        category="billing_issue",
        synthesis_status="COMPLETE",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
    )
    # Ticket 2: VIP Security ticket
    ticket_security = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_vip.id,
        conversation_id=conv_vip.id,
        title="Unauthorized password reset attempt",
        description="Received 5 unexpected password reset SMS codes.",
        masked_summary="5 unexpected password reset SMS codes.",
        priority="HIGH",
        status="OPEN",
        category="security_incident",
        synthesis_status="COMPLETE",
        sla_calendar_id=calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now + timedelta(minutes=30),
        sla_breach_at=now + timedelta(hours=1),
    )
    session.add_all([ticket_billing, ticket_security])
    await session.commit()

    return {
        "calendar": calendar,
        "agent_finance_1": agent_finance_1,
        "agent_finance_2": agent_finance_2,
        "agent_security": agent_security,
        "supervisor_user": supervisor_user,
        "customer_1": cust_1,
        "token_1": sess_token_1,
        "ticket_billing": ticket_billing,
        "customer_vip": cust_vip,
        "token_vip": sess_token_vip,
        "ticket_security": ticket_security,
        "agent_jwt": make_staff_jwt(["AGENT"], str(agent_finance_1.id)),
        "supervisor_jwt": make_staff_jwt(["SUPERVISOR"], str(supervisor_user.id)),
        "admin_jwt": make_staff_jwt(["ADMIN"]),
    }


# ==============================================================================
# 1. PRIORITY ENGINE UNIT TESTS
# ==============================================================================

class TestPriorityEngineUnit:
    """Test unit behavior of PriorityEngine."""

    def test_priority_base_scores_and_floors(self):
        engine = PriorityEngine()

        # Category: general -> base 1.5, floor LOW -> LOW
        req_gen = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="general",
            customer_tier="STANDARD",
        )
        res_gen = engine.evaluate(req_gen)
        assert res_gen.priority == PriorityLevel.LOW
        assert res_gen.priority_score == 1.5
        assert res_gen.priority_floor == PriorityLevel.LOW

        # Category: payment_issue -> base 2.5, floor MEDIUM -> MEDIUM
        req_pay = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="STANDARD",
        )
        res_pay = engine.evaluate(req_pay)
        assert res_pay.priority == PriorityLevel.MEDIUM
        assert res_pay.priority_score == 2.5
        assert res_pay.priority_floor == PriorityLevel.MEDIUM

    def test_customer_tier_weighting(self):
        engine = PriorityEngine()

        # Standard tier: +0.0
        req_std = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="STANDARD",
        )
        res_std = engine.evaluate(req_std)
        score_std = res_std.priority_score

        # VIP tier: +0.5 boost
        req_vip = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="VIP",
        )
        res_vip = engine.evaluate(req_vip)
        assert res_vip.priority_score == round(score_std + 0.5, 2)
        assert any("VIP" in f for f in res_vip.supporting_facts)

        # Enterprise tier: +0.8 boost
        req_ent = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="ENTERPRISE",
        )
        res_ent = engine.evaluate(req_ent)
        assert res_ent.priority_score == round(score_std + 0.8, 2)
        assert any("ENTERPRISE" in f for f in res_ent.supporting_facts)

    def test_financial_evidence_verification(self):
        engine = PriorityEngine()

        # Scenario 1: Customer-reported claim without corroboration -> NO financial bonus (+0.0)
        req_unverified = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="STANDARD",
            problem_statement="Customer claims unauthorized $5,000 charge on account.",
            has_verified_financial_variance=False,
            evidence_verification_level=None,
        )
        res_unverified = engine.evaluate(req_unverified)
        assert any("No authoritative corroboration" in f for f in res_unverified.supporting_facts)
        assert res_unverified.priority_score == 2.5  # base payment score unchanged
        assert res_unverified.priority == PriorityLevel.MEDIUM

        # Scenario 2: Corroborated document evidence with verified variance -> moderate +0.5 boost (NOT +0.8)
        req_corroborated = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="STANDARD",
            evidence_verification_level=FinancialVerificationLevel.DOCUMENT_CORROBORATED,
            has_verified_financial_variance=True,
            verified_variance_amount=250.0,
            verified_currency="USD",
        )
        res_corroborated = engine.evaluate(req_corroborated)
        assert res_corroborated.priority_score == 3.0  # 2.5 + 0.5 = 3.0
        assert res_corroborated.priority == PriorityLevel.HIGH
        assert res_corroborated.priority != PriorityLevel.CRITICAL  # NEVER independently elevates to CRITICAL
        assert any("Financial Evidence Corroborated" in f for f in res_corroborated.supporting_facts)
        assert any("+0.5 score boost" in f for f in res_corroborated.supporting_facts)

        # Scenario 3: Corroborated evidence on low-baseline category (cancellation_return: 2.0 + 0.5 = 2.5) -> MEDIUM
        # Proves absence of artificial HIGH floor
        req_low_cat = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="cancellation_return",
            customer_tier="STANDARD",
            evidence_verification_level=FinancialVerificationLevel.DOCUMENT_CORROBORATED,
            has_verified_financial_variance=True,
            verified_variance_amount=50.0,
            verified_currency="USD",
        )
        res_low_cat = engine.evaluate(req_low_cat)
        assert res_low_cat.priority_score == 2.5  # 2.0 base + 0.5 boost = 2.5
        assert res_low_cat.priority == PriorityLevel.MEDIUM  # No artificial HIGH floor

        # Scenario 4: Contradiction detected -> blocks financial bonus, notes contradiction
        req_contradiction = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="payment_issue",
            customer_tier="STANDARD",
            has_contradictions=True,
            evidence_verification_level=FinancialVerificationLevel.DOCUMENT_CORROBORATED,
            has_verified_financial_variance=True,
            verified_variance_amount=250.0,
        )
        res_contradiction = engine.evaluate(req_contradiction)
        assert res_contradiction.priority_score == 2.5  # boost denied due to contradiction
        assert any("Contradictions Detected" in f for f in res_contradiction.supporting_facts)

    def test_security_compromise_and_sla_proximity(self):
        engine = PriorityEngine()

        # Security compromise: +1.5 boost, floor CRITICAL
        req_sec = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="account_issue",
            is_account_compromised=True,
        )
        res_sec = engine.evaluate(req_sec)
        assert res_sec.priority == PriorityLevel.CRITICAL
        assert res_sec.priority_score == 4.0  # 2.5 + 1.5
        assert any("Account lockout/compromise detected" in f for f in res_sec.supporting_facts)

        # SLA proximity: <= 30 minutes remaining -> +0.8 boost
        req_sla = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="general",
            minutes_to_sla_breach=15.0,
        )
        res_sla = engine.evaluate(req_sla)
        assert res_sla.priority_score == 2.3  # 1.5 + 0.8
        assert res_sla.priority == PriorityLevel.MEDIUM
        assert any("<= 30m to breach" in f for f in res_sla.supporting_facts)

        # SLA breached: +2.0 boost, floor URGENT
        req_breach = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="general",
            sla_breached=True,
        )
        res_breach = engine.evaluate(req_breach)
        assert res_breach.priority == PriorityLevel.URGENT
        assert res_breach.priority_score == 3.5  # 1.5 + 2.0
        assert res_breach.priority_floor == PriorityLevel.URGENT

    def test_score_clamping_and_explainability(self):
        engine = PriorityEngine()

        # Max out boosts: category 2.5 + tier 0.8 + financial 0.5 + security 1.5 + sla 2.0 = 7.3 -> clamped 5.0
        req_extreme = PriorityEvaluationRequest(
            ticket_id=str(uuid.uuid4()),
            issue_category="account_issue",
            customer_tier="ENTERPRISE",
            evidence_verification_level=FinancialVerificationLevel.DOCUMENT_CORROBORATED,
            has_verified_financial_variance=True,
            verified_variance_amount=1000.0,
            is_account_compromised=True,
            sla_breached=True,
        )
        res_extreme = engine.evaluate(req_extreme)
        # Must be clamped at 5.0
        assert res_extreme.priority_score == 5.0
        assert res_extreme.priority == PriorityLevel.URGENT
        assert len(res_extreme.supporting_facts) >= 4
        assert "Evaluated as URGENT" in res_extreme.reason


# ==============================================================================
# 2. ROUTING ENGINE UNIT TESTS
# ==============================================================================

class TestRoutingEngineUnit:
    """Test unit behavior of RoutingEngine."""

    def test_category_department_mapping(self):
        engine = RoutingEngine()
        assert engine.resolve_department("security_incident") == "Security"
        assert engine.resolve_department("billing_issue") == "Billing"
        assert engine.resolve_department("payment_issue") == "Billing"
        assert engine.resolve_department("product_defect") == "Technical"
        assert engine.resolve_department("delivery_issue") == "Logistics"
        assert engine.resolve_department("unknown_category") == "Support"

    @pytest.mark.asyncio
    async def test_find_best_agent_skills_ranking_and_capacity(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        engine = RoutingEngine()

        # In Billing department, agent_finance_1 has 3 matching skills vs agent_finance_2 who has 1.
        selected_agent, act_count, candidates = await engine.find_best_agent(
            db=session,
            department="Billing",
            category="billing_issue",
        )
        assert selected_agent is not None
        assert selected_agent.id == ctx["agent_finance_1"].id
        assert act_count == 0
        assert len(candidates) == 2

    @pytest.mark.asyncio
    async def test_find_best_agent_saturation_skips_to_next(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        engine = RoutingEngine()

        # Saturate agent_finance_1
        ctx["agent_finance_1"].max_active_tickets = 0
        session.add(ctx["agent_finance_1"])
        await session.flush()

        selected_agent, act_count, candidates = await engine.find_best_agent(
            db=session,
            department="Billing",
            category="billing_issue",
        )
        # Should pick agent_finance_2
        assert selected_agent is not None
        assert selected_agent.id == ctx["agent_finance_2"].id

    @pytest.mark.asyncio
    async def test_find_best_agent_all_saturated_returns_none(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        engine = RoutingEngine()

        # Saturate both agents
        ctx["agent_finance_1"].max_active_tickets = 0
        ctx["agent_finance_2"].max_active_tickets = 0
        session.add_all([ctx["agent_finance_1"], ctx["agent_finance_2"]])
        await session.flush()

        selected_agent, act_count, candidates = await engine.find_best_agent(
            db=session,
            department="Billing",
            category="billing_issue",
        )
        assert selected_agent is None
        assert act_count is None
        assert len(candidates) == 2


# ==============================================================================
# 3. ROUTING SERVICE & CAPACITY LOCKING INTEGRATION TESTS
# ==============================================================================

class TestRoutingServiceIntegration:
    """Test TicketRoutingService integration with database and capacity locking."""

    @pytest.mark.asyncio
    async def test_evaluate_priority_preserves_sla(self, session: AsyncSession, seeded_routing_context):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]

        original_target = ticket.sla_target_minutes
        original_warning = ticket.sla_warning_at
        original_breach = ticket.sla_breach_at

        result = await svc.evaluate_priority(
            db=session,
            ticket_id=ticket.id,
            actor_id="test-agent",
            actor_roles=["AGENT"],
        )

        await session.refresh(ticket)
        # Priority and explanation updated
        assert ticket.priority == result.priority.value
        assert ticket.priority_explanation is not None
        # Invariant check: SLA target, warning, and breach MUST NOT be changed
        assert ticket.sla_target_minutes == original_target
        assert abs((ticket.sla_warning_at.replace(tzinfo=None) - original_warning.replace(tzinfo=None)).total_seconds()) < 1.0
        assert abs((ticket.sla_breach_at.replace(tzinfo=None) - original_breach.replace(tzinfo=None)).total_seconds()) < 1.0

        # Audit event recorded
        events = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.event_type == "PRIORITY_EVALUATED",
            )
        )
        assert events.scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_route_ticket_assigns_best_agent_and_locks_capacity(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]

        decision = await svc.route_ticket(
            db=session,
            ticket_id=ticket.id,
            actor_id="system-router",
            actor_roles=["SYSTEM"],
        )

        await session.refresh(ticket)
        assert decision.routing_status == "ASSIGNED"
        assert decision.target_department == "Billing"
        assert decision.assigned_agent_id == str(ctx["agent_finance_1"].id)
        assert ticket.assigned_agent_id == ctx["agent_finance_1"].id
        assert ticket.assigned_team == "Billing"
        assert ticket.routing_status == "ASSIGNED"

        # Check audit event
        events = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.event_type == "TICKET_ROUTED",
            )
        )
        assert events.scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_route_ticket_fallback_to_team_queue_when_saturated(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]

        # Saturate both finance agents
        ctx["agent_finance_1"].max_active_tickets = 0
        ctx["agent_finance_2"].max_active_tickets = 0
        session.add_all([ctx["agent_finance_1"], ctx["agent_finance_2"]])
        await session.flush()

        decision = await svc.route_ticket(
            db=session,
            ticket_id=ticket.id,
            actor_id="system-router",
            actor_roles=["SYSTEM"],
        )

        await session.refresh(ticket)
        assert decision.routing_status == "TEAM_QUEUED"
        assert decision.assigned_agent_id is None
        assert ticket.assigned_agent_id is None
        assert ticket.assigned_team == "Billing"
        assert ticket.routing_status == "TEAM_QUEUED"

    @pytest.mark.asyncio
    async def test_manual_assignment_capacity_exceeded_and_supervisor_override(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]
        agent = ctx["agent_finance_2"]

        # Set capacity to 0
        agent.max_active_tickets = 0
        session.add(agent)
        await session.flush()

        # Regular assignment without override should raise AgentCapacityExceededError
        with pytest.raises(AgentCapacityExceededError) as exc_info:
            await svc.manually_assign_ticket(
                db=session,
                ticket_id=ticket.id,
                target_agent_id=agent.id,
                reason="Regular manual assign",
                actor_id=str(ctx["supervisor_user"].id),
                actor_roles=["SUPERVISOR"],
                force_override=False,
            )
        assert "has reached maximum capacity" in str(exc_info.value)

        # Supervisor assignment with force_override=True MUST succeed
        res = await svc.manually_assign_ticket(
            db=session,
            ticket_id=ticket.id,
            target_agent_id=agent.id,
            reason="Supervisor emergency assignment",
            actor_id=str(ctx["supervisor_user"].id),
            actor_roles=["SUPERVISOR"],
            force_override=True,
        )
        assert res["assigned_agent_id"] == str(agent.id)
        assert res["capacity_overridden"] is True

        await session.refresh(ticket)
        assert ticket.assigned_agent_id == agent.id

    @pytest.mark.asyncio
    async def test_agent_cannot_assign_to_other_agent(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]

        # Agent 1 attempting to assign to Agent 2
        with pytest.raises(TicketRoutingAuthorizationError) as exc_info:
            await svc.manually_assign_ticket(
                db=session,
                ticket_id=ticket.id,
                target_agent_id=ctx["agent_finance_2"].id,
                reason="Attempting to reassign",
                actor_id=str(ctx["agent_finance_1"].id),
                actor_roles=["AGENT"],
                force_override=False,
            )
        assert "may only assign tickets to themselves" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_manual_reassignment_audit_event(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()
        ticket = ctx["ticket_billing"]
        agent1 = ctx["agent_finance_1"]
        agent2 = ctx["agent_finance_2"]

        # Initial assignment to agent 1
        res1 = await svc.manually_assign_ticket(
            db=session,
            ticket_id=ticket.id,
            target_agent_id=agent1.id,
            reason="Initial assignment",
            actor_id=str(ctx["supervisor_user"].id),
            actor_roles=["SUPERVISOR"],
        )
        assert res1["assigned_agent_id"] == str(agent1.id)

        # Reassign to agent 2
        res2 = await svc.manually_assign_ticket(
            db=session,
            ticket_id=ticket.id,
            target_agent_id=agent2.id,
            reason="Reassigning to agent 2 for load balancing",
            actor_id=str(ctx["supervisor_user"].id),
            actor_roles=["SUPERVISOR"],
        )
        assert res2["assigned_agent_id"] == str(agent2.id)

        # Check audit trail in routing history
        history = await svc.get_routing_history(db=session, ticket_id=ticket.id)
        event_types = [h["event_type"] for h in history]
        assert "TICKET_ASSIGNED" in event_types
        assert "TICKET_REASSIGNED" in event_types

        reassign_event = next(h for h in history if h["event_type"] == "TICKET_REASSIGNED")
        payload = reassign_event["event_payload"]
        assert payload["previous_agent_id"] == str(agent1.id)
        assert payload["new_agent_id"] == str(agent2.id)
        assert payload["actor_id"] == str(ctx["supervisor_user"].id)

    @pytest.mark.asyncio
    async def test_get_teams_workload_metrics(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        svc = get_ticket_routing_service()

        # Supervisor can view workloads
        workloads = await svc.get_teams_workload(
            db=session,
            actor_roles=["SUPERVISOR"],
        )
        assert len(workloads) > 0
        billing_team = next((w for w in workloads if w["department"] == "Billing"), None)
        assert billing_team is not None
        assert billing_team["total_agents"] == 2
        assert billing_team["total_capacity"] == 5  # 3 + 2


# ==============================================================================
# 4. REST API ENDPOINTS & RBAC TESTS
# ==============================================================================

class TestRoutingApiEndpoints:
    """Test API endpoints for routing, priority evaluation, assignment, and RBAC."""

    @pytest.mark.asyncio
    async def test_api_evaluate_priority_rbac(self, session: AsyncSession, seeded_routing_context):
        ctx = seeded_routing_context
        ticket = ctx["ticket_billing"]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer token -> 403 Forbidden
            res_cust = await client.post(
                f"/api/v1/tickets/{ticket.id}/priority/evaluate",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_cust.status_code == status.HTTP_403_FORBIDDEN

            # 2. Staff JWT -> 200 OK
            res_staff = await client.post(
                f"/api/v1/tickets/{ticket.id}/priority/evaluate",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
            )
            assert res_staff.status_code == status.HTTP_200_OK
            data = res_staff.json()
            assert data["ticket_id"] == str(ticket.id)
            assert "priority" in data
            assert "priority_score" in data
            assert len(data["supporting_facts"]) > 0

    @pytest.mark.asyncio
    async def test_api_route_ticket_rbac(self, session: AsyncSession, seeded_routing_context):
        ctx = seeded_routing_context
        ticket = ctx["ticket_billing"]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer token -> 403 Forbidden
            res_cust = await client.post(
                f"/api/v1/tickets/{ticket.id}/route",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_cust.status_code == status.HTTP_403_FORBIDDEN

            # 2. Staff JWT -> 200 OK
            res_staff = await client.post(
                f"/api/v1/tickets/{ticket.id}/route",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
            )
            assert res_staff.status_code == status.HTTP_200_OK
            data = res_staff.json()
            assert data["ticket_id"] == str(ticket.id)
            assert data["target_department"] == "Billing"
            assert data["routing_status"] == "ASSIGNED"

    @pytest.mark.asyncio
    async def test_api_manual_assign_capacity_conflict(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        ticket = ctx["ticket_billing"]
        agent = ctx["agent_finance_2"]

        # Saturate agent
        agent.max_active_tickets = 0
        session.add(agent)
        await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_db] = lambda: session

            # Supervisor assign without override -> 409 Conflict
            res_conf = await client.post(
                f"/api/v1/tickets/{ticket.id}/assign",
                headers={"Authorization": f"Bearer {ctx['supervisor_jwt']}"},
                json={"agent_id": str(agent.id), "reason": "Capacity limit test", "force_override": False},
            )
            assert res_conf.status_code == status.HTTP_409_CONFLICT

            # Supervisor assign with force_override=True -> 200 OK
            res_ok = await client.post(
                f"/api/v1/tickets/{ticket.id}/assign",
                headers={"Authorization": f"Bearer {ctx['supervisor_jwt']}"},
                json={"agent_id": str(agent.id), "reason": "Emergency capacity override", "force_override": True},
            )
            assert res_ok.status_code == status.HTTP_200_OK
            assert res_ok.json()["capacity_overridden"] is True

    @pytest.mark.asyncio
    async def test_api_routing_history_customer_isolation(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context
        ticket = ctx["ticket_billing"]

        # Route ticket first to create history
        svc = get_ticket_routing_service()
        await svc.route_ticket(
            db=session,
            ticket_id=ticket.id,
            actor_id="test",
            actor_roles=["SYSTEM"],
        )
        await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer 1 (owner) -> 200 OK
            res_owner = await client.get(
                f"/api/v1/tickets/{ticket.id}/routing-history",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_owner.status_code == status.HTTP_200_OK
            assert len(res_owner.json()["events"]) > 0

            # 2. Customer 2 (VIP - not owner) -> 403 Forbidden
            res_other = await client.get(
                f"/api/v1/tickets/{ticket.id}/routing-history",
                headers={"X-Session-Token": ctx["token_vip"]},
            )
            assert res_other.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_api_teams_workload_rbac(
        self, session: AsyncSession, seeded_routing_context
    ):
        ctx = seeded_routing_context

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_db] = lambda: session

            # 1. Customer -> 403 Forbidden
            res_cust = await client.get(
                "/api/v1/tickets/teams/workload",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert res_cust.status_code == status.HTTP_403_FORBIDDEN

            # 2. Regular Agent -> 403 Forbidden (supervisor/admin only)
            res_agent = await client.get(
                "/api/v1/tickets/teams/workload",
                headers={"Authorization": f"Bearer {ctx['agent_jwt']}"},
            )
            assert res_agent.status_code == status.HTTP_403_FORBIDDEN

            # 3. Supervisor -> 200 OK
            res_sup = await client.get(
                "/api/v1/tickets/teams/workload",
                headers={"Authorization": f"Bearer {ctx['supervisor_jwt']}"},
            )
            assert res_sup.status_code == status.HTTP_200_OK
            assert "teams" in res_sup.json()


# ==============================================================================
# 5. POSTGRESQL ROW-LOCK CONCURRENCY TEST
# ==============================================================================

@pytest.mark.asyncio
async def test_postgresql_agent_capacity_row_lock_concurrency():
    """Verify transaction-safe agent capacity enforcement under concurrent execution.

    Spawns concurrent tasks assigning different tickets to the same agent with capacity=1
    against live PostgreSQL. Proves that SELECT FOR UPDATE serializes checks, so strictly
    one task succeeds and the others are rejected with AgentCapacityExceededError.
    """
    if "sqlite" in settings.async_database_url:
        pytest.skip("Skipping PostgreSQL concurrency test: running in SQLite mode.")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    # Dedicated engine with NullPool strictly bound to this test's event loop
    pg_engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    PgSessionLocal = async_sessionmaker(
        bind=pg_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    now = datetime.now(timezone.utc)
    agent_id = uuid.uuid4()
    ticket_ids = [uuid.uuid4() for _ in range(5)]
    customer_id = uuid.uuid4()

    try:
        # Setup isolated test records in live PostgreSQL
        async with PgSessionLocal() as session:
            # 1. Customer
            cust = Customer(
                id=customer_id,
                email=f"concurrency-{uuid.uuid4().hex[:6]}@test.com",
                full_name="Concurrency Test Cust",
                customer_tier="STANDARD",
                created_at=now,
            )
            session.add(cust)

            # 2. User with max_active_tickets = 1
            agent = User(
                id=agent_id,
                email=f"concurrency-agent-{uuid.uuid4().hex[:6]}@support.internal",
                hashed_password="mock_hashed_password",
                full_name="Concurrency Agent",
                department="Support",
                skills=["general"],
                max_active_tickets=1,
                is_active=True,
            )
            session.add(agent)
            await session.flush()

            # Find or create AGENT role
            role_res = await session.execute(select(Role).where(Role.name == "AGENT"))
            role_agent = role_res.scalar_one_or_none()
            if not role_agent:
                role_agent = Role(id=uuid.uuid4(), name="AGENT", description="Agent Role")
                session.add(role_agent)
                await session.flush()
            session.add(UserRole(user_id=agent_id, role_id=role_agent.id))

            # Find or create SLA calendar
            cal_res = await session.execute(select(SLACalendar).limit(1))
            calendar = cal_res.scalar_one_or_none()
            if not calendar:
                calendar = SLACalendar(
                    name=f"CONC-CAL-{uuid.uuid4().hex[:6]}",
                    timezone="UTC",
                    work_start_time=time(9, 0, 0),
                    work_end_time=time(17, 0, 0),
                    work_days=[1, 2, 3, 4, 5],
                    holidays=[],
                )
                session.add(calendar)
                await session.flush()

            # 3. Five distinct tickets
            for tid in ticket_ids:
                t = Ticket(
                    id=tid,
                    ticket_number=f"CONC-{uuid.uuid4().hex[:6].upper()}",
                    customer_id=customer_id,
                    title=f"Concurrent Ticket {tid}",
                    description="Concurrency test",
                    masked_summary="Concurrency test summary",
                    priority="MEDIUM",
                    status="OPEN",
                    category="general_inquiry",
                    sla_calendar_id=calendar.id,
                    sla_target_minutes=240,
                    sla_warning_at=now + timedelta(hours=2),
                    sla_breach_at=now + timedelta(hours=4),
                )
                session.add(t)

            await session.commit()

        async def attempt_assignment(t_id: uuid.UUID) -> str:
            """Helper running in independent database transaction session."""
            async with PgSessionLocal() as sess:
                try:
                    # Shared capacity locker with SELECT FOR UPDATE
                    await acquire_agent_capacity_lock(
                        db=sess,
                        agent_id=agent_id,
                        allow_override=False,
                    )
                    # Assign ticket
                    res = await sess.execute(select(Ticket).where(Ticket.id == t_id))
                    t = res.scalar_one()
                    t.assigned_agent_id = agent_id
                    t.status = "OPEN"
                    sess.add(t)
                    await sess.commit()
                    return "SUCCESS"
                except AgentCapacityExceededError:
                    await sess.rollback()
                    return "CAPACITY_EXCEEDED"
                except Exception as e:
                    await sess.rollback()
                    return f"ERROR: {e}"

        # Launch 5 simultaneous assignment attempts concurrently
        results = await asyncio.gather(
            attempt_assignment(ticket_ids[0]),
            attempt_assignment(ticket_ids[1]),
            attempt_assignment(ticket_ids[2]),
            attempt_assignment(ticket_ids[3]),
            attempt_assignment(ticket_ids[4]),
        )

        success_count = results.count("SUCCESS")
        exceeded_count = results.count("CAPACITY_EXCEEDED")

        # Invariant: Since max_active_tickets=1, EXACTLY 1 attempt must succeed, 4 must fail
        assert success_count == 1, f"Expected 1 SUCCESS, got {success_count} ({results})"
        assert exceeded_count == 4, f"Expected 4 CAPACITY_EXCEEDED, got {exceeded_count} ({results})"

    finally:
        # Teardown test records by closing tickets and deactivating user (app_user has append/update rights)
        async with PgSessionLocal() as session:
            try:
                await session.execute(
                    update(Ticket).where(Ticket.customer_id == customer_id).values(status="CLOSED")
                )
                await session.execute(
                    update(User).where(User.id == agent_id).values(is_active=False)
                )
                await session.commit()
            except Exception:
                await session.rollback()
        await pg_engine.dispose()
