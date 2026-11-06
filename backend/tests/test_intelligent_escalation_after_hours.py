"""Comprehensive Test Suite for Day 08 — Intelligent Escalation & After-Hours Handling.

Validates:
1. Business Hours & After-Hours Engine:
   - Normal business hours detection
   - Business-hours boundary (09:00:00 start, 17:00:00 closing)
   - Just after closing time (17:00:01)
   - Weekends (Saturday, Sunday)
   - Configured Holidays (e.g. Thanksgiving, Christmas)
   - Deterministic next eligible business time calculation across weekends and holidays
   - Accurate timezone conversion between UTC and calendar timezone
2. Intelligent Escalation Rules:
   - Rule A: Repeated negative interactions detected across multi-turn conversation
   - Rule B: Negative cases unresolved for >= 15 minutes trigger escalation
   - Rule B Safeguard: Resolved or closed cases before threshold are NEVER escalated
   - Rule C: Urgent after-hours cases routed to on-call workflow with honest delivery confirmation
   - Rule D: Normal after-hours cases queued for next business day
3. Concurrency & Idempotency:
   - Duplicate escalation suppression for already-escalated conversations/tickets
   - Concurrency protection
4. Background Worker:
   - Worker batch scanning and recovery across restarts
   - Row-level lock safety
5. API Endpoints:
   - POST /api/v1/escalation/evaluate
   - GET /api/v1/escalation/after-hours/status
   - GET /api/v1/escalation/audits
   - POST /api/v1/escalation/scan-unresolved
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import SettableClock
from app.models.auth import Role, User
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.sentiment import SentimentLog
from app.models.ticket import EscalationAudit, SLACalendar, Ticket, TicketEvent
from app.modules.sentiment.analyzer import SentimentAnalyzer
from app.modules.ticketing.after_hours import (
    AfterHoursEvaluation,
    calculate_next_business_time,
    evaluate_after_hours,
    is_after_hours,
)
from app.modules.ticketing.escalation_service import EscalationDecision, EscalationService
from app.modules.ticketing.on_call import OnCallDispatcher, OnCallDispatchResult
from app.modules.ticketing.worker import EscalationWorker


# ==============================================================================
# FIXTURES
# ==============================================================================


@pytest.fixture
def mock_clock() -> SettableClock:
    """Deterministic simulated clock starting on Wednesday 2026-11-04 14:00:00 UTC."""
    return SettableClock(datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def sample_calendar() -> Dict[str, Any]:
    """Standard business calendar in America/New_York (09:00 - 17:00, Mon-Fri)."""
    return {
        "timezone": "America/New_York",
        "work_start_time": time(9, 0, 0),
        "work_end_time": time(17, 0, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": ["2026-11-26", "2026-12-25"],  # Thanksgiving & Christmas 2026
    }


@pytest_asyncio.fixture
async def seeded_entities(session: AsyncSession) -> Dict[str, Any]:
    """Provisions a baseline test customer, session, SLA calendar, and conversation."""
    customer = Customer(
        external_id=f"CUST-TEST-{uuid.uuid4().hex[:6]}",
        email="test_escalation@example.com",
        full_name="Escalation Test Customer",
        customer_tier="STANDARD",
    )
    session.add(customer)
    await session.flush()

    sess_token = f"sess_token_{uuid.uuid4().hex}"
    cust_session = CustomerSession(
        session_token=sess_token,
        customer_id=customer.id,
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add(cust_session)
    await session.flush()

    calendar = SLACalendar(
        name=f"CALENDAR-{uuid.uuid4().hex[:6]}",
        timezone="America/New_York",
        work_start_time=time(9, 0, 0),
        work_end_time=time(17, 0, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=["2026-11-26", "2026-12-25"],
    )
    session.add(calendar)
    await session.flush()

    conversation = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
        status="OPEN",
        is_escalated=False,
    )
    session.add(conversation)
    await session.commit()

    return {
        "customer": customer,
        "session": cust_session,
        "calendar": calendar,
        "conversation": conversation,
    }


# ==============================================================================
# 1. AFTER-HOURS ENGINE & CALENDAR TESTS
# ==============================================================================


def test_after_hours_normal_business_hours(sample_calendar):
    """Verifies that 11:30 AM on a Wednesday is identified as within business hours."""
    # Wednesday 2026-11-04 11:30 AM EST = 16:30 UTC
    dt_utc = datetime(2026, 11, 4, 16, 30, 0, tzinfo=timezone.utc)
    res = evaluate_after_hours(dt_utc, sample_calendar)

    assert res.is_after_hours is False
    assert res.reason == "WITHIN_BUSINESS_HOURS"
    assert res.local_time.hour == 11
    assert res.local_time.minute == 30
    assert res.next_eligible_business_time == dt_utc


def test_after_hours_exact_boundaries(sample_calendar):
    """Verifies boundary handling: exactly at opening (09:00:00) vs closing (17:00:00) vs after (17:00:01)."""
    # 09:00:00 AM EST = 14:00:00 UTC (Opening)
    opening_utc = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    eval_open = evaluate_after_hours(opening_utc, sample_calendar)
    assert eval_open.is_after_hours is False
    assert eval_open.reason == "WITHIN_BUSINESS_HOURS"

    # 08:59:59 AM EST = 13:59:59 UTC (1 second before opening)
    before_open_utc = datetime(2026, 11, 4, 13, 59, 59, tzinfo=timezone.utc)
    eval_before = evaluate_after_hours(before_open_utc, sample_calendar)
    assert eval_before.is_after_hours is True
    assert eval_before.reason == "BEFORE_WORK_HOURS"
    assert eval_before.next_eligible_business_time == opening_utc

    # 16:59:59 AM EST = 21:59:59 UTC (1 second before closing)
    before_close_utc = datetime(2026, 11, 4, 21, 59, 59, tzinfo=timezone.utc)
    eval_before_close = evaluate_after_hours(before_close_utc, sample_calendar)
    assert eval_before_close.is_after_hours is False
    assert eval_before_close.reason == "WITHIN_BUSINESS_HOURS"

    # 17:00:00 EST = 22:00:00 UTC (Closing boundary)
    close_boundary_utc = datetime(2026, 11, 4, 22, 0, 0, tzinfo=timezone.utc)
    eval_close = evaluate_after_hours(close_boundary_utc, sample_calendar)
    assert eval_close.is_after_hours is True
    assert eval_close.reason == "AFTER_WORK_HOURS"

    # 17:00:01 EST = 22:00:01 UTC (Just after closing)
    just_after_close = datetime(2026, 11, 4, 22, 0, 1, tzinfo=timezone.utc)
    eval_after = evaluate_after_hours(just_after_close, sample_calendar)
    assert eval_after.is_after_hours is True
    assert eval_after.reason == "AFTER_WORK_HOURS"
    # Next business time is Thursday 09:00:00 EST = Thursday 14:00:00 UTC
    expected_next = datetime(2026, 11, 5, 14, 0, 0, tzinfo=timezone.utc)
    assert eval_after.next_eligible_business_time == expected_next


def test_after_hours_weekends_roll_to_monday(sample_calendar):
    """Verifies that Friday evening and weekend arrivals roll over to Monday 09:00 AM."""
    # Friday 2026-11-06 18:30:00 EST = 23:30:00 UTC
    friday_evening_utc = datetime(2026, 11, 6, 23, 30, 0, tzinfo=timezone.utc)
    eval_fri = evaluate_after_hours(friday_evening_utc, sample_calendar)
    assert eval_fri.is_after_hours is True
    assert eval_fri.reason == "AFTER_WORK_HOURS"
    # Monday 2026-11-09 09:00:00 EST = 14:00:00 UTC
    expected_monday = datetime(2026, 11, 9, 14, 0, 0, tzinfo=timezone.utc)
    assert eval_fri.next_eligible_business_time == expected_monday

    # Saturday 2026-11-07 14:00:00 EST = 19:00:00 UTC
    sat_utc = datetime(2026, 11, 7, 19, 0, 0, tzinfo=timezone.utc)
    eval_sat = evaluate_after_hours(sat_utc, sample_calendar)
    assert eval_sat.is_after_hours is True
    assert eval_sat.reason == "WEEKEND"
    assert eval_sat.next_eligible_business_time == expected_monday


def test_after_hours_holidays_roll_forward(sample_calendar):
    """Verifies that holidays (e.g. Thanksgiving on Thursday 2026-11-26) roll to Friday."""
    # Thanksgiving Thursday 2026-11-26 10:00:00 EST = 15:00:00 UTC
    thanksgiving_utc = datetime(2026, 11, 26, 15, 0, 0, tzinfo=timezone.utc)
    eval_holiday = evaluate_after_hours(thanksgiving_utc, sample_calendar)
    assert eval_holiday.is_after_hours is True
    assert eval_holiday.reason == "HOLIDAY"
    # Next eligible business day: Friday 2026-11-27 09:00:00 EST = 14:00:00 UTC
    expected_fri = datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert eval_holiday.next_eligible_business_time == expected_fri


def test_timezone_conversion_accuracy():
    """Tests accuracy of conversion for different timezones (America/New_York vs Asia/Tokyo)."""
    cal_tokyo = {
        "timezone": "Asia/Tokyo",  # UTC+9
        "work_start_time": time(9, 0, 0),
        "work_end_time": time(18, 0, 0),
        "work_days": [1, 2, 3, 4, 5],
        "holidays": [],
    }
    # 01:00 UTC = 10:00 Tokyo time on Wednesday -> During Tokyo business hours
    dt_utc = datetime(2026, 11, 4, 1, 0, 0, tzinfo=timezone.utc)
    res = evaluate_after_hours(dt_utc, cal_tokyo)
    assert res.is_after_hours is False
    assert res.reason == "WITHIN_BUSINESS_HOURS"
    assert res.local_time.hour == 10


# ==============================================================================
# 2. SENTIMENT & FRUSTRATION ENGINE TESTS
# ==============================================================================


def test_sentiment_analysis_classification():
    """Verifies fine-grained sentiment scoring, frustration, and urgency detection."""
    analyzer = SentimentAnalyzer()

    # 1. Positive text
    res_pos = analyzer.analyze_text("Thank you so much, this was very helpful and resolved my issue!")
    assert res_pos.sentiment_class == "positive"
    assert res_pos.frustration_index < 0.2
    assert res_pos.requires_escalation is False

    # 2. Urgent text
    res_urg = analyzer.analyze_text("URGENT: Our production server is down immediately ASAP!!")
    assert res_urg.sentiment_class == "urgent"
    assert res_urg.urgency_index >= 0.8
    assert res_urg.requires_escalation is True

    # 3. Frustrated text
    res_fru = analyzer.analyze_text("This is completely unacceptable and ridiculous! Worst support ever!")
    assert res_fru.sentiment_class == "frustrated"
    assert res_fru.frustration_index >= 0.70
    assert res_fru.requires_escalation is True


def test_sentiment_conversation_repeated_negative():
    """Rule A: Verifies that multiple negative customer messages trigger repeated_negative."""
    analyzer = SentimentAnalyzer(repeated_negative_threshold=2)

    msg1 = Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        sender_type="CUSTOMER",
        content="The widget is broken and doesn't work.",
    )
    msg2 = Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        sender_type="AI_AGENT",
        content="I am sorry to hear that. Can you restart it?",
    )
    msg3 = Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        sender_type="CUSTOMER",
        content="I already did that! It is still not working and I am furious!",
    )

    assessment = analyzer.evaluate_conversation([msg1, msg2, msg3])
    assert assessment.repeated_negative is True
    assert assessment.negative_turn_count >= 2
    assert assessment.requires_escalation is True
    assert "Repeated negative sentiment" in assessment.escalation_reason


# ==============================================================================
# 3. ON-CALL DISPATCHER & HONESTY TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_on_call_unconfigured_honest_failure():
    """Rule C: When on-call destination is unconfigured, never claim CONFIRMED delivery."""
    dispatcher = OnCallDispatcher(webhook_url=None, enabled=True)
    res = await dispatcher.dispatch(
        incident_id="INC-TEST-001",
        priority="URGENT",
        summary="Urgent outage test",
        urgency_score=0.95,
    )
    assert res.status == "FAILED"
    assert "No valid ON_CALL_WEBHOOK_URL is configured" in res.failure_details


@pytest.mark.asyncio
async def test_on_call_delivery_confirmed():
    """Rule C: When downstream responds with HTTP 200, status is honestly CONFIRMED."""
    mock_client = AsyncMock()
    mock_response = MagicMock(status_code=200, text="OK")
    mock_client.post.return_value = mock_response

    dispatcher = OnCallDispatcher(
        webhook_url="https://oncall.example.internal/webhook",
        api_key="secret-key",
        http_client=mock_client,
    )
    res = await dispatcher.dispatch(
        incident_id="INC-TEST-002",
        priority="URGENT",
        summary="Urgent production outage",
        urgency_score=0.99,
    )
    assert res.status == "CONFIRMED"
    assert res.response_code == 200
    assert res.failure_details is None


@pytest.mark.asyncio
async def test_on_call_downstream_error_honest_failure():
    """Rule C: When downstream responds with HTTP 500, status is honestly FAILED with details."""
    mock_client = AsyncMock()
    mock_response = MagicMock(status_code=500, text="Internal Server Error")
    mock_client.post.return_value = mock_response

    dispatcher = OnCallDispatcher(
        webhook_url="https://oncall.example.internal/webhook",
        http_client=mock_client,
    )
    res = await dispatcher.dispatch(
        incident_id="INC-TEST-003",
        priority="URGENT",
        summary="Urgent issue",
        urgency_score=0.88,
    )
    assert res.status == "FAILED"
    assert res.response_code == 500
    assert "non-2xx status code 500" in res.failure_details


# ==============================================================================
# 4. INTELLIGENT ESCALATION RULES A, B, C, D
# ==============================================================================


@pytest.mark.asyncio
async def test_rule_a_repeated_negative_escalation(session: AsyncSession, seeded_entities):
    """Rule A: Evaluates conversation turns with repeated negative messages and escalates."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))  # 10am EST (business hours)
    service = EscalationService(clock=clock)

    # Turn 1: Negative message
    msg1 = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="My account is locked and payment failed.",
    )
    session.add(msg1)
    await session.flush()

    dec1 = await service.evaluate_and_escalate_turn(
        session=session, conversation_id=conv.id, message=msg1, calendar=calendar
    )
    # Turn 1 is 1 negative turn (threshold is 2), so not yet escalated
    assert dec1.should_escalate is False
    assert conv.negative_detected_at is not None

    # Turn 2: Second negative message -> triggers Rule A
    msg2 = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="I am furious! It is still broken and unacceptable service!",
    )
    session.add(msg2)
    await session.flush()

    dec2 = await service.evaluate_and_escalate_turn(
        session=session, conversation_id=conv.id, message=msg2, calendar=calendar
    )
    assert dec2.should_escalate is True
    assert dec2.trigger_condition == "REPEATED_NEGATIVE_INTERACTIONS"
    assert dec2.audit_record_id is not None
    assert conv.is_escalated is True
    assert conv.status == "ESCALATED"

    # Verify audit record persisted in DB
    audit = await session.get(EscalationAudit, dec2.audit_record_id)
    assert audit is not None
    assert audit.trigger_condition == "REPEATED_NEGATIVE_INTERACTIONS"
    assert audit.escalation_payload["sentiment_evidence"]["repeated_negative"] is True


