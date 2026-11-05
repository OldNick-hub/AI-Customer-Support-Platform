"""Test Suite for Day 03 Phase 3 (Foundational Services).

Covers:
- Phase 3A: Redis Service (connection, health checks, cache hit/miss, TTL, Pub/Sub,
  graceful degradation, no false success on failed writes).
- Phase 3B: Embedding Service (lazy singleton loading, 384-d output, batching,
  input validation, dimension mismatch error, non-blocking execution).
- Phase 3C: LLM Gateway (Gemini success, Ollama success, Gemini timeout, retry behavior,
  authentication failure without fallback, invalid request without fallback,
  fallback eligibility, double failure, and PII-safe logging).
"""

import asyncio
import io
import logging
import math
import pytest
from typing import Any, Dict, List, Optional
import redis.asyncio as aioredis
import redis.exceptions as redis_exc

from app.core.config import Settings
from app.core.exceptions import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingModelLoadError,
    LLMAuthenticationError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
)
from app.core.logging import PIISanitizingFormatter, SafeLoggingFilter
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_gateway import (
    BaseLLMProvider,
    GeminiProvider,
    LLMGateway,
    LLMResponse,
    OllamaProvider,
)
from app.services.redis_service import RedisService


# ==============================================================================
# MOCKS & FAKES FOR DETERMINISTIC IN-MEMORY TESTING
# ==============================================================================


