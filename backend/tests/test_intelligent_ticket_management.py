"""Comprehensive Test Suite for Intelligent Ticket Management Engine.

Covers all Phase 6 testing areas:
1. Multi-message conversation ticket synthesis and fact extraction.
2. Context preservation across multi-turn dialogues.
3. Missing field detection for incomplete tickets.
4. Contradictory information detection.
5. Deterministic category-specific completeness scoring.
6. Targeted clarification question generation and prioritization.
7. Duplicate-question prevention across rounds.
8. Completeness re-evaluation after customer answers.
9. Ambiguous / non-informative response handling.
10. LLM failure and fallback behavior (deterministic heuristic fallback).
11. Tenant isolation (Customer A cannot access Customer B's ticket).
12. RBAC (Customer vs Staff endpoints, mark_complete requires staff JWT).
13. State machine transitions (DRAFT -> SYNTHESIZING -> SYNTHESIZED -> NEEDS_INFORMATION -> READY_FOR_REVIEW -> COMPLETE).
14. Database persistence and relationship loading.
15. REST API endpoint contracts.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.evidence import EvidenceFile
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.models.ticket_synthesis import TicketClarificationRequest, TicketSynthesis
from app.modules.ticketing import (
    CATEGORY_REQUIREMENTS,
    ClarificationQuestion,
    ClarificationRoundRequest,
    ClarificationRoundResult,
    CompletenessResult,
    CustomerResponseResult,
    InvalidSynthesisTransitionError,
    SynthesisRequest,
    SynthesisResult,
    TicketAuthorizationError,
    TicketClarificationEngine,
    TicketCompletenessEvaluator,
    TicketNotFoundError,
    TicketSynthesisConflictError,
    TicketSynthesisEngine,
    TicketSynthesisService,
    get_clarification_engine,
    get_completeness_evaluator,
    get_synthesis_engine,
    get_ticket_synthesis_service,
)
from app.modules.ticketing.ticket_synthesis import _heuristic_extract
from app.services.llm_gateway import LLMResponse


# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest_asyncio.fixture(scope="function")
async def seeded_ticket_context(session: AsyncSession):
    """Provisions isolated test customers, SLA calendars, tickets, and multi-turn conversations."""
    now = datetime.now(timezone.utc)

    # SLA Calendar
    calendar = SLACalendar(
        name=f"CALENDAR-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        work_start_time=time(9, 0, 0),
        work_end_time=time(17, 0, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=[],
    )
    session.add(calendar)
    await session.flush()

    # Customer 1 (Alice)
    cust_1 = Customer(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Support",
        customer_tier="STANDARD",
        created_at=now,
    )
    session.add(cust_1)

    sess_token_1 = f"alice-session-{uuid.uuid4().hex}"
    cust_sess_1 = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_1.id,
        session_token=sess_token_1,
        expires_at=now + timedelta(days=7),
        created_at=now,
    )
    session.add(cust_sess_1)

    conv_1 = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_1.id,
        session_id=cust_sess_1.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_1)

    # Multi-turn conversation messages for Alice (delivery issue, initially missing order ID)
    m1 = Message(
        id=uuid.uuid4(),
        conversation_id=conv_1.id,
        sender_type="CUSTOMER",
        content="Hello, my package has not arrived yet. It was supposed to be delivered yesterday.",
        detected_language="en",
        created_at=now - timedelta(minutes=10),
    )
    m2 = Message(
        id=uuid.uuid4(),
        conversation_id=conv_1.id,
        sender_type="AI_AGENT",
        content="I am sorry to hear that. Could you provide any tracking information?",
        detected_language="en",
        created_at=now - timedelta(minutes=9),
    )
    m3 = Message(
        id=uuid.uuid4(),
        conversation_id=conv_1.id,
        sender_type="CUSTOMER",
        content="The courier was ExpressDelivery and the tracking number is TRK-88123456.",
        detected_language="en",
        created_at=now - timedelta(minutes=8),
    )
    session.add_all([m1, m2, m3])

    # Ticket for Alice
    t1 = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_1.id,
        conversation_id=conv_1.id,
        title="Delivery Delay Inquiry",
        description="Customer package not received.",
        masked_summary="Package not received.",
        priority="MEDIUM",
        status="OPEN",
        category="delivery_issue",
        synthesis_status="DRAFT",
        sla_calendar_id=calendar.id,
        sla_target_minutes=240,
        sla_warning_at=now + timedelta(hours=2),
        sla_breach_at=now + timedelta(hours=4),
    )
    session.add(t1)

    # Customer 2 (Bob - Separate tenant)
    cust_2 = Customer(
        id=uuid.uuid4(),
        email="bob@example.com",
        full_name="Bob Support",
        customer_tier="STANDARD",
        created_at=now,
    )
    session.add(cust_2)

    sess_token_2 = f"bob-session-{uuid.uuid4().hex}"
    cust_sess_2 = CustomerSession(
        id=uuid.uuid4(),
        customer_id=cust_2.id,
        session_token=sess_token_2,
        expires_at=now + timedelta(days=7),
        created_at=now,
    )
    session.add(cust_sess_2)

    conv_2 = Conversation(
        id=uuid.uuid4(),
        customer_id=cust_2.id,
        session_id=cust_sess_2.id,
        primary_language="en",
        status="OPEN",
        started_at=now,
        last_message_at=now,
    )
    session.add(conv_2)

    # Bob's customer message (payment issue with invoice ID but missing resolution sought)
    bm1 = Message(
        id=uuid.uuid4(),
        conversation_id=conv_2.id,
        sender_type="CUSTOMER",
        content="I was charged twice for invoice INV-9988 on my card.",
        detected_language="en",
        created_at=now - timedelta(minutes=15),
    )
    session.add(bm1)

    t2 = Ticket(
        id=uuid.uuid4(),
        ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
        customer_id=cust_2.id,
        conversation_id=conv_2.id,
        title="Billing Discrepancy",
        description="Bob was charged incorrectly.",
        masked_summary="Charged incorrectly.",
        priority="HIGH",
        status="OPEN",
        category="payment_issue",
        synthesis_status="DRAFT",
        sla_calendar_id=calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now + timedelta(hours=1),
        sla_breach_at=now + timedelta(hours=2),
    )
    session.add(t2)

    # Seed Staff User with AGENT role for JWT RBAC tests
    staff_id = uuid.uuid4()
    staff_role = Role(id=uuid.uuid4(), name="AGENT", description="Support Agent")
    session.add(staff_role)
    await session.flush()

    staff_user = User(
        id=staff_id,
        email="agent_test@support.internal",
        full_name="Agent Smith",
        department="Support",
        hashed_password="mock_hashed_password",
        is_active=True,
    )
    session.add(staff_user)
    await session.flush()

    ur = UserRole(user_id=staff_user.id, role_id=staff_role.id)
    session.add(ur)

    staff_jwt = jwt.encode(
        {
            "sub": str(staff_user.id),
            "roles": ["AGENT"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=2)).timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    await session.commit()

    return {
        "customer_1": cust_1,
        "token_1": sess_token_1,
        "conv_1": conv_1,
        "ticket_1": t1,
        "customer_2": cust_2,
        "token_2": sess_token_2,
        "conv_2": conv_2,
        "ticket_2": t2,
        "calendar": calendar,
        "staff_user": staff_user,
        "staff_jwt": staff_jwt,
    }


def make_staff_jwt(roles: Optional[List[str]] = None) -> str:
    """Helper to generate a valid staff JWT Bearer token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "roles": roles or ["AGENT"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ==============================================================================
# 1. HEURISTIC SYNTHESIS UNIT TESTS
# ==============================================================================

def test_heuristic_synthesis_multi_message_identifiers_preserved():
    """Verifies that identifiers from multiple messages are extracted without losing earlier context."""
    messages = [
        {"role": "CUSTOMER", "content": "I ordered a monitor on Monday, order ORD-55443."},
        {"role": "AI_AGENT", "content": "Let me look into that."},
        {"role": "CUSTOMER", "content": "Also tracking number is TRK-99887766, it shows delivered but it is not."},
    ]
    result = _heuristic_extract(messages, evidence_ids=[])

    types = {i["type"] for i in result.reference_identifiers}
    values = {i["value"] for i in result.reference_identifiers}

    assert "ORDER_ID" in types
    assert "TRACKING_NUMBER" in types
    assert "ORD-55443" in values
    assert "TRK-99887766" in values
    assert result.issue_category == "delivery_issue"
    assert result.llm_model_used == "fallback_heuristic"


def test_heuristic_synthesis_payment_issue_category():
    """Verifies that billing/payment keywords map to payment_issue."""
    messages = [
        {"role": "CUSTOMER", "content": "I was double charged on my card for invoice INV-2026-999. Please refund me."}
    ]
    result = _heuristic_extract(messages, evidence_ids=[])

    assert result.issue_category == "payment_issue"
    assert any(i["type"] == "INVOICE_NUMBER" for i in result.reference_identifiers)


def test_heuristic_synthesis_damaged_product_category():
    """Verifies that damage keywords map to damaged_product."""
    messages = [
        {"role": "CUSTOMER", "content": "The headphones arrived broken and the headband is cracked."}
    ]
    result = _heuristic_extract(messages, evidence_ids=[])
    assert result.issue_category == "damaged_product"


def test_heuristic_synthesis_pii_masked():
    """Verifies that credit cards and emails are masked in problem statement."""
    messages = [
        {"role": "CUSTOMER", "content": "My email is secret@company.com and card is 4111 1111 1111 1111. It failed."}
    ]
    result = _heuristic_extract(messages, evidence_ids=[])
    assert "secret@company.com" not in result.problem_statement
    assert "4111 1111 1111" not in result.problem_statement
    assert "****-****-****-1111" in result.problem_statement


# ==============================================================================
# 2. LLM SYNTHESIS & FALLBACK UNIT TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_llm_synthesis_successful_parsing():
    """Verifies that well-formed LLM JSON output is parsed into structured fields."""
    mock_json = """{
      "problem_statement": "Customer received a defective keyboard with non-working spacebar.",
      "product_or_service": "Mechanical Keyboard Pro",
      "reference_identifiers": [{"type": "ORDER_ID", "value": "ORD-11223"}],
      "issue_category": "damaged_product",
      "issue_subcategory": "hardware_defect",
      "resolution_sought": "replacement",
      "actions_already_attempted": ["tested on different USB ports", "restarted PC"],
      "missing_fields": [],
      "contradictions": [],
      "conversation_context_summary": "Customer reported spacebar issue after unboxing.",
      "extraction_confidence": 0.95,
      "extraction_provenance": {
        "problem_statement": "explicit",
        "product_or_service": "explicit",
        "issue_category": "inferred"
      }
    }"""
    mock_gateway = AsyncMock()
    mock_gateway.generate.return_value = LLMResponse(
        content=mock_json,
        provider="gemini",
        model="gemini-3.8-flash",
    )

    engine = TicketSynthesisEngine(llm_gateway=mock_gateway)
    req = SynthesisRequest(
        conversation_id="conv-1",
        ticket_id="tck-1",
        messages=[{"role": "CUSTOMER", "content": "My keyboard spacebar does not work. Order ORD-11223."}],
    )
    res = await engine.synthesize(req)

    assert res.problem_statement == "Customer received a defective keyboard with non-working spacebar."
    assert res.product_or_service == "Mechanical Keyboard Pro"
    assert res.issue_category == "damaged_product"
    assert res.resolution_sought == "replacement"
    assert len(res.actions_already_attempted) == 2
    assert res.extraction_provenance["problem_statement"] == "explicit"
    assert res.llm_model_used == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_llm_synthesis_markdown_code_fences_stripped():
    """Verifies that markdown fences ```json ... ``` are cleanly stripped."""
    mock_json = """```json
    {
      "problem_statement": "Account locked out after password reset.",
      "product_or_service": null,
      "reference_identifiers": [],
      "issue_category": "account_issue",
      "issue_subcategory": "login_failure",
      "resolution_sought": "unlock account",
      "actions_already_attempted": [],
      "missing_fields": [],
      "contradictions": [],
      "conversation_context_summary": "Customer unable to log in.",
      "extraction_confidence": 0.9,
      "extraction_provenance": {"problem_statement": "explicit"}
    }
    ```"""
    mock_gateway = AsyncMock()
    mock_gateway.generate.return_value = LLMResponse(
        content=mock_json,
        provider="gemini",
        model="gemini-3.8-flash",
    )

    engine = TicketSynthesisEngine(llm_gateway=mock_gateway)
    req = SynthesisRequest(
        conversation_id="conv-2",
        ticket_id="tck-2",
        messages=[{"role": "CUSTOMER", "content": "I am locked out of my account."}],
    )
    res = await engine.synthesize(req)
    assert res.issue_category == "account_issue"
    assert res.resolution_sought == "unlock account"


@pytest.mark.asyncio
async def test_llm_synthesis_fallback_on_malformed_json():
    """Verifies that malformed JSON from LLM gracefully triggers heuristic fallback."""
    mock_gateway = AsyncMock()
    mock_gateway.generate.return_value = LLMResponse(
        content="I am an LLM and I forgot to output JSON: The customer has an issue with ORD-77665.",
        provider="gemini",
        model="gemini-3.8-flash",
    )

    engine = TicketSynthesisEngine(llm_gateway=mock_gateway)
    req = SynthesisRequest(
        conversation_id="conv-3",
        ticket_id="tck-3",
        messages=[{"role": "CUSTOMER", "content": "I have an issue with order ORD-77665 delivery."}],
    )
    res = await engine.synthesize(req)

    # Heuristic fallback succeeded
    assert res.llm_model_used == "fallback_heuristic"
    assert any(i["value"] == "ORD-77665" for i in res.reference_identifiers)


@pytest.mark.asyncio
async def test_llm_synthesis_fallback_on_gateway_exception():
    """Verifies that LLM gateway exceptions gracefully trigger heuristic fallback."""
    mock_gateway = AsyncMock()
    mock_gateway.generate.side_effect = TimeoutError("Connection to LLM timed out.")

    engine = TicketSynthesisEngine(llm_gateway=mock_gateway)
    req = SynthesisRequest(
        conversation_id="conv-4",
        ticket_id="tck-4",
        messages=[{"role": "CUSTOMER", "content": "My package with tracking TRK-12345678 is missing."}],
    )
    res = await engine.synthesize(req)

    assert res.llm_model_used == "fallback_heuristic"
    assert any(i["value"] == "TRK-12345678" for i in res.reference_identifiers)


# ==============================================================================
# 3. DETERMINISTIC COMPLETENESS EVALUATOR TESTS
# ==============================================================================

def test_completeness_delivery_issue_missing_order():
    """Verifies delivery issue without order or tracking fails completeness."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "delivery_issue",
        "problem_statement": "My delivery has not arrived yet after 3 days.",
        "reference_identifiers": [],
    }
    result = evaluator.evaluate(synthesis)

    assert not result.is_complete
    assert result.completeness_score < 1.0
    assert "order_id_or_tracking" in result.missing_mandatory_fields
    assert any(v.field_name == "order_id_or_tracking" and v.is_blocker for v in result.violations)


def test_completeness_delivery_issue_complete():
    """Verifies delivery issue with order ID and description passes completeness."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "delivery_issue",
        "problem_statement": "My delivery has not arrived yet after 3 days.",
        "reference_identifiers": [{"type": "ORDER_ID", "value": "ORD-12345"}],
    }
    result = evaluator.evaluate(synthesis)

    assert result.is_complete
    assert result.completeness_score == 1.0
    assert not result.missing_mandatory_fields
    assert "order_id_or_tracking" in result.already_provided_fields


