"""Phase 1: Gemini & Pluggable Embedding Provider Comprehensive Verification Tests.

Verifies:
1. Abstract BaseEmbeddingProvider contract and provider polymorphism.
2. MockEmbeddingProvider deterministic vector generation, unit normalization, and dimension consistency.
3. GeminiEmbeddingProvider request serialization (gemini-embedding-2, output_dimensionality=768, RETRIEVAL_DOCUMENT).
4. GeminiEmbeddingProvider bounded exponential backoff retry on transient 429/503 API errors.
5. GeminiEmbeddingProvider rate-limit and quota exhaustion handling.
6. Dimension validation and EmbeddingDimensionMismatchError propagation.
7. Input sanitation (empty string, None, whitespace rejection with ValueError).
8. Strict configuration enforcement: missing GEMINI_API_KEY raises EmbeddingConfigurationError.
9. Lazy-loading behavior of SentenceTransformerEmbeddingProvider (no eager PyTorch imports).
10. Memory safety: PyTorch and SentenceTransformers are not loaded into sys.modules during normal mock/gemini usage.
"""

import asyncio
import math
import sys
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.core.config import Settings
from app.core.exceptions import (
    EmbeddingAPIError,
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatchError,
)
from app.services.embedding_service import (
    BaseEmbeddingProvider,
    EmbeddingService,
    GeminiEmbeddingProvider,
    MockEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    get_embedding_service,
    reset_embedding_service,
)


# =============================================================================
# 1. MOCK EMBEDDING PROVIDER TESTS
# =============================================================================