class FakeAsyncRedis:
    """In-memory Redis client simulating connections, commands, and network faults."""

    def __init__(self) -> None:
        self.data: Dict[str, str] = {}
        self.ttls: Dict[str, Optional[int]] = {}
        self.published: List[tuple[str, str]] = []
        self.should_fail = False
        self.should_timeout = False

    async def ping(self) -> bool:
        if self.should_fail:
            raise redis_exc.ConnectionError("Connection refused")
        if self.should_timeout:
            raise redis_exc.TimeoutError("Operation timed out")
        return True

    async def get(self, key: str) -> Optional[str]:
        if self.should_fail:
            raise redis_exc.ConnectionError("Network disconnected")
        if self.should_timeout:
            raise redis_exc.TimeoutError("Timeout")
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        if self.should_fail:
            raise redis_exc.ConnectionError("Network disconnected")
        if self.should_timeout:
            raise redis_exc.TimeoutError("Timeout")
        self.data[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        if self.should_fail:
            raise redis_exc.ConnectionError("Network disconnected")
        if self.should_timeout:
            raise redis_exc.TimeoutError("Timeout")
        if key in self.data:
            del self.data[key]
            self.ttls.pop(key, None)
            return 1
        return 0

    async def exists(self, key: str) -> int:
        if self.should_fail:
            raise redis_exc.ConnectionError("Network disconnected")
        return 1 if key in self.data else 0

    async def publish(self, channel: str, message: str) -> int:
        if self.should_fail:
            raise redis_exc.ConnectionError("Network disconnected")
        self.published.append((channel, message))
        return 1

    def pubsub(self) -> Any:
        class FakePubSub:
            async def subscribe(self, channel: str) -> None:
                pass
            async def aclose(self) -> None:
                pass
        return FakePubSub()

    async def aclose(self) -> None:
        pass


class MockLLMProvider(BaseLLMProvider):
    """Mock provider with controllable responses, failures, and attempt counters."""

    def __init__(self, name: str, default_model: str = "mock-model") -> None:
        self.provider_name = name
        self.default_model = default_model
        self.call_count = 0
        self.responses: List[Any] = []  # Can queue responses or Exception instances

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
        if self.responses:
            next_item = self.responses.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item

        return LLMResponse(
            content=f"Response from {self.provider_name} to: {prompt[:30]}",
            provider=self.provider_name,
            model=model_override or self.default_model,
            usage={"total_tokens": 42},
        )


# ==============================================================================
# PHASE 3A: REDIS SERVICE TESTS
# ==============================================================================


class TestRedisService:
    """Verifies Redis Service connection, cache operations, TTL, and graceful degradation."""

    @pytest.fixture
    def fake_redis(self) -> FakeAsyncRedis:
        return FakeAsyncRedis()

    @pytest.fixture
    def redis_service(self, fake_redis: FakeAsyncRedis) -> RedisService:
        service = RedisService(namespace="test")
        service.client = fake_redis  # Inject in-memory client
        service._is_connected = True
        return service

    @pytest.mark.asyncio
    async def test_redis_successful_connection_and_ping(self, redis_service: RedisService):
        """Asserts healthy Redis ping returns True."""
        assert await redis_service.ping() is True
        assert await redis_service.is_healthy() is True

    @pytest.mark.asyncio
    async def test_redis_connection_failure(self, redis_service: RedisService, fake_redis: FakeAsyncRedis):
        """Asserts connection failures raise RedisConnectionError on ping."""
        fake_redis.should_fail = True
        with pytest.raises(RedisConnectionError):
            await redis_service.ping()
        assert await redis_service.is_healthy() is False

    @pytest.mark.asyncio
    async def test_redis_cache_hit_and_miss(self, redis_service: RedisService):
        """Asserts clear distinction between genuine cache miss and cache hit."""
        # Genuine cache miss: key does not exist
        val = await redis_service.get("non_existent_key")
        assert val is None

        # Check status tuple: (value, hit, available)
        res, hit, available = await redis_service.get_with_status("non_existent_key")
        assert res is None
        assert hit is False
        assert available is True  # Redis is healthy, key just not there

        # Set value
        set_ok = await redis_service.set("session_101", "active_state")
        assert set_ok is True

        # Cache hit
        cached_val = await redis_service.get("session_101")
        assert cached_val == "active_state"

        res, hit, available = await redis_service.get_with_status("session_101")
        assert res == "active_state"
        assert hit is True
        assert available is True

    @pytest.mark.asyncio
    async def test_redis_ttl_behavior(self, redis_service: RedisService, fake_redis: FakeAsyncRedis):
        """Asserts TTL argument is properly forwarded to Redis."""
        await redis_service.set("token_key", "jwt_payload", ttl_seconds=300)
        assert fake_redis.ttls["test:token_key"] == 300

    @pytest.mark.asyncio
    async def test_redis_pubsub(self, redis_service: RedisService, fake_redis: FakeAsyncRedis):
        """Asserts pub/sub messages are published to specified channel."""
        count = await redis_service.publish("channel:index_reload", "reload_faiss")
        assert count == 1
        assert ("channel:index_reload", "reload_faiss") in fake_redis.published

        pubsub = await redis_service.get_pubsub()
        assert pubsub is not None

    @pytest.mark.asyncio
    async def test_redis_graceful_degradation(self, redis_service: RedisService, fake_redis: FakeAsyncRedis):
        """Asserts get() returns default value without raising when Redis is down."""
        fake_redis.should_fail = True

        # Degradation on GET: returns fallback default
        val = await redis_service.get("any_key", default="fallback_data", raise_on_error=False)
        assert val == "fallback_data"

        # Explicit status reports available=False
        res, hit, available = await redis_service.get_with_status("any_key")
        assert res is None
        assert hit is False
        assert available is False

        # If raise_on_error is requested, it raises RedisConnectionError
        with pytest.raises(RedisConnectionError):
            await redis_service.get("any_key", raise_on_error=True)

    @pytest.mark.asyncio
    async def test_redis_no_false_success_on_failed_writes(
        self, redis_service: RedisService, fake_redis: FakeAsyncRedis
    ):
        """CRITICAL: Asserts failed writes NEVER return True."""
        fake_redis.should_fail = True

        # SET must return False on failure, NOT True
        set_result = await redis_service.set("fail_key", "val", raise_on_error=False)
        assert set_result is False

        # DELETE must return False on failure
        del_result = await redis_service.delete("fail_key", raise_on_error=False)
        assert del_result is False

        # With raise_on_error=True, raises RedisConnectionError
        with pytest.raises(RedisConnectionError):
            await redis_service.set("fail_key", "val", raise_on_error=True)

        with pytest.raises(RedisConnectionError):
            await redis_service.delete("fail_key", raise_on_error=True)


# ==============================================================================
# PHASE 3B: EMBEDDING SERVICE TESTS
# ==============================================================================


class TestEmbeddingService:
    """Verifies Embedding Service singleton loading, 384-d output, batching, and async execution."""

    @pytest.fixture
    def embedding_service(self) -> EmbeddingService:
        return get_embedding_service()

    @pytest.mark.asyncio
    async def test_embedding_384_dimensional_output(self, embedding_service: EmbeddingService):
        """Asserts generated embedding vector length is strictly 384 floats."""
        text = "How do I request a refund for my delayed shipment?"
        vec = await embedding_service.embed_text(text)

        assert isinstance(vec, list)
        assert len(vec) == 384
        assert all(isinstance(x, float) for x in vec)

        # Assert vector is unit normalized (L2 norm ≈ 1.0 for cosine similarity)
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_embedding_singleton_loading(self, embedding_service: EmbeddingService):
        """Asserts model is loaded once and subsequent calls reuse the cached instance."""
        assert embedding_service.is_loaded is True

        model_ref_1 = embedding_service._get_model()
        model_ref_2 = embedding_service._get_model()
        assert id(model_ref_1) == id(model_ref_2)

    @pytest.mark.asyncio
    async def test_embedding_batch_processing(self, embedding_service: EmbeddingService):
        """Asserts batch embedding returns multiple 384-dimensional vectors."""
        batch = [
            "Password reset instructions for customer portal",
            "Billing discrepancy on invoice #10492",
            "Technical support inquiry regarding monitor flicker",
        ]
        embeddings = await embedding_service.embed_batch(batch)

        assert len(embeddings) == 3
        for vec in embeddings:
            assert len(vec) == 384

        # Empty batch returns empty list
        empty_res = await embedding_service.embed_batch([])
        assert empty_res == []

    @pytest.mark.asyncio
    async def test_embedding_invalid_input(self, embedding_service: EmbeddingService):
        """Asserts empty or malformed inputs raise ValueError safely."""
        with pytest.raises(ValueError):
            await embedding_service.embed_text("")

        with pytest.raises(ValueError):
            await embedding_service.embed_text(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            await embedding_service.embed_text("   ")

        with pytest.raises(ValueError):
            await embedding_service.embed_batch(["valid text", ""])

        with pytest.raises(ValueError):
            await embedding_service.embed_batch(["valid text", None])  # type: ignore[list-item]

    @pytest.mark.asyncio
    async def test_embedding_dimension_mismatch(self):
        """Asserts configured dimension mismatch raises EmbeddingDimensionMismatchError."""
        # Configure service expecting 512 dimensions while model yields 384
        mismatched_service = EmbeddingService(expected_dimension=512)

        with pytest.raises(EmbeddingDimensionMismatchError) as exc_info:
            await mismatched_service.embed_text("This should trigger a dimension mismatch error")

        err = exc_info.value
        assert err.expected_dim == 512
        assert err.actual_dim == 384
        assert err.status_code == 500
        assert err.error_code == "EMBEDDING_DIMENSION_MISMATCH"

    @pytest.mark.asyncio
    async def test_embedding_non_blocking_execution(self, embedding_service: EmbeddingService):
        """Asserts asyncio.to_thread prevents blocking the FastAPI event loop during inference."""
        async def background_counter():
            count = 0
            for _ in range(5):
                await asyncio.sleep(0.01)
                count += 1
            return count

        # Run counter concurrently with embedding inference
        counter_task = asyncio.create_task(background_counter())
        embed_task = asyncio.create_task(
            embedding_service.embed_text("Test concurrent non-blocking execution")
        )

        counter_val, vec = await asyncio.gather(counter_task, embed_task)
        assert counter_val == 5
        assert len(vec) == 384


# ==============================================================================
# PHASE 3C: LLM GATEWAY TESTS
# ==============================================================================


class TestLLMGateway:
    """Verifies LLM Gateway routing, transient retries, non-fallback authentication boundaries, and safe logging."""

    @pytest.fixture
    def mock_gemini(self) -> MockLLMProvider:
        return MockLLMProvider(name="gemini", default_model="gemini-3.8-flash")

    @pytest.fixture
    def mock_ollama(self) -> MockLLMProvider:
        return MockLLMProvider(name="ollama", default_model="llama3:latest")

    @pytest.fixture
    def gateway(self, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider) -> LLMGateway:
        settings = Settings(
            PRIMARY_LLM_PROVIDER="gemini",
            GEMINI_MODEL="gemini-3.8-flash",
            OLLAMA_MODEL="llama3:latest",
            ENABLE_OLLAMA_FALLBACK=True,
            LLM_MAX_RETRIES=2,
            LLM_TIMEOUT_SECONDS=10.0,
        )
        return LLMGateway(
            settings=settings,
            gemini_provider=mock_gemini,
            ollama_provider=mock_ollama,
        )

    @pytest.mark.asyncio
    async def test_gemini_success(self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider):
        """Asserts primary Gemini provider executes successfully when available."""
        mock_gemini.queue_response(
            LLMResponse(content="Gemini greeting", provider="gemini", model="gemini-3.8-flash")
        )
        resp = await gateway.generate(prompt="Hello support assistant")

        assert resp.content == "Gemini greeting"
        assert resp.provider == "gemini"
        assert resp.model == "gemini-3.8-flash"
        assert mock_gemini.call_count == 1
        assert mock_ollama.call_count == 0

    @pytest.mark.asyncio
    async def test_ollama_success(self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider):
        """Asserts direct Ollama provider execution when requested via override."""
        mock_ollama.queue_response(
            LLMResponse(content="Ollama greeting", provider="ollama", model="llama3:latest")
        )
        resp = await gateway.generate(prompt="Hello from local", provider_override="ollama")

        assert resp.content == "Ollama greeting"
        assert resp.provider == "ollama"
        assert mock_ollama.call_count == 1
        assert mock_gemini.call_count == 0

    @pytest.mark.asyncio
    async def test_gemini_retry_behavior_on_transient_failure(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider
    ):
        """Asserts transient 503 error is retried and succeeds without triggering fallback."""
        # First call fails with transient error; second succeeds
        transient_err = LLMProviderError(
            message="Gemini overloaded", provider="gemini", detail={"status_code": 503, "is_transient": True}
        )
        mock_gemini.queue_response(transient_err)
        mock_gemini.queue_response(
            LLMResponse(content="Recovered after retry", provider="gemini", model="gemini-3.8-flash")
        )

        resp = await gateway.generate(prompt="Recover test")

        assert resp.content == "Recovered after retry"
        assert resp.provider == "gemini"
        assert mock_gemini.call_count == 2
        assert mock_ollama.call_count == 0  # No fallback needed

    @pytest.mark.asyncio
    async def test_authentication_failure_no_fallback_no_retry(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider
    ):
        """STRICT RULE: Auth failures must NEVER be retried and must NEVER trigger fallback."""
        auth_err = LLMAuthenticationError(message="Invalid API Key for Gemini", provider="gemini")
        mock_gemini.queue_response(auth_err)

        with pytest.raises(LLMAuthenticationError) as exc_info:
            await gateway.generate(prompt="Secure transaction")

        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "LLM_AUTH_ERROR"
        assert mock_gemini.call_count == 1  # Never retried
        assert mock_ollama.call_count == 0  # Never fell back

    @pytest.mark.asyncio
    async def test_invalid_request_no_fallback_no_retry(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider
    ):
        """STRICT RULE: 400 Bad Request client errors must NEVER be retried and must NEVER fall back."""
        client_err = LLMProviderError(
            message="Malformed prompt argument",
            provider="gemini",
            detail={"status_code": 400},
        )
        mock_gemini.queue_response(client_err)

        with pytest.raises(LLMProviderError) as exc_info:
            await gateway.generate(prompt="Invalid prompt schema")

        assert exc_info.value.detail["status_code"] == 400
        assert mock_gemini.call_count == 1  # Never retried
        assert mock_ollama.call_count == 0  # Never fell back

    @pytest.mark.asyncio
    async def test_fallback_eligibility_on_transient_exhaustion(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider
    ):
        """Asserts gateway falls back to Ollama after primary retries are exhausted on transient errors."""
        timeout_err = LLMTimeoutError(provider="gemini", timeout_seconds=10.0)

        # Queue 3 timeout errors (1 initial + 2 retries)
        mock_gemini.queue_response(timeout_err)
        mock_gemini.queue_response(timeout_err)
        mock_gemini.queue_response(timeout_err)

        mock_ollama.queue_response(
            LLMResponse(content="Responded via Ollama fallback", provider="ollama", model="llama3:latest")
        )

        resp = await gateway.generate(prompt="Fallback when timeout")

        assert resp.content == "Responded via Ollama fallback"
        assert resp.provider == "ollama"
        assert mock_gemini.call_count == 3  # Initial + 2 retries
        assert mock_ollama.call_count == 1  # Successfully called fallback

    @pytest.mark.asyncio
    async def test_both_providers_unavailable(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider, mock_ollama: MockLLMProvider
    ):
        """Asserts descriptive error when primary exhausts retries and fallback also fails."""
        gemini_err = LLMTimeoutError(provider="gemini", timeout_seconds=10.0)
        mock_gemini.queue_response(gemini_err)
        mock_gemini.queue_response(gemini_err)
        mock_gemini.queue_response(gemini_err)

        ollama_err = LLMProviderError(message="Ollama connection refused", provider="ollama")
        mock_ollama.queue_response(ollama_err)

        with pytest.raises(LLMProviderError) as exc_info:
            await gateway.generate(prompt="Double failure test")

        assert "All LLM providers failed" in str(exc_info.value)
        assert mock_gemini.call_count == 3
        assert mock_ollama.call_count == 1

    @pytest.mark.asyncio
    async def test_pii_safe_logging_in_gateway(
        self, gateway: LLMGateway, mock_gemini: MockLLMProvider
    ):
        """Asserts prompts logged by gateway have credit cards and emails masked in logs."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(PIISanitizingFormatter("%(levelname)s - %(message)s"))

        logger = logging.getLogger("app.services.llm_gateway")
        logger.disabled = False
        logger.setLevel(logging.INFO)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.addFilter(SafeLoggingFilter())

        mock_gemini.queue_response(
            LLMResponse(content="Processed refund request", provider="gemini", model="gemini-3.8-flash")
        )

        prompt_with_pii = (
            "Refund customer with card 4111 1111 1111 1111 and email sarah.connor@cyberdyne.com"
        )
        await gateway.generate(prompt=prompt_with_pii)

        log_output = stream.getvalue()

        # Zero raw credit cards or emails in log stream
        assert "4111 1111 1111 1111" not in log_output
        assert "sarah.connor@" not in log_output

        # Masked representations must be present
        assert "****-****-****-1111" in log_output
        assert "s***r@cyberdyne.com" in log_output