def test_completeness_payment_issue_missing_resolution():
    """Verifies payment issue requires resolution_sought (e.g. refund, reversal)."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "payment_issue",
        "problem_statement": "I was charged twice on my card for this transaction.",
        "reference_identifiers": [{"type": "INVOICE_NUMBER", "value": "INV-100"}],
        "resolution_sought": None,
    }
    result = evaluator.evaluate(synthesis)

    assert not result.is_complete
    assert "resolution_sought" in result.missing_mandatory_fields


def test_completeness_damaged_product_requires_product():
    """Verifies damaged product requires product_or_service identification."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "damaged_product",
        "problem_statement": "The item arrived completely shattered inside the box.",
        "product_or_service": None,
    }
    result = evaluator.evaluate(synthesis)

    assert not result.is_complete
    assert "product_or_service" in result.missing_mandatory_fields


def test_completeness_account_issue_complete():
    """Verifies account issue is complete with just a clear problem description."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "account_issue",
        "problem_statement": "I cannot reset my password because the link email never arrives.",
    }
    result = evaluator.evaluate(synthesis)

    assert result.is_complete
    assert result.completeness_score == 1.0


def test_completeness_contradiction_detection():
    """Verifies contradictions are surfaced in reasons without being treated as missing fields."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "delivery_issue",
        "problem_statement": "My order has not arrived.",
        "reference_identifiers": [{"type": "ORDER_ID", "value": "ORD-111"}],
        "contradictions": [
            {"field": "order_id", "description": "Customer mentioned ORD-111 in message 1 but ORD-222 in message 3."}
        ],
    }
    result = evaluator.evaluate(synthesis)

    assert len(result.contradictions_detected) == 1
    assert "ORD-111" in result.contradictions_detected[0]