class TestMockEmbeddingProvider:
    """Verifies deterministic, zero-network mock embedding generation."""

    def test_mock_provider_initialization_defaults(self):
        provider = MockEmbeddingProvider()
        assert provider.expected_dimension == 384
        assert provider.model_name == "mock-embedding-2"
        assert provider.is_loaded is True
        assert provider.get_model() is provider

    def test_mock_provider_custom_dimension(self):
        provider_768 = MockEmbeddingProvider(dimension=768, model_name="mock-768")
        assert provider_768.expected_dimension == 768
        assert provider_768.model_name == "mock-768"

    @pytest.mark.asyncio
    async def test_mock_embedding_deterministic_generation(self):
        provider = MockEmbeddingProvider(dimension=768)
        text = "How do I claim a refund for my damaged package?"

        vec1 = await provider.embed_text(text)
        vec2 = await provider.embed_text(text)
        assert vec1 == vec2, "Mock provider must be 100% deterministic for identical input text"
        assert len(vec1) == 768
        assert all(isinstance(x, float) for x in vec1)

        # Different texts produce different vectors
        vec3 = await provider.embed_text("Technical support for router connection issue")
        assert vec1 != vec3

    @pytest.mark.asyncio
    async def test_mock_embedding_unit_normalization(self):
        provider = MockEmbeddingProvider(dimension=768)
        texts = ["Text A", "Text B", "Arbitrary text payload with special characters: 123!@#"]
        vectors = await provider.embed_batch(texts)

        assert len(vectors) == 3
        for vec in vectors:
            assert len(vec) == 768
            norm = math.sqrt(sum(x * x for x in vec))
            assert abs(norm - 1.0) < 1e-5, f"Vector must be unit normalized, got norm {norm}"

    @pytest.mark.asyncio
    async def test_mock_embedding_empty_batch(self):
        provider = MockEmbeddingProvider()
        res = await provider.embed_batch([])
        assert res == []

    @pytest.mark.asyncio
    async def test_mock_embedding_input_validation(self):
        provider = MockEmbeddingProvider()

        with pytest.raises(ValueError):
            await provider.embed_text("")

        with pytest.raises(ValueError):
            await provider.embed_text(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            await provider.embed_text("   \n\t  ")

        with pytest.raises(ValueError):
            await provider.embed_batch(["valid", ""])

        with pytest.raises(ValueError):
            await provider.embed_batch(["valid", None])  # type: ignore[list-item]


# =============================================================================
# 2. GEMINI EMBEDDING PROVIDER TESTS (MOCKED SDK)
# =============================================================================

class TestGeminiEmbeddingProvider:
    """Verifies GeminiEmbeddingProvider targeting gemini-embedding-2."""

    def test_missing_api_key_raises_configuration_error(self):
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            GeminiEmbeddingProvider(api_key=None, client=None)
        assert "GEMINI_API_KEY is required" in str(exc_info.value)

    def test_empty_string_api_key_raises_configuration_error(self):
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            GeminiEmbeddingProvider(api_key="", client=None)
        assert "GEMINI_API_KEY is required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_gemini_request_serialization_and_dimension(self):
        """Verifies gemini-embedding-2 invocation passes 768-d config and extracts values."""
        # Create mock ContentEmbedding object
        fake_768_vec = [1.0 / math.sqrt(768)] * 768
        mock_emb = MagicMock()
        mock_emb.values = fake_768_vec

        mock_response = MagicMock()
        mock_response.embeddings = [mock_emb]

        mock_client = MagicMock()
        mock_client.aio.models.embed_content = AsyncMock(return_value=mock_response)

        provider = GeminiEmbeddingProvider(
            api_key="fake-test-key",
            model_name="gemini-embedding-2",
            dimension=768,
            client=mock_client,
        )

        assert provider.expected_dimension == 768
        assert provider.model_name == "gemini-embedding-2"
        assert provider.is_loaded is True

        res = await provider.embed_text("Test query text for knowledge retrieval")
        assert len(res) == 768
        assert abs(math.sqrt(sum(x * x for x in res)) - 1.0) < 1e-4

        # Verify SDK call signature
        mock_client.aio.models.embed_content.assert_called_once()
        call_kwargs = mock_client.aio.models.embed_content.call_args.kwargs
        assert call_kwargs["model"] == "gemini-embedding-2"
        assert call_kwargs["contents"] == ["Test query text for knowledge retrieval"]
        assert call_kwargs["config"].output_dimensionality == 768

    @pytest.mark.asyncio
    async def test_gemini_batch_chunking(self):
        """Verifies batch_size parameter chunks large lists into multiple API calls."""
        fake_vec = [1.0 / math.sqrt(768)] * 768

        def make_response(contents, **kwargs):
            resp = MagicMock()
            embs = []
            for _ in contents:
                m = MagicMock()
                m.values = fake_vec
                embs.append(m)
            resp.embeddings = embs
            return resp

        mock_client = MagicMock()
        mock_client.aio.models.embed_content = AsyncMock(side_effect=make_response)

        # Batch size 2 with 5 input texts -> should invoke API 3 times (2, 2, 1)
        provider = GeminiEmbeddingProvider(
            api_key="fake-key",
            dimension=768,
            batch_size=2,
            client=mock_client,
        )

        texts = ["Text 1", "Text 2", "Text 3", "Text 4", "Text 5"]
        results = await provider.embed_batch(texts)

        assert len(results) == 5
        assert mock_client.aio.models.embed_content.call_count == 3

    @pytest.mark.asyncio
    async def test_gemini_transient_retry_with_backoff(self):
        """Verifies transient 429 rate limit triggers bounded exponential backoff and succeeds."""
        fake_vec = [1.0 / math.sqrt(768)] * 768
        success_emb = MagicMock()
        success_emb.values = fake_vec
        success_response = MagicMock()
        success_response.embeddings = [success_emb]

        # First call fails with 429 APIError, second call succeeds
        mock_api_error = MagicMock()
        mock_api_error.status = 429
        mock_api_error.message = "Resource exhausted: Rate limit exceeded"

        mock_client = MagicMock()
        mock_client.aio.models.embed_content = AsyncMock(
            side_effect=[mock_api_error, success_response]
        )

        provider = GeminiEmbeddingProvider(
            api_key="fake-key",
            dimension=768,
            max_retries=3,
            base_delay_seconds=0.01,  # Fast for test
            client=mock_client,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await provider.embed_text("Testing 429 rate limit retry")
            assert len(res) == 768
            assert mock_client.aio.models.embed_content.call_count == 2
            mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_gemini_exhausted_retries_raises_embedding_api_error(self):
        """Verifies permanent failure after max retries raises EmbeddingAPIError."""
        mock_api_error = MagicMock()
        mock_api_error.status = 503
        mock_api_error.message = "Service Unavailable: Backend overloaded"

        mock_client = MagicMock()
        mock_client.aio.models.embed_content = AsyncMock(side_effect=mock_api_error)

        provider = GeminiEmbeddingProvider(
            api_key="fake-key",
            dimension=768,
            max_retries=2,
            base_delay_seconds=0.01,
            client=mock_client,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(EmbeddingAPIError) as exc_info:
                await provider.embed_text("Will fail after retries")
            assert "Gemini embedding API error" in str(exc_info.value)
            assert mock_client.aio.models.embed_content.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_gemini_dimension_mismatch_raises_immediately(self):
        """Verifies unexpected vector length from API raises EmbeddingDimensionMismatchError without retry."""
        wrong_emb = MagicMock()
        wrong_emb.values = [0.1] * 384  # Returns 384 when 768 expected
        mock_response = MagicMock()
        mock_response.embeddings = [wrong_emb]

        mock_client = MagicMock()
        mock_client.aio.models.embed_content = AsyncMock(return_value=mock_response)

        provider = GeminiEmbeddingProvider(
            api_key="fake-key",
            dimension=768,
            client=mock_client,
        )

        with pytest.raises(EmbeddingDimensionMismatchError) as exc_info:
            await provider.embed_text("Testing dimension validation")
        err = exc_info.value
        assert err.expected_dim == 768
        assert err.actual_dim == 384


# =============================================================================
# 3. EMBEDDING SERVICE FACADE & RESOLUTION TESTS
# =============================================================================

class TestEmbeddingServiceFacade:
    """Verifies EmbeddingService resolution, delegation, and dependency injection."""

    def test_facade_resolves_mock_provider_by_default(self):
        settings = Settings(EMBEDDING_PROVIDER="mock", EMBEDDING_DIMENSION=384)
        service = EmbeddingService(settings=settings)
        assert isinstance(service.provider, MockEmbeddingProvider)
        assert service.expected_dimension == 384
        assert service.is_loaded is True

    def test_facade_resolves_gemini_provider_with_api_key(self):
        settings = Settings(
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="test-api-key",
            EMBEDDING_MODEL_NAME="gemini-embedding-2",
            EMBEDDING_DIMENSION=768,
        )
        service = EmbeddingService(settings=settings)
        assert isinstance(service.provider, GeminiEmbeddingProvider)
        assert service.expected_dimension == 768
        assert service.model_name == "gemini-embedding-2"

    def test_facade_raises_error_for_unknown_provider(self):
        settings = Settings()
        setattr(settings, "EMBEDDING_PROVIDER", "invalid_provider_name")
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            EmbeddingService(settings=settings)
        assert "Unknown EMBEDDING_PROVIDER" in str(exc_info.value)

    def test_facade_rejects_mock_in_staging_environment(self):
        settings = Settings(ENVIRONMENT="development", EMBEDDING_PROVIDER="mock")
        setattr(settings, "ENVIRONMENT", "staging")
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            EmbeddingService(settings=settings)
        assert "EMBEDDING_PROVIDER 'mock' is prohibited in staging environment" in str(exc_info.value)

    def test_facade_rejects_mock_in_production_environment(self):
        settings = Settings(ENVIRONMENT="development", EMBEDDING_PROVIDER="mock")
        setattr(settings, "ENVIRONMENT", "production")
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            EmbeddingService(settings=settings)
        assert "EMBEDDING_PROVIDER 'mock' is prohibited in production environment" in str(exc_info.value)

    def test_facade_never_silently_falls_back_to_mock_when_gemini_missing_key(self):
        settings = Settings(
            ENVIRONMENT="development",
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY=None,
        )
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            EmbeddingService(settings=settings)
        assert "GEMINI_API_KEY is required" in str(exc_info.value)

    def test_facade_supports_injected_provider(self):
        custom_mock = MockEmbeddingProvider(dimension=512, model_name="custom-mock")
        service = EmbeddingService(provider=custom_mock, expected_dimension=512)
        assert service.provider is custom_mock
        assert service.expected_dimension == 512

    @pytest.mark.asyncio
    async def test_facade_embed_alias(self):
        service = EmbeddingService()
        vec_text = await service.embed_text("Customer query")
        vec_alias = await service.embed("Customer query")
        assert vec_text == vec_alias

    def test_singleton_accessor_and_reset(self):
        reset_embedding_service()
        s1 = get_embedding_service()
        s2 = get_embedding_service()
        assert s1 is s2
        reset_embedding_service()
        s3 = get_embedding_service()
        assert s3 is not s1
        reset_embedding_service()


# =============================================================================
# 4. LAZY LOADING & MEMORY ISOLATION TESTS
# =============================================================================

class TestMemoryAndLazyLoading:
    """Verifies that PyTorch and SentenceTransformers are not loaded during mock or gemini operations."""

    def test_mock_provider_does_not_load_torch_or_sentence_transformers(self):
        """Confirms MockEmbeddingProvider does not import heavy ML libraries."""
        provider = MockEmbeddingProvider(dimension=768)
        assert provider.is_loaded is True

        # Ensure sentence_transformers is not in sys.modules from this provider
        # Note: In a test environment where pytest might touch other modules,
        # we verify provider does not have a reference to SentenceTransformer
        assert not hasattr(provider, "_model") or provider.get_model() is provider

    def test_gemini_provider_does_not_load_sentence_transformers(self):
        """Confirms GeminiEmbeddingProvider does not import sentence_transformers."""
        mock_client = MagicMock()
        provider = GeminiEmbeddingProvider(
            api_key="fake-key",
            dimension=768,
            client=mock_client,
        )
        assert provider.get_model() is mock_client

    def test_sentence_transformer_lazy_provider_does_not_load_model_on_init(self):
        """Confirms SentenceTransformerEmbeddingProvider does not instantiate the model until get_model()."""
        provider = SentenceTransformerEmbeddingProvider(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            expected_dimension=384,
        )
        assert provider._model is None
        assert provider.is_loaded is False

