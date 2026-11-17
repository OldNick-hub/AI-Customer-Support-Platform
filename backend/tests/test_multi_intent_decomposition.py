"""Tests for Multi-Intent Query Decomposition & Synthesis (Task 6 Criterion 9).

Verifies:
1. Single-intent queries pass through as atomic single requests without alteration.
2. Numbered and bulleted compound messages decompose accurately into distinct sub-queries.
3. Compound inquiries connected by 'also', 'additionally', or sequential question marks decompose properly.
4. Response synthesizer generates a structured, multi-part unified response.
5. MultilingualOrchestrator dispatches sub-queries and provides coherent multi-part answers.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock
import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import ResolvedAuthContext
from app.db.base import Base
from app.models.customer import Conversation, Customer, Session as CustomerSession
from app.modules.multilingual.orchestrator import MultilingualOrchestrator
from app.modules.multilingual.query_decomposer import QueryDecomposer, SubQuery
from app.modules.rag.generator import RAGResolutionResult
from app.modules.rag.orchestrator import RAGOrchestrator


@pytest.fixture
def decomposer() -> QueryDecomposer:
    return QueryDecomposer()


def test_single_intent_passthrough(decomposer: QueryDecomposer):
    """Verifies that a standard single-intent query is not split."""
    query = "What is the warranty period for the Apex wireless mouse?"
    res = decomposer.decompose(query)
    assert res.is_multi_intent is False
    assert len(res.sub_queries) == 1
    assert res.sub_queries[0].text == query


def test_numbered_list_decomposition(decomposer: QueryDecomposer):
    """Verifies that numbered multi-question prompts decompose into distinct sub-queries."""
    query = "1. How do I return a damaged product? 2. How can I request a refund?"
    res = decomposer.decompose(query)
    assert res.is_multi_intent is True
    assert len(res.sub_queries) == 2
    assert "return a damaged product" in res.sub_queries[0].text
    assert "request a refund" in res.sub_queries[1].text


def test_multiline_bulleted_decomposition(decomposer: QueryDecomposer):
    """Verifies that multiline bullet-point queries decompose properly."""
    query = """- Can I change my delivery address for order ORD-1234?
- What is your standard shipping delivery window?"""
    res = decomposer.decompose(query)
    assert res.is_multi_intent is True
    assert len(res.sub_queries) == 2
    assert "ORD-1234" in res.sub_queries[0].text
    assert "standard shipping" in res.sub_queries[1].text


def test_connector_and_question_mark_decomposition(decomposer: QueryDecomposer):
    """Verifies that compound questions with 'also' or sequential question marks decompose."""
    query = "Where can I track my shipment? Also, what happens if I am not home during delivery?"
    res = decomposer.decompose(query)
    assert res.is_multi_intent is True
    assert len(res.sub_queries) == 2
    assert "track my shipment" in res.sub_queries[0].text
    assert "not home during delivery" in res.sub_queries[1].text


def test_synthesize_combined_response(decomposer: QueryDecomposer):
    """Verifies that multi-intent answers are combined into a structured format."""
    sub_responses = [
        (SubQuery(index=0, text="How to return shoes?"), "Shoes can be returned within 30 days unworn."),
        (SubQuery(index=1, text="Where to find tracking?"), "Tracking details are in your order history."),
    ]
    combined = decomposer.synthesize_combined_response(sub_responses)
    assert "Question 1: How to return shoes?" in combined
    assert "Shoes can be returned within 30 days" in combined
    assert "Question 2: Where to find tracking?" in combined
    assert "Tracking details are in your order history" in combined


@pytest.mark.asyncio
async def test_orchestrator_multi_intent_dispatch(decomposer: QueryDecomposer):
    """Verifies that MultilingualOrchestrator dispatches sub-queries to RAG and returns synthesized answer."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        cust_id = uuid.uuid4()
        sess_id = uuid.uuid4()
        conv_id = uuid.uuid4()

        cust = Customer(id=cust_id, email="multi@example.com", full_name="Multi Intent")
        sess_obj = CustomerSession(
            id=sess_id,
            customer_id=cust_id,
            session_token="multi-session-token",
            expires_at=date(2026, 12, 31),
        )
        conv = Conversation(id=conv_id, session_id=sess_id, customer_id=cust_id, status="OPEN")
        session.add_all([cust, sess_obj, conv])
        await session.commit()

        # Mock RAG orchestrator that answers different questions accordingly
        mock_rag = MagicMock(spec=RAGOrchestrator)

        async def mock_process_query(request, session):
            q = request.query.lower()
            if "return" in q:
                ans = "Return window is 30 days with receipt."
            elif "shipping" in q or "track" in q:
                ans = "Standard delivery takes 3 to 5 business days."
            else:
                ans = f"Generic answer for: {request.query}"

            mock_res = MagicMock()
            mock_res.answer = ans
            mock_res.citations = []
            mock_res.confidence_score = 0.95
            mock_res.is_resolved = True
            mock_res.refusal_triggered = False
            mock_res.refusal_reason = None
            mock_res.claim_verification = MagicMock(total_claims=1, verified_claims=1, all_claims_verified=True)
            return mock_res

        mock_rag.process_query = mock_process_query

        orchestrator = MultilingualOrchestrator(
            session_factory=session_factory,
            rag_orchestrator=mock_rag,
            query_decomposer=decomposer,
        )

        auth_ctx = ResolvedAuthContext(
            auth_type="SESSION_TOKEN",
            customer_id=str(cust_id),
            session_token="multi-session-token",
            roles=["CUSTOMER"],
        )

        turn_result = await orchestrator.process_chat_turn(
            session=session,
            customer_id=cust_id,
            session_id=sess_id,
            conversation_id=conv_id,
            message="1. How do I return an item? 2. How long does standard shipping take?",
            preferred_language="en",
            auth_context=auth_ctx,
        )

        reply = turn_result["reply_text"]
        assert "Question 1:" in reply
        assert "Return window is 30 days" in reply
        assert "Question 2:" in reply
        assert "Standard delivery takes 3 to 5 business days" in reply

    await engine.dispose()