def test_completeness_score_not_proof_of_ticket_resolution():
    """Invariant test: completeness_score=1.0 means all mandatory info is present, NOT that ticket is resolved."""
    evaluator = TicketCompletenessEvaluator()
    synthesis = {
        "issue_category": "general",
        "problem_statement": "I am experiencing unexpected behavior in the desktop application.",
    }
    result = evaluator.evaluate(synthesis)
    assert result.is_complete
    assert result.completeness_score == 1.0
    # The result has no resolution state — it only reflects information sufficiency


# ==============================================================================
# 4. CLARIFICATION ENGINE UNIT TESTS
# ==============================================================================

def test_clarification_generates_targeted_questions():
    """Verifies that missing fields generate targeted natural language questions."""
    engine = TicketClarificationEngine()
    completeness = {
        "issue_category": "delivery_issue",
        "completeness_score": 0.5,
        "is_complete": False,
        "mandatory_fields_total": 2,
        "mandatory_fields_present": 1,
        "missing_mandatory_fields": ["order_id_or_tracking"],
        "missing_optional_fields": [],
        "contradictions_detected": [],
        "reasons": ["Missing order_id_or_tracking"],
        "already_provided_fields": ["problem_statement"],
    }
    req = ClarificationRoundRequest(
        ticket_id="tck-1",
        synthesis_data={"issue_category": "delivery_issue"},
        completeness_result=completeness,
        current_round_number=1,
    )
    result = engine.generate_clarification_round(req)

    assert result.should_ask
    assert len(result.questions) == 1
    assert result.questions[0].field_name == "order_id_or_tracking"
    assert "order number or tracking number" in result.questions[0].question