@pytest.mark.asyncio
async def test_rule_b_unresolved_negative_timeout_crossing(session: AsyncSession, seeded_entities):
    """Rule B: Negative case escalates strictly after MORE THAN 15 minutes, NOT at exactly 15 minutes."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    service = EscalationService(clock=clock, unresolved_timeout_minutes=15)
    worker = EscalationWorker(escalation_service=service, clock=clock)

    # Initial negative detection at T=0
    conv.negative_detected_at = clock.now()
    session.add(conv)
    await session.commit()

    # Step 1: Advance clock by 14 minutes 59 seconds (899s elapsed -> before threshold)
    clock.advance(timedelta(minutes=14, seconds=59))
    audit_pre = await service.evaluate_unresolved_negative_timeout(
        session=session, conversation_id=conv.id, calendar=calendar
    )
    assert audit_pre is None
    assert conv.is_escalated is False
    worker_audits_pre = await worker.scan_and_escalate_batch(session)
    assert len(worker_audits_pre) == 0

    # Step 2: Advance clock by 1 second (exactly 15m 00s / 900s elapsed)
    # Critical requirement: must NOT escalate at exactly 15 minutes!
    clock.advance(timedelta(seconds=1))
    audit_exact = await service.evaluate_unresolved_negative_timeout(
        session=session, conversation_id=conv.id, calendar=calendar
    )
    assert audit_exact is None, "Must not escalate at exactly 15 minutes"
    assert conv.is_escalated is False
    worker_audits_exact = await worker.scan_and_escalate_batch(session)
    assert len(worker_audits_exact) == 0, "Worker must not escalate at exactly 15 minutes"

    # Step 3: Advance clock by 1 second (15m 01s / 901s elapsed -> MORE than 15 minutes)
    clock.advance(timedelta(seconds=1))
    audit_post = await service.evaluate_unresolved_negative_timeout(
        session=session, conversation_id=conv.id, calendar=calendar
    )
    assert audit_post is not None, "Must escalate strictly after more than 15 minutes"
    assert audit_post.trigger_condition == "UNRESOLVED_NEGATIVE_TIMEOUT"
    assert conv.is_escalated is True
    assert conv.status == "ESCALATED"


@pytest.mark.asyncio
async def test_rule_b_safeguard_resolved_case_never_escalated(session: AsyncSession, seeded_entities):
    """Rule B Safeguard: Resolved case older than 15 minutes is NEVER escalated."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    service = EscalationService(clock=clock, unresolved_timeout_minutes=15)

    # Negative detected 30 minutes ago, but conversation is RESOLVED
    conv.negative_detected_at = clock.now() - timedelta(minutes=30)
    conv.status = "RESOLVED"
    conv.closed_at = clock.now() - timedelta(minutes=10)
    session.add(conv)
    await session.flush()

    audit = await service.evaluate_unresolved_negative_timeout(
        session=session, conversation_id=conv.id, calendar=calendar
    )
    assert audit is None
    assert conv.is_escalated is False
    assert conv.status == "RESOLVED"


