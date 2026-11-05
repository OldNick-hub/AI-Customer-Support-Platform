"""Unit Test Suite for Day 07 — Task 6 (Phase 2).

Tests Phase 2 Translation Service, Redis Caching, and Resilience:
1. Bidirectional translation across en <-> (es, fr, de, hi)
2. 128-bit entity & citation preservation in both directions
3. PII masking at the actual LLMGateway provider boundary
4. Provider timeout and outage handling (Zero Silent English)
5. Corrupted provider output and placeholder rejection
6. Redis unavailable and recovery (graceful degradation)
7. Cache hit, miss, TTL, and language pair isolation
8. Cache content contains no raw PII or raw customer identifiers
9. Cross-request and cross-customer template isolation
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.core.exceptions import LLMProviderError, LLMTimeoutError, RedisConnectionError
from app.modules.multilingual.config import (
    LOCALIZED_SERVICE_NOTICES,
    REDIS_TRANSLATION_TTL_SECONDS,
)
from app.modules.multilingual.entity_preserver import EntityPreservationVault
from app.modules.multilingual.security_scanner import PreTranslationSecurityScanner
from app.modules.multilingual.translator import TranslationResult, Translator
from app.services.llm_gateway import LLMGateway, LLMResponse
from app.services.pii_masker import PIIMasker
from app.services.redis_service import RedisService


@pytest.fixture
def mock_llm_gateway() -> MagicMock:
    """Creates a mock LLMGateway returning predictable completions."""
    mock = MagicMock(spec=LLMGateway)
    mock.generate = AsyncMock()
    return mock


@pytest.fixture
def mock_redis_service() -> MagicMock:
    """Creates an in-memory mock RedisService."""
    mock = MagicMock(spec=RedisService)
    storage = {}

    async def mock_get(key: str, default=None):
        return storage.get(key, default)

    async def mock_set(key: str, value: str, ttl_seconds=None):
        storage[key] = value
        return True

    mock.get = AsyncMock(side_effect=mock_get)
    mock.set = AsyncMock(side_effect=mock_set)
    mock._storage = storage
    return mock


@pytest.fixture
def translator(mock_llm_gateway: MagicMock, mock_redis_service: MagicMock) -> Translator:
    """Instantiates a Translator with mocked LLM and Redis dependencies."""
    return Translator(
        llm_gateway=mock_llm_gateway,
        redis_service=mock_redis_service,
        pii_masker=PIIMasker(),
        cache_ttl_seconds=REDIS_TRANSLATION_TTL_SECONDS,
        timeout_seconds=5.0,
    )


# ==============================================================================
# 1. BIDIRECTIONAL TRANSLATION & PLACEHOLDER PRESERVATION
# ==============================================================================

class TestBidirectionalTranslation:
    """Verifies bidirectional translation between English and Spanish, French, German, and Hindi."""

    @pytest.mark.asyncio
    async def test_english_to_spanish_with_entities(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies en -> es translation preserves order ID, amount, and date."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Su pedido __NORM_ORDER_0__ por el monto de __NORM_AMT_1__ fue enviado el __NORM_DATE_2__.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "Your order ORD-12345 for the amount of $49.99 was shipped on 2026-11-04."
        vault = EntityPreservationVault(request_id="req-es-1")

        result = await translator.translate(
            text=raw,
            source_language="en",
            target_language="es",
            vault=vault,
        )

        assert result.is_successful is True
        assert result.target_language == "es"
        assert "ORD-12345" in result.translated_text
        assert "$49.99" in result.translated_text
        assert "2026-11-04" in result.translated_text
        assert "__NORM_" not in result.translated_text
        assert "__PRESERVED_" not in result.translated_text

    @pytest.mark.asyncio
    async def test_spanish_to_english_with_entities(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies es -> en translation preserves entities."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="When will my order __NORM_ORDER_0__ arrive?",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "¿Cuándo llegará mi pedido ORD-99881?"
        vault = EntityPreservationVault(request_id="req-es-en")

        result = await translator.translate(
            text=raw,
            source_language="es",
            target_language="en",
            vault=vault,
        )

        assert result.is_successful is True
        assert "When will my order ORD-99881 arrive?" in result.translated_text
        assert "ORD-99881" in result.translated_text

    @pytest.mark.asyncio
    async def test_english_to_french_with_citations(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies en -> fr outbound response preserves citations [1] and [2]."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="La politique de retour est de 30 jours __NORM_CIT_1_0__ pour la commande __NORM_ORDER_1__.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "The return policy is 30 days [1] for order ORD-5544."
        vault = EntityPreservationVault(request_id="req-fr-1")

        result = await translator.translate(
            text=raw,
            source_language="en",
            target_language="fr",
            vault=vault,
            is_outbound_response=True,
        )

        assert result.is_successful is True
        assert "[1]" in result.translated_text
        assert "ORD-5544" in result.translated_text

    @pytest.mark.asyncio
    async def test_english_to_german(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies en -> de translation."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Ihre Rückerstattung für Artikel __NORM_SKU_0__ beträgt __NORM_AMT_1__.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "Your refund for item SKU-7762 is $29.00."
        vault = EntityPreservationVault(request_id="req-de-1")

        result = await translator.translate(
            text=raw,
            source_language="en",
            target_language="de",
            vault=vault,
        )

        assert result.is_successful is True
        assert "SKU-7762" in result.translated_text
        assert "$29.00" in result.translated_text

    @pytest.mark.asyncio
    async def test_hindi_hinglish_to_english(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies Hinglish hi -> en translation."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="When will my order __NORM_ORDER_0__ be delivered?",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "Mera order ORD-991 kab tak deliver hoga bhai?"
        vault = EntityPreservationVault(request_id="req-hi-en")

        result = await translator.translate(
            text=raw,
            source_language="hi",
            target_language="en",
            vault=vault,
        )

        assert result.is_successful is True
        assert "When will my order ORD-991 be delivered?" in result.translated_text

    @pytest.mark.asyncio
    async def test_english_to_hindi(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies en -> hi translation."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="आपका ऑर्डर __NORM_ORDER_0__ भेज दिया गया है __NORM_CIT_1_1__।",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "Your order ORD-8811 has been shipped [1]."
        vault = EntityPreservationVault(request_id="req-en-hi")

        result = await translator.translate(
            text=raw,
            source_language="en",
            target_language="hi",
            vault=vault,
            is_outbound_response=True,
        )

        assert result.is_successful is True
        assert "ORD-8811" in result.translated_text
        assert "[1]" in result.translated_text

    @pytest.mark.asyncio
    async def test_same_language_passthrough(self, translator: Translator):
        """Verifies that translating en -> en is a fast no-op."""
        raw = "This is already in English."
        result = await translator.translate(raw, "en", "en")
        assert result.is_successful is True
        assert result.translated_text == raw


# ==============================================================================
# 2. PII MASKING AT THE ACTUAL LLM PROVIDER BOUNDARY
# ==============================================================================

class TestPIIMaskingAtProviderBoundary:
    """Verifies that raw customer PII is NEVER transmitted to external LLM providers."""

    @pytest.mark.asyncio
    async def test_llm_prompt_contains_zero_raw_pii(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies credit card, email, SSN, and phone are scrubbed before reaching LLMGateway.generate()."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Por favor verifique la tarjeta [CREDIT_CARD] y correo [EMAIL_ADDRESS].",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        secret_email = "alice.smith@confidential-corp.org"
        secret_card = "4532712389104455"  # Valid Luhn number
        secret_ssn = "987-65-4321"
        secret_phone = "555-123-4567"

        raw_inquiry = (
            f"Please check refund for card {secret_card}, email {secret_email}, "
            f"SSN {secret_ssn}, and phone {secret_phone}."
        )

        result = await translator.translate(
            text=raw_inquiry,
            source_language="en",
            target_language="es",
        )

        assert result.is_successful is True

        # Inspect the exact prompt dispatched to LLMGateway.generate()
        mock_llm_gateway.generate.assert_awaited_once()
        dispatched_prompt = mock_llm_gateway.generate.call_args.kwargs["prompt"]

        # ZERO RAW PII PROOF: Assert none of the raw sensitive values appear in the prompt
        assert secret_email not in dispatched_prompt
        assert secret_card not in dispatched_prompt
        assert secret_ssn not in dispatched_prompt
        assert secret_phone not in dispatched_prompt

        # Assert masked tokens ARE present matching PIIMasker formats
        assert "****-****-****-4455" in dispatched_prompt
        assert "a***h@confidential-corp.org" in dispatched_prompt
        assert "***-**-4321" in dispatched_prompt
        assert "[PHONE_REDACTED]" in dispatched_prompt


# ==============================================================================
# 3. TRANSLATION CACHING, TTL & LANGUAGE ISOLATION
# ==============================================================================

class TestTranslationCaching:
    """Tests Redis template caching, TTL, language isolation, and PII-free storage."""

    @pytest.mark.asyncio
    async def test_cache_hit_and_miss_lifecycle(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies cache miss triggers LLM, and subsequent identical call is a cache hit."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="¿Cuál es la política de devoluciones?",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        query = "What is the return policy?"

        # Call 1: Cache Miss
        res1 = await translator.translate(query, "en", "es")
        assert res1.is_successful is True
        assert res1.cached is False
        assert mock_llm_gateway.generate.await_count == 1

        # Call 2: Cache Hit (LLM must NOT be called again)
        res2 = await translator.translate(query, "en", "es")
        assert res2.is_successful is True
        assert res2.cached is True
        assert mock_llm_gateway.generate.await_count == 1  # Still 1!
        assert res2.translated_text == res1.translated_text

    @pytest.mark.asyncio
    async def test_language_pair_cache_isolation(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies en -> es and en -> fr for the same query use distinct cache keys and never collide."""
        mock_llm_gateway.generate.side_effect = [
            LLMResponse(content="Hola", provider="gemini", model="gemini-2.5-flash"),
            LLMResponse(content="Bonjour", provider="gemini", model="gemini-2.5-flash"),
        ]

        query = "Hello"

        res_es = await translator.translate(query, "en", "es")
        res_fr = await translator.translate(query, "en", "fr")

        assert res_es.translated_text == "Hola"
        assert res_fr.translated_text == "Bonjour"
        assert mock_llm_gateway.generate.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_stores_zero_raw_pii_or_customer_orders(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies Redis storage contains zero customer emails, cards, or order IDs."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Su pedido __NORM_ORDER_0__ para [EMAIL_ADDRESS] está listo.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        secret_email = "customer@secret.org"
        raw_order = "ORD-PRIVATE-999"
        query = f"Your order {raw_order} for {secret_email} is ready."

        await translator.translate(query, "en", "es")

        # Inspect Redis storage dictionary directly
        redis_storage = mock_redis_service._storage
        assert len(redis_storage) == 1

        for key, cached_val in redis_storage.items():
            assert secret_email not in key
            assert raw_order not in key
            assert secret_email not in cached_val
            assert raw_order not in cached_val
            # Stored value must contain normalized placeholders only
            assert "__NORM_ORDER_0__" in cached_val
            assert "[EMAIL_ADDRESS]" in cached_val

    @pytest.mark.asyncio
    async def test_cross_customer_template_isolation(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies two different customers querying with different orders share the template safely."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="¿Dónde está mi pedido __NORM_ORDER_0__?",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        # Customer 1 with Order A
        vault_1 = EntityPreservationVault(request_id="cust-1")
        res1 = await translator.translate(
            "Where is my order ORD-11111?", "en", "es", vault=vault_1
        )
        assert res1.cached is False
        assert "ORD-11111" in res1.translated_text
        assert "ORD-22222" not in res1.translated_text

        # Customer 2 with Order B (Hits the template cache!)
        vault_2 = EntityPreservationVault(request_id="cust-2")
        res2 = await translator.translate(
            "Where is my order ORD-22222?", "en", "es", vault=vault_2
        )
        assert res2.cached is True  # Cache hit on the normalized template!
        assert "ORD-22222" in res2.translated_text
        assert "ORD-11111" not in res2.translated_text  # ZERO cross-customer contamination


# ==============================================================================
# 4. REDIS RESILIENCE & OUTAGE DEGRADATION
# ==============================================================================

class TestRedisResilience:
    """Verifies that Redis outages do not fail requests and degrade gracefully to direct LLM."""

    @pytest.mark.asyncio
    async def test_redis_connection_outage_does_not_fail_translation(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies that Redis throwing connection errors triggers graceful fallback to direct LLM."""
        mock_redis_service.get.side_effect = RedisConnectionError("Redis connection refused on port 6379")
        mock_redis_service.set.side_effect = RedisConnectionError("Redis connection refused on port 6379")

        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Bonjour tout le monde",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        result = await translator.translate("Hello everyone", "en", "fr")

        # Request succeeds despite Redis outage!
        assert result.is_successful is True
        assert result.translated_text == "Bonjour tout le monde"
        assert result.cached is False


# ==============================================================================
# 5. TRANSLATION FAILURE PROTOCOL (ZERO SILENT ENGLISH)
# ==============================================================================

class TestTranslationFailureProtocol:
    """Verifies that provider outages and corrupted tokens NEVER leak English to foreign customers."""

    @pytest.mark.asyncio
    async def test_provider_outage_returns_localized_notice_spanish(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies that Gemini outage returns Spanish service notice, never English."""
        mock_llm_gateway.generate.side_effect = LLMProviderError("Gemini 503 Service Unavailable")

        result = await translator.translate("How do I return an item?", "en", "es")

        assert result.is_successful is False
        assert result.error_reason == "TRANSLATION_PROVIDER_ERROR"
        # ZERO SILENT ENGLISH: Output MUST be the Spanish localized notice
        assert result.translated_text == LOCALIZED_SERVICE_NOTICES["es"]
        assert "Lo sentimos, nuestro servicio de traducción" in result.translated_text

    @pytest.mark.asyncio
    async def test_provider_timeout_returns_localized_notice_hindi(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies that provider timeout returns Hindi service notice."""
        mock_llm_gateway.generate.side_effect = LLMTimeoutError("LLM call timed out after 5.0s")

        result = await translator.translate("Order status inquiry", "en", "hi")

        assert result.is_successful is False
        assert result.translated_text == LOCALIZED_SERVICE_NOTICES["hi"]
        assert "क्षमा करें, हमारी अनुवाद सेवा" in result.translated_text

    @pytest.mark.asyncio
    async def test_corrupted_placeholder_in_llm_output_triggers_rejection(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies that an LLM mangling or dropping placeholder tokens triggers refusal rather than corrupted text."""
        # LLM dropped the placeholder token in translation
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Su pedido fue enviado sin número.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        raw = "Your order ORD-12345 was shipped."
        vault = EntityPreservationVault(request_id="req-corrupt")

        result = await translator.translate(raw, "en", "es", vault=vault)

        assert result.is_successful is False
        assert "PLACEHOLDER_INTEGRITY_VIOLATION" in result.error_reason
        # Falls back to safe localized notice instead of mangled output
        assert result.translated_text == LOCALIZED_SERVICE_NOTICES["es"]


# ==============================================================================
# 6. PHASE 2 SECURITY AUDIT: NORMALIZED PLACEHOLDER INJECTION DEFENSE
# ==============================================================================

class TestNormalizedPlaceholderInjectionAudit:
    """Verifies that inbound user input attempting to inject internal __NORM_*__ markers is blocked."""

    @pytest.mark.parametrize(
        "token_type,injected_string",
        [
            ("ORDER", "Where is my order __NORM_ORDER_0__?"),
            ("CITATION", "Return policy details __NORM_CIT_1_1__."),
            ("SKU", "I want to exchange item __NORM_SKU_0__."),
            ("AMOUNT", "Please refund me __NORM_AMT_0__ right now."),
            ("DATE", "Shipment was scheduled on __NORM_DATE_0__."),
            ("CODE", "Under policy __NORM_CODE_0__ I want a replacement."),
            ("ENTITY", "Entity reference __NORM_ENTITY_0__."),
        ],
    )
    def test_scanner_blocks_normalized_marker_injection_all_types(
        self, token_type: str, injected_string: str
    ):
        """Verifies PreTranslationSecurityScanner flags every __NORM_*__ token as PLACEHOLDER_PROBE_DETECTED."""
        scanner = PreTranslationSecurityScanner()
        scan_res = scanner.scan(injected_string)
        assert scan_res.is_safe is False
        assert scan_res.risk_detected is True
        assert scan_res.risk_reason == "PLACEHOLDER_PROBE_DETECTED"
        assert scan_res.quarantined is True

    @pytest.mark.asyncio
    async def test_translator_blocks_normalized_marker_injection_defense_in_depth(
        self, translator: Translator
    ):
        """Verifies Translator.translate rejects __NORM_*__ injection if scanner was bypassed."""
        injected = "Please track __NORM_ORDER_0__ for me."
        res = await translator.translate(injected, "en", "es")
        assert res.is_successful is False
        assert res.error_reason == "NORMALIZED_PLACEHOLDER_INJECTION_DETECTED"
        assert res.translated_text == LOCALIZED_SERVICE_NOTICES["es"]

    def test_legitimate_customer_messages_discussing_placeholders_preserved(self):
        """Verifies legitimate customer messages discussing placeholders without exact __NORM_ markers pass."""
        scanner = PreTranslationSecurityScanner()
        benign_messages = [
            "Can you explain how the order placeholder format works?",
            "Please replace the placeholder with my real shipping address.",
            "My order status is still pending, can you help?",
            "What does the placeholder {order_id} mean in the template?",
            "Why does the invoice say [citation needed]?",
        ]
        for msg in benign_messages:
            res = scanner.scan(msg)
            assert res.is_safe is True, f"Failed on benign message: {msg}"
            assert res.risk_detected is False


# ==============================================================================
# 7. PHASE 2 SECURITY AUDIT: CROSS-CUSTOMER CACHE & TEMPLATE ISOLATION
# ==============================================================================

class TestCrossCustomerCacheAudit:
    """Verifies that shared translation cache entries never leak customer data, context, or tokens."""

    @pytest.mark.asyncio
    async def test_three_customers_different_orders_and_context(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Customer A and Customer C share a template with different order IDs.
        Customer B has different surrounding context and gets a separate cache key.
        """
        mock_llm_gateway.generate.side_effect = [
            # Call 1: Customer A
            LLMResponse(
                content="Por favor verifique mi pedido __NORM_ORDER_0__ inmediatamente.",
                provider="gemini",
                model="gemini-2.5-flash",
            ),
            # Call 2: Customer B
            LLMResponse(
                content="¿Podría verificar mi pedido __NORM_ORDER_0__ mañana?",
                provider="gemini",
                model="gemini-2.5-flash",
            ),
        ]

        vault_a = EntityPreservationVault(request_id="cust-A")
        vault_b = EntityPreservationVault(request_id="cust-B")
        vault_c = EntityPreservationVault(request_id="cust-C")

        # Customer A: "Please check my order ORD-11111 immediately."
        res_a = await translator.translate(
            "Please check my order ORD-11111 immediately.", "en", "es", vault=vault_a
        )
        assert res_a.is_successful is True
        assert res_a.cached is False
        assert "ORD-11111" in res_a.translated_text
        assert mock_llm_gateway.generate.await_count == 1

        # Customer B: "Could you please check my order ORD-22222 tomorrow?" (Different context)
        res_b = await translator.translate(
            "Could you please check my order ORD-22222 tomorrow?", "en", "es", vault=vault_b
        )
        assert res_b.is_successful is True
        assert res_b.cached is False
        assert "ORD-22222" in res_b.translated_text
        assert mock_llm_gateway.generate.await_count == 2

        # Customer C: "Please check my order ORD-33333 immediately." (Same template as Customer A, different order)
        res_c = await translator.translate(
            "Please check my order ORD-33333 immediately.", "en", "es", vault=vault_c
        )
        assert res_c.is_successful is True
        assert res_c.cached is True  # Cache HIT on Customer A's template!
        assert mock_llm_gateway.generate.await_count == 2  # Zero additional LLM calls!
        assert "ORD-33333" in res_c.translated_text
        assert "ORD-11111" not in res_c.translated_text
        assert "ORD-22222" not in res_c.translated_text

        # Inspect Redis storage dictionary: ZERO raw order IDs or 128-bit vault tokens
        redis_storage = mock_redis_service._storage
        for key, val in redis_storage.items():
            assert "ORD-11111" not in val
            assert "ORD-22222" not in val
            assert "ORD-33333" not in val
            assert "__PRESERVED_" not in val
            assert "__PRESERVED_" not in key


# ==============================================================================
# 8. PHASE 2 SECURITY AUDIT: CACHE INTEGRITY, VERSIONING & TTL
# ==============================================================================

class TestCacheIntegrityAndVersioningAudit:
    """Verifies pre-denormalization cache validation, prompt/model version binding, and TTL."""

    @pytest.mark.asyncio
    async def test_corrupted_cached_normalized_placeholders_rejected_before_denormalization(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies that if Redis contains corrupted normalized tags, it is rejected and re-translated."""
        # Pre-seed Redis with corrupted cache value (missing placeholder tag)
        norm_key = translator._generate_cache_key("en", "es", "Order __NORM_ORDER_0__ is ready.")
        mock_redis_service._storage[norm_key] = "El pedido está listo sin etiquetas."  # __NORM_ORDER_0__ missing!

        mock_llm_gateway.generate.return_value = LLMResponse(
            content="El pedido __NORM_ORDER_0__ está listo.",
            provider="gemini",
            model="gemini-2.5-flash",
        )

        vault = EntityPreservationVault(request_id="req-corrupt-cache")
        res = await translator.translate("Order ORD-7777 is ready.", "en", "es", vault=vault)

        # Corrupted cache was rejected, fresh LLM generation was called
        assert res.is_successful is True
        assert res.cached is False
        assert "ORD-7777" in res.translated_text
        assert mock_llm_gateway.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_cache_key_binds_prompt_version_and_model_name(
        self, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies that changing prompt_version or model_name changes the cache key and prevents stale cache reuse."""
        t_v1 = Translator(
            llm_gateway=mock_llm_gateway,
            redis_service=mock_redis_service,
            prompt_version="v1.0",
            model_name="gemini-2.5-flash",
        )
        t_v2 = Translator(
            llm_gateway=mock_llm_gateway,
            redis_service=mock_redis_service,
            prompt_version="v2.0",
            model_name="gemini-2.5-flash",
        )
        t_llama = Translator(
            llm_gateway=mock_llm_gateway,
            redis_service=mock_redis_service,
            prompt_version="v1.0",
            model_name="llama3",
        )

        key1 = t_v1._generate_cache_key("en", "es", "Hello world")
        key2 = t_v2._generate_cache_key("en", "es", "Hello world")
        key3 = t_llama._generate_cache_key("en", "es", "Hello world")

        assert key1 != key2
        assert key1 != key3
        assert key2 != key3
        assert "trans:v1.0:gemini-2.5-flash:en:es:" in key1
        assert "trans:v2.0:gemini-2.5-flash:en:es:" in key2
        assert "trans:v1.0:llama3:en:es:" in key3

    @pytest.mark.asyncio
    async def test_redis_ttl_is_exactly_86400_seconds(
        self, translator: Translator, mock_llm_gateway: MagicMock, mock_redis_service: MagicMock
    ):
        """Verifies that cached translations are persisted with exactly 86400s (24h) TTL."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Hola", provider="gemini", model="gemini-2.5-flash"
        )
        await translator.translate("Hello", "en", "es")

        assert mock_redis_service.set.await_count == 1
        _, kwargs = mock_redis_service.set.call_args
        assert kwargs["ttl_seconds"] == 86400


# ==============================================================================
# 9. PHASE 2 SECURITY AUDIT: ALL 8 BIDIRECTIONAL TRANSLATION DIRECTIONS
# ==============================================================================

class TestAllBidirectionalDirectionsAudit:
    """Verifies all 8 language directions (en <-> es, en <-> fr, en <-> de, en <-> hi)."""

    @pytest.mark.asyncio
    async def test_french_to_english_with_order(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies fr -> en direction."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="Where is my order __NORM_ORDER_0__?",
            provider="gemini",
            model="gemini-2.5-flash",
        )
        vault = EntityPreservationVault(request_id="req-fr-en")
        res = await translator.translate("Où est ma commande ORD-9988?", "fr", "en", vault=vault)
        assert res.is_successful is True
        assert res.translated_text == "Where is my order ORD-9988?"

    @pytest.mark.asyncio
    async def test_german_to_english_with_sku(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies de -> en direction."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="I would like to return item __NORM_SKU_0__.",
            provider="gemini",
            model="gemini-2.5-flash",
        )
        vault = EntityPreservationVault(request_id="req-de-en")
        res = await translator.translate("Ich möchte Artikel SKU-4422 zurückgeben.", "de", "en", vault=vault)
        assert res.is_successful is True
        assert res.translated_text == "I would like to return item SKU-4422."

    @pytest.mark.asyncio
    async def test_empty_llm_output_triggers_empty_provider_error(
        self, translator: Translator, mock_llm_gateway: MagicMock
    ):
        """Verifies that an empty LLM response returns localized service notice."""
        mock_llm_gateway.generate.return_value = LLMResponse(
            content="   ", provider="gemini", model="gemini-2.5-flash"
        )
        res = await translator.translate("Check my order ORD-1234", "en", "es")
        assert res.is_successful is False
        assert res.error_reason == "EMPTY_PROVIDER_OUTPUT"
        assert res.translated_text == LOCALIZED_SERVICE_NOTICES["es"]