def test_clarification_skips_when_complete():
    """Verifies that complete tickets produce should_ask=False."""
    engine = TicketClarificationEngine()
    completeness = {
        "issue_category": "delivery_issue",
        "completeness_score": 1.0,
        "is_complete": True,
        "mandatory_fields_total": 2,
        "mandatory_fields_present": 2,
        "missing_mandatory_fields": [],
        "missing_optional_fields": [],
        "contradictions_detected": [],
        "reasons": [],
        "already_provided_fields": ["order_id_or_tracking", "problem_statement"],
    }
    req = ClarificationRoundRequest(
        ticket_id="tck-1",
        synthesis_data={"issue_category": "delivery_issue"},
        completeness_result=completeness,
    )
    result = engine.generate_clarification_round(req)

    assert not result.should_ask
    assert "already has sufficient information" in (result.stop_reason or "")


def test_clarification_duplicate_question_prevention():
    """Verifies that questions asked in round 1 are NOT repeated in round 2."""
    engine = TicketClarificationEngine()
    completeness = {
        "issue_category": "delivery_issue",
        "completeness_score": 0.5,
        "is_complete": False,
        "mandatory_fields_total": 2,
        "mandatory_fields_present": 1,
        "missing_mandatory_fields": ["order_id_or_tracking"],
        "missing_optional_fields": [],
        "contradictions_detected": [],
        "reasons": [],
        "already_provided_fields": ["problem_statement"],
    }
    # Round 1 already asked for order_id_or_tracking
    prior_rounds = [
        {
            "questions": [{"field_name": "order_id_or_tracking", "question": "What is your order number?"}],
            "targeted_missing_fields": ["order_id_or_tracking"],
            "status": "ANSWERED",
            "customer_response": "I cannot find it.",
        }
    ]
    req = ClarificationRoundRequest(
        ticket_id="tck-1",
        synthesis_data={"issue_category": "delivery_issue"},
        completeness_result=completeness,
        prior_rounds=prior_rounds,
        current_round_number=2,
    )
    result = engine.generate_clarification_round(req)

    # Should NOT repeat the question
    assert not result.should_ask
    assert "already asked" in (result.stop_reason or "")


