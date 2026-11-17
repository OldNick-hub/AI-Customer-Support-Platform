"""Tests for Deterministic Customer Corrections & Authoritative Slot Updates (Task 6 Criterion 10).

Verifies:
1. Slot extraction captures explicit order_id, tracking_number, and invoice_number entities.
2. Explicit correction utterances ('not X, but Y', 'instead of X, use Y', 'actually Y, not X')
   update active slot values and append superseded values to history.
3. Chronological conversation processing ensures the latest correction wins authoritatively.
4. TicketSynthesisEngine resolves reference identifiers using authoritative slots,
   preventing contradictory/stale identifiers and logging clear audit descriptions in contradictions.
5. MultilingualOrchestrator detects customer corrections and surfaces active slots in turn results.
"""

from datetime import date
from unittest.mock import MagicMock
import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import ResolvedAuthContext
from app.db.base import Base
from app.models.customer import Conversation, Customer, Session as CustomerSession
from app.modules.multilingual.correction_handler import SlotCorrectionEngine, SlotValue
from app.modules.multilingual.orchestrator import MultilingualOrchestrator
from app.modules.rag.orchestrator import RAGOrchestrator
from app.modules.ticketing.ticket_synthesis import SynthesisRequest, TicketSynthesisEngine


@pytest.fixture
def engine() -> SlotCorrectionEngine:
    return SlotCorrectionEngine()


def test_slot_extraction(engine: SlotCorrectionEngine):
    """Verifies that candidate slots are extracted from standard text."""
    slots = engine.extract_slots("My order id is ORD-7777 and tracking number is TRK-9999")
    assert slots.get("order_id") == "ORD-7777"
    assert slots.get("tracking_number") == "TRK-9999"


def test_detect_correction_not_x_but_y(engine: SlotCorrectionEngine):
    """Verifies correction pattern: 'not X, but Y'."""
    text = "Wait, not ORD-1234, but ORD-9999"
    corr = engine.detect_correction(text)
    assert corr.has_correction is True
    assert corr.old_value == "ORD-1234"
    assert corr.new_value == "ORD-9999"


def test_detect_correction_instead_of_x(engine: SlotCorrectionEngine):
    """Verifies correction pattern: 'instead of X, use Y'."""
    text = "Please use tracking TRK-8888 instead of TRK-1111"
    corr = engine.detect_correction(text)
    assert corr.has_correction is True


def test_process_turn_updates_slot_history(engine: SlotCorrectionEngine):
    """Verifies that process_turn preserves superseded history when a correction occurs."""
    # Turn 1: Customer provides initial order
    slots, corr1 = engine.process_turn("My order is ORD-1001")
    assert corr1 is None
    assert "order_id" in slots
    assert slots["order_id"].current_value == "ORD-1001"
    assert slots["order_id"].superseded_values == []
    assert slots["order_id"].is_corrected is False

    # Turn 2: Customer provides correction
    updated_slots, corr2 = engine.process_turn("Actually, my order is ORD-2002, not ORD-1001", slots)
    assert corr2 is not None
    assert corr2.has_correction is True
    assert updated_slots["order_id"].current_value == "ORD-2002"
    assert "ORD-1001" in updated_slots["order_id"].superseded_values
    assert updated_slots["order_id"].is_corrected is True


def test_process_conversation_messages_chronological(engine: SlotCorrectionEngine):
    """Verifies that an ordered list of messages is processed chronologically."""
    messages = [
        {"role": "CUSTOMER", "content": "Hello, I am inquiring about order ORD-4004"},
        {"role": "AGENT", "content": "Let me look into order ORD-4004 for you."},
        {"role": "CUSTOMER", "content": "Sorry, I made a typo! Not ORD-4004, but ORD-5005."},
    ]
    slots = engine.process_conversation_messages(messages)
    assert "order_id" in slots
    assert slots["order_id"].current_value == "ORD-5005"
    assert "ORD-4004" in slots["order_id"].superseded_values