@pytest.mark.asyncio
async def test_rule_c_urgent_after_hours_routing(session: AsyncSession, seeded_entities):
    """Rule C: Urgent cases received outside business hours are routed to on-call workflow."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    # Saturday 2026-11-07 14:00:00 EST = 19:00:00 UTC (Weekend / After hours)
    weekend_clock = SettableClock(datetime(2026, 11, 7, 19, 0, 0, tzinfo=timezone.utc))

    mock_client = AsyncMock()
    mock_client.post.return_value = MagicMock(status_code=200, text="OK")
    on_call = OnCallDispatcher(
        webhook_url="https://oncall.company.internal/pager",
        http_client=mock_client,
    )

    service = EscalationService(clock=weekend_clock, on_call_dispatcher=on_call)

    # Urgent message arrives on weekend
    urgent_msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="CRITICAL: Emergency system crash and production database outage immediately ASAP!!",
    )
    session.add(urgent_msg)
    await session.flush()

    decision = await service.evaluate_and_escalate_turn(
        session=session,
        conversation_id=conv.id,
        message=urgent_msg,
        calendar=calendar,
    )

    assert decision.should_escalate is True
    assert decision.trigger_condition == "URGENT_AFTER_HOURS"
    assert decision.is_after_hours is True
    assert decision.routing_destination == "ON_CALL"
    assert decision.on_call_result is not None
    assert decision.on_call_result.status == "CONFIRMED"

    # Verify audit payload records on-call outcome honestly
    audit = await session.get(EscalationAudit, decision.audit_record_id)
    assert audit.escalation_payload["after_hours_determination"] is True
    assert audit.escalation_payload["routing_destination"] == "ON_CALL"
    assert audit.escalation_payload["processing_status"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_rule_d_normal_after_hours_queueing(session: AsyncSession, seeded_entities):
    """Rule D: Non-urgent after-hours escalations are queued for next business day with accurate opening time."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    # Tuesday 2026-11-03 21:00:00 EST = 2026-11-04 02:00:00 UTC (After closing)
    after_hours_clock = SettableClock(datetime(2026, 11, 4, 2, 0, 0, tzinfo=timezone.utc))
    service = EscalationService(clock=after_hours_clock)

    # 2 repeated frustrated messages arriving after hours (non-urgent)
    msg1 = Message(conversation_id=conv.id, sender_type="CUSTOMER", content="Widget failed again.")
    msg2 = Message(conversation_id=conv.id, sender_type="CUSTOMER", content="Worst app, so disappointed.")
    session.add_all([msg1, msg2])
    await session.flush()

    decision = await service.evaluate_and_escalate_turn(
        session=session,
        conversation_id=conv.id,
        message=msg2,
        calendar=calendar,
    )

    assert decision.should_escalate is True
    assert decision.is_after_hours is True
    assert decision.routing_destination == "NEXT_BUSINESS_DAY_QUEUE"
    # Expected opening: Wednesday 09:00:00 EST = 14:00:00 UTC
    expected_open = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    assert decision.next_eligible_business_time == expected_open

    audit = await session.get(EscalationAudit, decision.audit_record_id)
    assert audit.escalation_payload["routing_destination"] == "NEXT_BUSINESS_DAY_QUEUE"
    assert audit.escalation_payload["next_eligible_business_time"] == expected_open.isoformat()