def test_clarification_max_rounds_ceiling():
    """Verifies that round number > MAX_CLARIFICATION_ROUNDS stops asking."""
    engine = TicketClarificationEngine()
    completeness = {
        "issue_category": "delivery_issue",
        "completeness_score": 0.5,
        "is_complete": False,
        "mandatory_fields_total": 2,
        "mandatory_fields_present": 1,
        "missing_mandatory_fields": ["order_id_or_tracking"],
        "missing_optional_fields": [],
        "contradictions_detected": [],
        "reasons": [],
        "already_provided_fields": [],
    }
    req = ClarificationRoundRequest(
        ticket_id="tck-1",
        synthesis_data={"issue_category": "delivery_issue"},
        completeness_result=completeness,
        current_round_number=6,  # > 5
    )
    result = engine.generate_clarification_round(req)

    assert not result.should_ask
    assert "Maximum clarification rounds" in (result.stop_reason or "")


def test_clarification_absorbs_customer_response():
    """Verifies that customer response provides order ID, updating completeness to 1.0."""
    engine = TicketClarificationEngine()
    synthesis_data = {
        "issue_category": "delivery_issue",
        "problem_statement": "My delivery is missing.",
        "reference_identifiers": [],
        "missing_fields": ["order_id_or_tracking"],
        "completeness_score": 0.5,
    }
    result = engine.process_customer_response(
        synthesis_data=synthesis_data,
        customer_response="My order number is ORD-44332.",
        targeted_missing_fields=["order_id_or_tracking"],
        current_round_number=1,
    )

    assert result.response_absorbed
    assert not result.ambiguous_response
    assert any(i["value"] == "ORD-44332" for i in result.updated_synthesis_data["reference_identifiers"])
    assert result.new_completeness["is_complete"]
    assert result.new_completeness["completeness_score"] == 1.0
    assert not result.should_continue_clarification