def test_resolve_authoritative_identifiers(engine: SlotCorrectionEngine):
    """Verifies that raw identifiers are reconciled against slots, removing stale values."""
    raw_identifiers = [
        {"type": "ORDER_ID", "value": "ORD-4004"},
        {"type": "ORDER_ID", "value": "ORD-5005"},
    ]
    slots = {
        "order_id": SlotValue(
            slot_name="order_id",
            current_value="ORD-5005",
            superseded_values=["ORD-4004"],
            is_corrected=True,
        )
    }
    clean_ids, contradictions = engine.resolve_authoritative_identifiers(raw_identifiers, slots)

    # Must retain ONLY authoritative ORD-5005
    assert len(clean_ids) == 1
    assert clean_ids[0]["value"] == "ORD-5005"

    # Must note contradiction/correction in audit log
    assert len(contradictions) == 1
    assert "ORD-4004" in contradictions[0]["description"]
    assert "ORD-5005" in contradictions[0]["description"]


@pytest.mark.asyncio
async def test_ticket_synthesis_with_customer_correction():
    """Verifies that TicketSynthesisEngine resolves customer corrections into authoritative identifiers."""
    messages = [
        {"role": "CUSTOMER", "content": "I ordered a monitor under ORD-1111 but it was broken."},
        {"role": "CUSTOMER", "content": "Wait, my order is ORD-9999, not ORD-1111."},
    ]
    synthesis_engine = TicketSynthesisEngine()
    req = SynthesisRequest(
        conversation_id=str(uuid.uuid4()),
        ticket_id=str(uuid.uuid4()),
        messages=messages,
    )

    result = await synthesis_engine.synthesize(req)

    # Must contain only authoritative ORD-9999
    order_vals = [i["value"] for i in result.reference_identifiers if i["type"] == "ORDER_ID"]
    assert "ORD-9999" in order_vals
    assert "ORD-1111" not in order_vals

    # Must record the customer revision in contradictions
    assert any("ORD-1111" in c.get("description", "") for c in result.contradictions)


@pytest.mark.asyncio
async def test_orchestrator_surfaces_slots_and_corrections(engine: SlotCorrectionEngine):
    """Verifies that MultilingualOrchestrator tracks slots and surfaces correction flags in turn response."""
    sqlite_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        cust_id = uuid.uuid4()
        sess_id = uuid.uuid4()
        conv_id = uuid.uuid4()

        cust = Customer(id=cust_id, email="slot@example.com", full_name="Slot Tester")
        sess_obj = CustomerSession(
            id=sess_id,
            customer_id=cust_id,
            session_token="slot-session-token",
            expires_at=date(2026, 12, 31),
        )
        conv = Conversation(id=conv_id, session_id=sess_id, customer_id=cust_id, status="OPEN")
        session.add_all([cust, sess_obj, conv])
        await session.commit()

        mock_rag = MagicMock(spec=RAGOrchestrator)

        async def mock_rag_proc(request, session):
            m = MagicMock()
            m.answer = f"Details for {request.query}"
            m.citations = []
            m.confidence_score = 1.0
            m.is_resolved = True
            m.refusal_triggered = False
            m.refusal_reason = None
            m.claim_verification = MagicMock(total_claims=0, verified_claims=0, all_claims_verified=True)
            return m

        mock_rag.process_query = mock_rag_proc

        orchestrator = MultilingualOrchestrator(
            session_factory=session_factory,
            rag_orchestrator=mock_rag,
            slot_correction_engine=engine,
        )

        auth_ctx = ResolvedAuthContext(
            auth_type="SESSION_TOKEN",
            customer_id=str(cust_id),
            session_token="slot-session-token",
            roles=["CUSTOMER"],
        )

        # Turn with correction
        turn_result = await orchestrator.process_chat_turn(
            session=session,
            customer_id=cust_id,
            session_id=sess_id,
            conversation_id=conv_id,
            message="Wait, not ORD-1234, but ORD-8888",
            preferred_language="en",
            auth_context=auth_ctx,
        )

        assert turn_result.get("customer_correction_detected") is True
        active_slots = turn_result.get("active_slots", {})
        assert active_slots.get("order_id") == "ORD-8888"

    await sqlite_engine.dispose()