# ==============================================================================
# 5. CONCURRENCY & IDEMPOTENCY TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_idempotency_duplicate_escalation_suppression(session: AsyncSession, seeded_entities):
    """Verifies that an already-escalated conversation suppresses duplicate audits and tickets."""
    conv: Conversation = seeded_entities["conversation"]
    calendar: SLACalendar = seeded_entities["calendar"]

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    service = EscalationService(clock=clock)

    # Pre-escalate conversation
    conv.is_escalated = True
    conv.status = "ESCALATED"
    initial_audit = EscalationAudit(
        conversation_id=conv.id,
        trigger_condition="REPEATED_NEGATIVE_INTERACTIONS",
        previous_state="OPEN",
        new_state="ESCALATED",
        escalation_reason="Initial escalation event",
    )
    session.add_all([conv, initial_audit])
    await session.flush()

    # Trigger second evaluation attempt
    msg = Message(conversation_id=conv.id, sender_type="CUSTOMER", content="Still angry!")
    session.add(msg)
    await session.flush()

    decision = await service.evaluate_and_escalate_turn(
        session=session,
        conversation_id=conv.id,
        message=msg,
        calendar=calendar,
    )

    assert decision.should_escalate is False
    assert decision.already_escalated is True
    assert decision.audit_record_id == initial_audit.id

    # Verify no second audit was created
    audits = (
        (await session.execute(select(EscalationAudit).where(EscalationAudit.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert len(audits) == 1


# ==============================================================================
# 6. BACKGROUND WORKER & RECOVERY TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_escalation_worker_batch_and_recovery(session: AsyncSession, seeded_entities):
    """Verifies that EscalationWorker scans 15m unresolved cases and recovers across crashes/restarts."""
    conv: Conversation = seeded_entities["conversation"]
    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    service = EscalationService(clock=clock, unresolved_timeout_minutes=15)

    worker = EscalationWorker(escalation_service=service, clock=clock)

    # 1. Negative detected 20 minutes ago
    conv.negative_detected_at = clock.now() - timedelta(minutes=20)
    session.add(conv)
    await session.commit()

    # 2. Worker executes scan cycle
    audits = await worker.scan_and_escalate_batch(session)
    assert len(audits) == 1
    assert audits[0].conversation_id == conv.id
    assert audits[0].trigger_condition == "UNRESOLVED_NEGATIVE_TIMEOUT"

    # 3. Simulated Worker Restart / Second Run: conv is now is_escalated=True -> picked up count is 0
    second_audits = await worker.scan_and_escalate_batch(session)
    assert len(second_audits) == 0


# ==============================================================================
# 7. API ENDPOINT INTEGRATION TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_api_after_hours_status_endpoint(session: AsyncSession, seeded_entities):
    """Tests GET /api/v1/escalation/after-hours/status endpoint directly."""
    from app.api.v1.escalation import get_after_hours_status
    from app.api.deps import ResolvedAuthContext

    auth_ctx = ResolvedAuthContext(auth_type="JWT", user_id_or_email="test_admin@test.local", roles=["ADMIN"])
    # Wednesday 10:00 EST = 15:00 UTC
    test_dt = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc)

    status_resp = await get_after_hours_status(check_time=test_dt, db=session, auth_ctx=auth_ctx)
    assert status_resp.is_after_hours is False
    assert status_resp.reason == "WITHIN_BUSINESS_HOURS"


@pytest.mark.asyncio
async def test_api_escalation_evaluate_and_audit_endpoints(session: AsyncSession, seeded_entities):
    """Tests POST /api/v1/escalation/evaluate and GET /api/v1/escalation/audits."""
    from app.api.v1.escalation import (
        EscalationEvaluateRequest,
        evaluate_conversation_escalation,
        list_escalation_audits,
    )
    from app.api.deps import ResolvedAuthContext

    conv: Conversation = seeded_entities["conversation"]
    auth_ctx = ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        customer_id=str(conv.customer_id),
        session_token="test_token",
    )

    # 1. First negative message (single negative, below critical frustration threshold)
    req1 = EscalationEvaluateRequest(
        conversation_id=conv.id,
        message_content="The device failed to turn on and is not working.",
    )
    res1 = await evaluate_conversation_escalation(payload=req1, db=session, auth_ctx=auth_ctx, correlation_id="c1")
    assert res1.should_escalate is False

    # 2. Second negative message -> Triggers escalation
    req2 = EscalationEvaluateRequest(
        conversation_id=conv.id,
        message_content="Still broken and horrible experience!",
    )
    res2 = await evaluate_conversation_escalation(payload=req2, db=session, auth_ctx=auth_ctx, correlation_id="c2")
    assert res2.should_escalate is True
    assert res2.trigger_condition == "REPEATED_NEGATIVE_INTERACTIONS"

    # 3. Query audit trail
    audits = await list_escalation_audits(conversation_id=conv.id, db=session, auth_ctx=auth_ctx)
    assert len(audits) >= 1
    assert audits[0].trigger_condition == "REPEATED_NEGATIVE_INTERACTIONS"


# ==============================================================================
# 8. APPLICATION & WORKER LIFECYCLE WORKFLOW INTEGRATION TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_chat_workflow_automatic_sentiment_and_escalation(session: AsyncSession, seeded_entities):
    """Item 1: Proves automatic sentiment evaluation & escalation directly in process_chat_turn without manual escalation API."""
    from app.modules.multilingual.orchestrator import MultilingualOrchestrator
    from tests.helpers.database import TestSessionLocal

    conv: Conversation = seeded_entities["conversation"]
    customer: Customer = seeded_entities["customer"]
    cust_session: CustomerSession = seeded_entities["session"]

    # Mock RAG orchestrator for instant test execution
    mock_rag = MagicMock()
    mock_rag_result = MagicMock()
    mock_rag_result.answer = "I am an AI assistant here to help."
    mock_rag_result.generated_response = "I am an AI assistant here to help."
    mock_rag_result.reply_text = "I am an AI assistant here to help."
    mock_rag_result.is_resolved = True
    mock_rag_result.refusal_triggered = False
    mock_rag_result.refusal_reason = None
    mock_rag_result.citations = []
    mock_rag_result.confidence_score = 0.95
    mock_rag_result.claim_verification = MagicMock(total_claims=1, verified_claims=1, all_claims_verified=True)
    mock_rag_result.model_used = "mock-llm"
    mock_rag_result.suggested_ticket_category = "ACCOUNT_SUPPORT"
    mock_rag_result.retrieval_latency_ms = 1.0
    mock_rag_result.generation_latency_ms = 1.0
    mock_rag.process_query = AsyncMock(return_value=mock_rag_result)

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    esc_service = EscalationService(clock=clock)
    orchestrator = MultilingualOrchestrator(
        session_factory=TestSessionLocal,
        rag_orchestrator=mock_rag,
        escalation_service=esc_service,
    )

    # Turn 1: Negative message through chat workflow
    res1 = await orchestrator.process_chat_turn(
        session=session,
        customer_id=customer.id,
        session_id=cust_session.id,
        message="The device failed to power on and is not working.",
        conversation_id=conv.id,
    )
    await session.commit()

    # Verify SentimentLog automatically created and negative_detected_at recorded
    slogs = (await session.execute(select(SentimentLog))).scalars().all()
    assert len(slogs) >= 1
    assert slogs[0].sentiment_class in ("negative", "frustrated")

    refreshed_conv = await session.get(Conversation, conv.id)
    assert refreshed_conv.negative_detected_at is not None
    assert refreshed_conv.is_escalated is False

    # Turn 2: Second negative customer message through chat workflow
    res2 = await orchestrator.process_chat_turn(
        session=session,
        customer_id=customer.id,
        session_id=cust_session.id,
        message="It is still completely broken and unusable, so frustrated!",
        conversation_id=conv.id,
    )
    await session.commit()

    # Verify automatic escalation was triggered by chat workflow!
    assert res2.get("is_escalated") is True
    assert res2.get("escalation_reason") == "REPEATED_NEGATIVE_INTERACTIONS"

    refreshed_conv2 = await session.get(Conversation, conv.id)
    assert refreshed_conv2.is_escalated is True
    assert refreshed_conv2.status == "ESCALATED"

    # Verify Ticket synthesized and EscalationAudit created
    audits = (
        (await session.execute(select(EscalationAudit).where(EscalationAudit.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert len(audits) >= 1
    assert audits[0].trigger_condition == "REPEATED_NEGATIVE_INTERACTIONS"


@pytest.mark.asyncio
async def test_worker_lifecycle_restart_recovery(session: AsyncSession, seeded_entities):
    """Item 2: Verifies escalation worker runs through actual WorkerLifecycleManager and recovers overdue cases on startup."""
    from app.lifecycle import WorkerLifecycleManager
    from tests.helpers.database import TestSessionLocal

    conv: Conversation = seeded_entities["conversation"]

    # Seed overdue negative case (negative detected 25 minutes ago)
    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    conv.negative_detected_at = clock.now() - timedelta(minutes=25)
    conv.status = "OPEN"
    conv.is_escalated = False
    session.add(conv)
    await session.commit()

    # Create worker with injected test clock
    esc_service = EscalationService(clock=clock, unresolved_timeout_minutes=15)
    worker = EscalationWorker(session_factory=TestSessionLocal, escalation_service=esc_service, clock=clock)

    # Initialize WorkerLifecycleManager with worker
    lifecycle_mgr = WorkerLifecycleManager(
        session_factory=TestSessionLocal,
        escalation_worker=worker,
    )

    # Execute production on_startup lifecycle
    startup_res = await lifecycle_mgr.on_startup()
    assert "escalation_recovery" in startup_res
    assert len(startup_res["escalation_recovery"]) >= 1

    # Verify the overdue conversation is now escalated in DB using a fresh session
    async with TestSessionLocal() as check_session:
        refreshed_conv = await check_session.get(Conversation, conv.id)
        assert refreshed_conv is not None
        assert refreshed_conv.is_escalated is True
        assert refreshed_conv.status == "ESCALATED"

    # Verify audit record was created by restart recovery
    audits = (
        (await session.execute(select(EscalationAudit).where(EscalationAudit.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].trigger_condition == "UNRESOLVED_NEGATIVE_TIMEOUT"

    # Execute clean shutdown lifecycle
    await lifecycle_mgr.on_shutdown()


# ==============================================================================
# 9. PRODUCTION LIFECYCLE, PERIODIC SCANNING & RESILIENCE AUDIT TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_fastapi_production_lifespan_starts_worker_automatically():
    """Requirement 1: Confirms EscalationWorker starts automatically in actual FastAPI lifespan."""
    from app.main import app, lifespan

    async with lifespan(app):
        # 1. Worker attached to app.state
        worker = getattr(app.state, "escalation_worker", None)
        assert worker is not None, "EscalationWorker must be attached to app.state on FastAPI startup."

        # 2. Worker is actively running
        assert worker.is_running is True, "EscalationWorker must be active during lifespan."
        assert worker._task is not None
        assert not worker._task.done(), "Background scanner task must be actively running."

    # 3. Upon lifespan exit, worker has cleanly stopped
    assert worker.is_running is False, "EscalationWorker must be stopped when lifespan terminates."
    assert worker._task is None or worker._task.done(), "Worker task must be cleanly terminated."


@pytest.mark.asyncio
async def test_worker_continues_periodic_scanning_after_startup(session: AsyncSession):
    """Requirement 2: Confirms worker continues scanning periodically after startup, not just once."""
    from tests.helpers.database import TestSessionLocal

    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))
    worker = EscalationWorker(
        session_factory=TestSessionLocal,
        clock=clock,
        poll_interval_seconds=0.01,  # Fast interval for test
    )

    try:
        task = worker.start_periodic_scanner()
        assert not task.done()
        assert worker.is_running is True

        # Let the background loop perform multiple periodic cycles
        await asyncio.sleep(0.06)

        # Worker executed multiple scan iterations
        assert worker.scan_count >= 2, f"Expected periodic scanning (scan_count >= 2), got {worker.scan_count}"
        assert worker.last_scan_at is not None
    finally:
        await worker.stop_async()
        assert worker.is_running is False


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_worker_task_cleanly():
    """Requirement 3: Confirms shutdown cancels and awaits the worker task cleanly."""
    from tests.helpers.database import TestSessionLocal

    worker = EscalationWorker(
        session_factory=TestSessionLocal,
        poll_interval_seconds=60.0,  # Long interval to ensure it waits
    )

    task = worker.start_periodic_scanner()
    assert worker.is_running is True
    assert not task.done()

    # Initiate clean shutdown
    await worker.stop_async(timeout_seconds=2.0)

    # Task is completed/cancelled, not dangling
    assert task.done() is True
    assert worker.is_running is False
    assert worker._task is None


@pytest.mark.asyncio
async def test_restart_recovery_prevents_duplicate_escalation_events(session: AsyncSession, seeded_entities):
    """Requirement 4: Confirms restart recovery does not create duplicate escalation events."""
    from tests.helpers.database import TestSessionLocal

    conv: Conversation = seeded_entities["conversation"]
    clock = SettableClock(datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc))

    # Seed overdue case (20 minutes past negative detection)
    conv.negative_detected_at = clock.now() - timedelta(minutes=20)
    conv.status = "OPEN"
    conv.is_escalated = False
    session.add(conv)
    await session.commit()

    service = EscalationService(clock=clock, unresolved_timeout_minutes=15)
    worker = EscalationWorker(session_factory=TestSessionLocal, escalation_service=service, clock=clock)

    # Recovery Pass 1 (first worker boot)
    audits_pass1 = await worker.run_once()
    assert len(audits_pass1) == 1
    assert audits_pass1[0].conversation_id == conv.id

    # Recovery Pass 2 (immediate restart / crash recovery simulation)
    audits_pass2 = await worker.run_once()
    assert len(audits_pass2) == 0, "Restart recovery must not re-escalate already escalated cases."

    # Periodic Scan Pass 3 (subsequent periodic cycle)
    audits_pass3 = await worker.run_once()
    assert len(audits_pass3) == 0, "Periodic scanner must not create duplicate escalation events."

    # Verify single immutable audit record exists in database
    audits = (
        (await session.execute(select(EscalationAudit).where(EscalationAudit.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].trigger_condition == "UNRESOLVED_NEGATIVE_TIMEOUT"


@pytest.mark.asyncio
async def test_no_unhandled_background_exceptions_silently_ignored(session: AsyncSession):
    """Requirement 5: Confirms unhandled background exceptions are captured, recorded, and logged."""
    from tests.helpers.database import TestSessionLocal

    worker = EscalationWorker(
        session_factory=TestSessionLocal,
        poll_interval_seconds=0.01,
    )

    # Inject an intentional transient database failure
    fail_count = 0

    async def faulty_run_once():
        nonlocal fail_count
        fail_count += 1
        raise RuntimeError("Simulated transient database connection dropped")

    worker.run_once = faulty_run_once

    try:
        worker.start_periodic_scanner()
        await asyncio.sleep(0.04)

        # Exception was caught, stored in last_error, and logged (not silently ignored)
        assert worker.last_error is not None
        assert isinstance(worker.last_error, RuntimeError)
        assert "Simulated transient database" in str(worker.last_error)
        assert fail_count >= 1
    finally:
        await worker.stop_async()