def test_clarification_ambiguous_customer_response():
    """Verifies that non-informative responses like '.' or 'idk' are flagged and not absorbed."""
    engine = TicketClarificationEngine()
    synthesis_data = {
        "issue_category": "delivery_issue",
        "problem_statement": "My delivery is missing.",
        "reference_identifiers": [],
        "missing_fields": ["order_id_or_tracking"],
        "completeness_score": 0.5,
    }
    result = engine.process_customer_response(
        synthesis_data=synthesis_data,
        customer_response="idk",
        targeted_missing_fields=["order_id_or_tracking"],
        current_round_number=1,
    )

    assert not result.response_absorbed
    assert result.ambiguous_response
    # Completeness still incomplete
    assert not result.new_completeness["is_complete"]


# ==============================================================================
# 5. SERVICE INTEGRATION & LIFECYCLE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_service_synthesize_ticket_lifecycle(session: AsyncSession, seeded_ticket_context):
    """Verifies full synthesis lifecycle: DRAFT -> SYNTHESIZING -> SYNTHESIZED -> READY_FOR_REVIEW."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    result = await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    assert result["ticket_id"] == str(ticket.id)
    assert result["synthesis_status"] in ("READY_FOR_REVIEW", "NEEDS_INFORMATION")

    # Verify DB persistence
    reloaded_ticket = (await session.execute(
        select(Ticket).where(Ticket.id == ticket.id).options(selectinload(Ticket.ticket_synthesis))
    )).scalar_one()

    assert reloaded_ticket.ticket_synthesis is not None
    assert reloaded_ticket.ticket_synthesis.problem_statement is not None
    assert reloaded_ticket.synthesis_status == result["synthesis_status"]

    # Verify audit event
    events = (await session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    event_types = [e.event_type for e in events]
    assert "TICKET_SYNTHESIZED" in event_types
    assert "SYNTHESIS_STATUS_CHANGED" in event_types


@pytest.mark.asyncio
async def test_service_conflict_without_force_flag(session: AsyncSession, seeded_ticket_context):
    """Verifies that re-synthesizing without force_resynthesis=True raises 409 conflict."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    with pytest.raises(TicketSynthesisConflictError):
        await service.synthesize_ticket(
            db=session,
            ticket_id=ticket.id,
            customer_id=str(ctx["customer_1"].id),
            force_resynthesis=False,
        )


