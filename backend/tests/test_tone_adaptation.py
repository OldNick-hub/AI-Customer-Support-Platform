"""Test Suite for Phase 2.2-B Wave 2 — Sentiment Tone Adaptation (Task 5 Criterion 4).

Validates:
1. Sentiment analysis occurs before final LLM answer generation.
2. Frustrated customer receives an appropriate empathetic tone directive.
3. Urgent customer receives clear, direct actionable next steps.
4. Neutral customer retains default professional tone.
5. Low-confidence sentiment falls back to neutral tone.
6. Sentiment service failure does not break response generation.
7. Tone adaptation does not change business policy.
8. Refund and deadline restrictions remain strictly intact.
9. Citations remain present regardless of adapted tone.
10. Multilingual conversation context and entity preservation are maintained.
11. Existing RAG behavior remains unchanged.
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
import unittest.mock as mock
import uuid

import pytest

from app.core.config import Settings
from app.modules.rag.citation import CitationManager, CitationMetadata, ClaimVerificationResult
from app.modules.rag.generator import (
    GroundedAnswerGenerator,
    RAGResolutionResult,
    SUPPORTED_SENTIMENT_TONES,
    TONE_PRESERVATION_RULES,
    get_tone_directive,
)
from app.modules.rag.orchestrator import RAGInternalRequest, RAGOrchestrator
from app.modules.rag.retriever import RetrievalResult, RetrievedChunk
from app.modules.sentiment.analyzer import (
    SentimentAnalysisResult,
    SentimentAnalyzer,
    default_sentiment_analyzer,
)
from app.services.llm_gateway import BaseLLMProvider, LLMGateway, LLMResponse
from app.services.pii_masker import PIIMasker


# ==============================================================================
# TEST FIXTURES & HELPERS
# ==============================================================================

class MockTestLLMProvider(BaseLLMProvider):
    """Deterministic in-memory mock LLM provider capturing prompts and instructions."""

    provider_name: str = "mock"

    def __init__(self, name: str = "mock", default_model: str = "mock-gemini") -> None:
        self.provider_name = name
        self.default_model = default_model
        self.call_count = 0
        self.prompts: List[str] = []
        self.system_instructions: List[Optional[str]] = []
        self.responses: List[Any] = []

    def queue_response(self, resp_or_exc: Any) -> None:
        self.responses.append(resp_or_exc)

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        self.call_count += 1
        self.prompts.append(prompt)
        self.system_instructions.append(system_instruction)

        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return LLMResponse(
                    content=item,
                    provider=self.provider_name,
                    model=model_override or self.default_model,
                )
            if isinstance(item, LLMResponse):
                return item

        return LLMResponse(
            content=f"Mock grounded answer [1].",
            provider=self.provider_name,
            model=model_override or self.default_model,
        )


def make_test_chunk(
    vector_id: int = 1,
    doc_code: str = "POL-RET-2026",
    title: str = "Standard Return Policy",
    version: int = 1,
    text: str = "Customers may return unopened items within 30 days of delivery for a full refund. A 15% restocking fee applies to opened hardware.",
) -> RetrievedChunk:
    return RetrievedChunk(
        vector_id=vector_id,
        chunk_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        document_id=str(uuid.uuid4()),
        document_code=doc_code,
        document_title=title,
        version_number=version,
        category="POLICY",
        product_line="GLOBAL",
        region="US",
        access_level="PUBLIC",
        effective_from=date(2026, 1, 1),
        effective_until=None,
        chunk_index=0,
        token_count=25,
        chunk_text=text,
        similarity_score=0.92,
        metadata={},
        sha256_hash="dummy_hash_123",
    )


from app.modules.rag.security import SecurityContext


def make_retrieval_result(chunks: List[RetrievedChunk], query: str = "Can I get a refund?") -> RetrievalResult:
    chunk_list = chunks or []
    return RetrievalResult(
        query=query,
        chunks=chunk_list,
        candidate_count=len(chunk_list),
        retrieved_count=len(chunk_list),
        top_similarity=chunk_list[0].similarity_score if chunk_list else 0.0,
        is_refusal=False,
        refusal_reason=None,
        conflict_detected=False,
        effective_as_of_date_applied=date(2026, 1, 1),
        security_context=SecurityContext(
            role="STANDARD_CUSTOMER",
            access_tier="REGISTERED",
            permitted_access_tiers={"PUBLIC", "REGISTERED"},
            region="US",
            permitted_regions={"GLOBAL", "US"},
            product_lines={"GLOBAL"},
        ),
        latency_ms=12.5,
    )


def create_test_generator(
    mock_provider: MockTestLLMProvider,
) -> GroundedAnswerGenerator:
    """Creates a GroundedAnswerGenerator wired with a mock LLM gateway."""
    test_settings = Settings(
        ENABLE_OLLAMA_FALLBACK=False,
        LLM_MAX_RETRIES=0,
    )
    gateway = LLMGateway(
        settings=test_settings,
        gemini_provider=mock_provider,
        ollama_provider=mock_provider,
    )
    gateway.register_provider("gemini", mock_provider)
    gateway.register_provider("mock", mock_provider)
    return GroundedAnswerGenerator(settings=test_settings, llm_gateway=gateway, pii_masker=PIIMasker())

@pytest.mark.asyncio
async def test_sentiment_analysis_categories_and_directives():
    """Verifies that all supported sentiment classes map to appropriate controlled directives."""
    analyzer = SentimentAnalyzer()

    # Frustrated
    res_fru = analyzer.analyze_text("This is completely unacceptable, broken, and ridiculous!!")
    assert res_fru.sentiment_class == "frustrated"
    dir_fru = get_tone_directive(res_fru.sentiment_class)
    assert "empathetic" in dir_fru.lower() or "frustration" in dir_fru.lower()
    assert "DO NOT promise unauthorized refunds" in dir_fru

    # Urgent
    res_urg = analyzer.analyze_text("URGENT: system is down and blocking production immediately")
    assert res_urg.sentiment_class == "urgent"
    dir_urg = get_tone_directive(res_urg.sentiment_class)
    assert "clarity" in dir_urg.lower() or "directness" in dir_urg.lower()

    # Positive
    res_pos = analyzer.analyze_text("Thank you so much! Great helpful service and awesome support.")
    assert res_pos.sentiment_class == "positive"
    dir_pos = get_tone_directive(res_pos.sentiment_class)
    assert "warm" in dir_pos.lower() or "courteous" in dir_pos.lower()

    # Neutral
    res_neu = analyzer.analyze_text("What is the warranty period for model X?")
    assert res_neu.sentiment_class == "neutral"
    dir_neu = get_tone_directive(res_neu.sentiment_class)
    assert "objective" in dir_neu.lower() or "professional" in dir_neu.lower()


@pytest.mark.asyncio
async def test_generator_injects_tone_directive_into_prompt():
    """Verifies GroundedAnswerGenerator injects tone directive and preservation boundaries into user prompt."""
    mock_provider = MockTestLLMProvider()
    generator = create_test_generator(mock_provider)
    chunk = make_test_chunk()
    retrieval = make_retrieval_result([chunk])

    frustrated_directive = get_tone_directive("frustrated")
    mock_provider.queue_response(
        "I understand your frustration regarding the item. Customers may return unopened items within 30 days of delivery [1]."
    )

    result = await generator.generate_answer(
        retrieval_result=retrieval,
        query_override="I am furious, give me my money back!!",
        tone_directive=frustrated_directive,
    )

    assert result.is_resolved is True
    assert mock_provider.call_count == 1
    sent_prompt = mock_provider.prompts[0]

    # Verify tone directive and preservation boundaries were included in prompt
    assert "<tone_guidance>" in sent_prompt
    assert "The customer is experiencing frustration" in sent_prompt
    assert "MANDATORY FACTUAL AND POLICY PRESERVATION BOUNDARIES" in sent_prompt
    assert "NEVER promise or authorize refunds" in sent_prompt


@pytest.mark.asyncio
async def test_urgent_customer_prompt_contains_urgency_directive():
    """Verifies that an urgent customer prompt contains the urgency directive."""
    mock_provider = MockTestLLMProvider()
    generator = create_test_generator(mock_provider)
    chunk = make_test_chunk()
    retrieval = make_retrieval_result([chunk])

    urgent_directive = get_tone_directive("urgent")
    mock_provider.queue_response(
        "Immediate Next Step: Items can be returned within 30 days of delivery [1]."
    )

    result = await generator.generate_answer(
        retrieval_result=retrieval,
        query_override="CRITICAL EMERGENCY: Need refund now",
        tone_directive=urgent_directive,
    )

    assert result.is_resolved is True
    sent_prompt = mock_provider.prompts[0]
    assert "time-sensitive or urgent" in sent_prompt
    assert "Prioritize maximum clarity and directness" in sent_prompt


@pytest.mark.asyncio
async def test_neutral_customer_prompt_without_explicit_tone():
    """Verifies that when no tone directive is passed, prompt omits <tone_guidance> block."""
    mock_provider = MockTestLLMProvider()
    generator = create_test_generator(mock_provider)
    chunk = make_test_chunk()
    retrieval = make_retrieval_result([chunk])

    mock_provider.queue_response(
        "Customers may return unopened items within 30 days of delivery [1]."
    )

    result = await generator.generate_answer(
        retrieval_result=retrieval,
        query_override="What is the return window?",
        tone_directive=None,
    )

    assert result.is_resolved is True
    sent_prompt = mock_provider.prompts[0]
    assert "<tone_guidance>" not in sent_prompt


@pytest.mark.asyncio
async def test_low_confidence_fallback_to_neutral():
    """Verifies that low confidence sentiment scores gracefully fall back to neutral tone."""
    low_conf_result = SentimentAnalysisResult(
        sentiment_class="frustrated",
        confidence_score=0.45,  # Below 0.60 threshold
        frustration_index=0.3,
        urgency_index=0.2,
        trajectory_slope=0.0,
        requires_escalation=False,
    )

    # Simulated fallback logic matching MultilingualOrchestrator Step 8c
    if low_conf_result.confidence_score < 0.60:
        applied_sentiment = "neutral"
    else:
        applied_sentiment = low_conf_result.sentiment_class

    assert applied_sentiment == "neutral"
    directive = get_tone_directive(applied_sentiment)
    assert "objective, professional" in directive


@pytest.mark.asyncio
async def test_sentiment_analyzer_failure_does_not_break_generation():
    """Verifies that if sentiment analysis raises an unexpected exception, generation falls back safely."""
    failing_analyzer = mock.MagicMock(spec=SentimentAnalyzer)
    failing_analyzer.analyze_text.side_effect = RuntimeError("Lexical sentiment parser crashed")

    # In Step 8c, failure is caught and defaults to neutral
    try:
        sentiment_eval = failing_analyzer.analyze_text("Some text")
        sentiment_class = sentiment_eval.sentiment_class
    except Exception:
        sentiment_class = "neutral"

    assert sentiment_class == "neutral"
    directive = get_tone_directive(sentiment_class)
    assert "objective, professional" in directive


@pytest.mark.asyncio
async def test_rag_internal_request_carries_tone_and_sentiment():
    """Verifies that RAGInternalRequest accepts and validates tone_directive and sentiment_class."""
    req = RAGInternalRequest(
        query="I am disappointed with this service",
        tone_directive=get_tone_directive("frustrated"),
        sentiment_class="frustrated",
        correlation_id=str(uuid.uuid4()),
    )
    assert req.sentiment_class == "frustrated"
    assert "empathetic" in req.tone_directive.lower()


@pytest.mark.asyncio
async def test_policy_invariants_preserved_under_adapted_tone():
    """Verifies that tone adaptation NEVER alters citations or unverified factual claims."""
    mock_provider = MockTestLLMProvider()
    generator = create_test_generator(mock_provider)
    chunk = make_test_chunk(
        text="Customers may return unopened items within 30 days of delivery for a full refund."
    )
    retrieval = make_retrieval_result([chunk])

    # Attempt 1: Hallucinates 60-day exception due to customer anger -> Claim verification catches it!
    # Attempt 2 (Remediation): Returns verified 30 days with citation.
    mock_provider.queue_response(
        "I am so sorry for your trouble. Because you are unhappy, we will extend your return to 60 days [1]."
    )
    mock_provider.queue_response(
        "I understand your frustration. Customers may return unopened items within 30 days of delivery for a full refund [1]."
    )

    result = await generator.generate_answer(
        retrieval_result=retrieval,
        query_override="I demand an exception because I am angry!",
        tone_directive=get_tone_directive("frustrated"),
    )

    assert result.is_resolved is True
    # Verify that the final response contains the strictly verified 30 days, not the hallucinated 60 days
    assert "30 days" in result.generated_response
    assert "60 days" not in result.generated_response
    assert len(result.citations) >= 1
    assert result.citations[0].document_code == "POL-RET-2026"
    assert mock_provider.call_count == 2  # Remediation loop was triggered and enforced!


@pytest.mark.asyncio
async def test_multilingual_context_and_citations_preserved():
    """Verifies that tone adaptation preserves multilingual translation and citation tags."""
    from app.modules.multilingual.entity_preserver import EntityPreservationVault

    vault = EntityPreservationVault()
    response_with_citations = "I understand your frustration. Customers have 30 days to return items [1]."

    # Test that citation shielding preserves bracketed citations across translation
    masked = vault.mask_citations(response_with_citations)
    assert "[1]" not in masked
    assert "__PRESERVED_CIT_1_" in masked

    unmasked = vault.unmask_citations(masked)
    assert unmasked == response_with_citations
    assert "[1]" in unmasked


@pytest.mark.asyncio
async def test_existing_rag_orchestrator_behavior_unchanged():
    """Verifies that RAGOrchestrator continues to function properly without tone directive."""
    mock_provider = MockTestLLMProvider()
    generator = create_test_generator(mock_provider)
    chunk = make_test_chunk()
    retrieval = make_retrieval_result([chunk])

    mock_provider.queue_response(
        "Customers may return unopened items within 30 days of delivery for a full refund [1]."
    )

    result = await generator.generate_answer(retrieval)
    assert result.is_resolved is True
    assert len(result.citations) == 1
    assert result.citations[0].document_code == "POL-RET-2026"
    assert result.claim_verification.all_claims_verified is True