@pytest.mark.asyncio
async def test_service_force_resynthesis_increments_version(session: AsyncSession, seeded_ticket_context):
    """Verifies force_resynthesis=True increments synthesis_version."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    res2 = await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
        force_resynthesis=True,
    )
    await session.commit()

    reloaded = (await session.execute(
        select(Ticket).where(Ticket.id == ticket.id).options(selectinload(Ticket.ticket_synthesis))
    )).scalar_one()
    assert reloaded.ticket_synthesis.synthesis_version == 2


@pytest.mark.asyncio
async def test_service_tenant_isolation(session: AsyncSession, seeded_ticket_context):
    """Verifies that Customer 2 cannot synthesize or access Customer 1's ticket."""
    ctx = seeded_ticket_context
    ticket_1 = ctx["ticket_1"]
    cust_2_id = str(ctx["customer_2"].id)
    service = get_ticket_synthesis_service()

    with pytest.raises(TicketAuthorizationError):
        await service.synthesize_ticket(
            db=session,
            ticket_id=ticket_1.id,
            customer_id=cust_2_id,  # Wrong customer!
        )


@pytest.mark.asyncio
async def test_service_clarification_round_lifecycle(session: AsyncSession, seeded_ticket_context):
    """Verifies generating and answering a clarification round."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_2"]  # Bob's payment issue ticket
    service = get_ticket_synthesis_service()

    # Step 1: Synthesize (payment issue without resolution sought -> NEEDS_INFORMATION)
    synth = await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_2"].id),
    )
    await session.commit()

    # Step 2: Generate clarification round
    clarify_res = await service.generate_clarification(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_2"].id),
    )
    await session.commit()

    assert clarify_res["should_ask"]
    assert clarify_res["round_number"] == 1
    assert len(clarify_res["questions"]) > 0

    # Load created clarification request
    cr = (await session.execute(
        select(TicketClarificationRequest).where(TicketClarificationRequest.ticket_id == ticket.id)
    )).scalars().first()
    assert cr is not None
    assert cr.status == "PENDING_CUSTOMER"

    # Step 3: Respond to clarification
    resp_res = await service.respond_to_clarification(
        db=session,
        ticket_id=ticket.id,
        clarification_request_id=cr.id,
        customer_response="I want a full refund of $50 for invoice INV-9988.",
        customer_id=str(ctx["customer_2"].id),
    )
    await session.commit()

    assert resp_res["response_absorbed"]
    # Round is now ANSWERED
    await session.refresh(cr)
    assert cr.status == "ANSWERED"
    assert cr.customer_response is not None


@pytest.mark.asyncio
async def test_service_mark_complete(session: AsyncSession, seeded_ticket_context):
    """Verifies staff can explicitly mark a ticket synthesis COMPLETE."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    result = await service.mark_complete(
        db=session,
        ticket_id=ticket.id,
    )
    await session.commit()
    assert result["synthesis_status"] == "COMPLETE"


# ==============================================================================
# 6. REST API INTEGRATION TESTS (FastAPI AsyncClient)
# ==============================================================================

@pytest.mark.asyncio
async def test_api_synthesize_ticket_customer_auth(session: AsyncSession, seeded_ticket_context):
    """Verifies POST /api/v1/tickets/synthesize with customer session token."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/tickets/synthesize",
                headers={"X-Session-Token": ctx["token_1"]},
                json={"ticket_id": str(ticket.id), "force_resynthesis": False},
            )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["ticket_id"] == str(ticket.id)
        assert "synthesis" in data
        assert "completeness" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_synthesize_cross_tenant_forbidden(session: AsyncSession, seeded_ticket_context):
    """Verifies Customer B cannot synthesize Customer A's ticket (403 Forbidden)."""
    ctx = seeded_ticket_context
    ticket_1 = ctx["ticket_1"]

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/tickets/synthesize",
                headers={"X-Session-Token": ctx["token_2"]},  # Bob trying to synthesize Alice's ticket
                json={"ticket_id": str(ticket_1.id)},
            )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_get_synthesis_details(session: AsyncSession, seeded_ticket_context):
    """Verifies GET /api/v1/tickets/{id}/synthesis returns the synthesis details."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                f"/api/v1/tickets/{ticket.id}/synthesis",
                headers={"X-Session-Token": ctx["token_1"]},
            )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["ticket_id"] == str(ticket.id)
        assert data["problem_statement"] is not None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_evaluate_completeness(session: AsyncSession, seeded_ticket_context):
    """Verifies POST /api/v1/tickets/{id}/synthesis/completeness."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                f"/api/v1/tickets/{ticket.id}/synthesis/completeness",
                headers={"X-Session-Token": ctx["token_1"]},
            )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert "completeness_score" in data
        assert "is_complete" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_generate_and_list_clarifications(session: AsyncSession, seeded_ticket_context):
    """Verifies POST /clarify and GET /clarifications endpoints."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Generate round
            clarify_resp = await ac.post(
                f"/api/v1/tickets/{ticket.id}/synthesis/clarify",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert clarify_resp.status_code == status.HTTP_200_OK

            # List rounds
            list_resp = await ac.get(
                f"/api/v1/tickets/{ticket.id}/synthesis/clarifications",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert list_resp.status_code == status.HTTP_200_OK
            rounds = list_resp.json()["rounds"]
            assert isinstance(rounds, list)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_respond_to_clarification_endpoint(session: AsyncSession, seeded_ticket_context):
    """Verifies customer submitting clarification response via API endpoint."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_2"]  # Bob's payment issue
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_2"].id),
    )
    clarify_res = await service.generate_clarification(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_2"].id),
    )
    await session.commit()

    cr = (await session.execute(
        select(TicketClarificationRequest).where(TicketClarificationRequest.ticket_id == ticket.id)
    )).scalars().first()
    assert cr is not None

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                f"/api/v1/tickets/{ticket.id}/synthesis/respond",
                headers={"X-Session-Token": ctx["token_2"]},
                json={
                    "clarification_request_id": str(cr.id),
                    "customer_response": "I would like a full refund for this charge.",
                },
            )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["response_absorbed"] is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_mark_complete_rbac(session: AsyncSession, seeded_ticket_context):
    """Verifies customer session gets 403 on mark_complete; staff JWT succeeds."""
    ctx = seeded_ticket_context
    ticket = ctx["ticket_1"]
    service = get_ticket_synthesis_service()

    await service.synthesize_ticket(
        db=session,
        ticket_id=ticket.id,
        customer_id=str(ctx["customer_1"].id),
    )
    await session.commit()

    staff_jwt = ctx["staff_jwt"]

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Customer tries -> 403 Forbidden
            cust_resp = await ac.post(
                f"/api/v1/tickets/{ticket.id}/synthesis/complete",
                headers={"X-Session-Token": ctx["token_1"]},
            )
            assert cust_resp.status_code == status.HTTP_403_FORBIDDEN

            # Staff tries -> 200 OK
            staff_resp = await ac.post(
                f"/api/v1/tickets/{ticket.id}/synthesis/complete",
                headers={"Authorization": f"Bearer {staff_jwt}"},
            )
            assert staff_resp.status_code == status.HTTP_200_OK
            assert staff_resp.json()["synthesis_status"] == "COMPLETE"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_ticket_not_found(session: AsyncSession, seeded_ticket_context):
    """Verifies that non-existent ticket IDs return 404."""
    ctx = seeded_ticket_context
    fake_id = uuid.uuid4()

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                f"/api/v1/tickets/{fake_id}/synthesis",
                headers={"X-Session-Token": ctx["token_1"]},
            )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.clear()

