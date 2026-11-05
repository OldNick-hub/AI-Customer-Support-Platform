"""Test Suite for Day 03 Phase 1 & Phase 2.

Covers:
- Configuration validation, defaults, overrides, computed redis_url, and missing key resilience
- Standardized platform exceptions hierarchy, error codes, HTTP status mapping, and client serialization
- PII masking service: individual entity detection, combined text, exact original character offsets,
  Luhn algorithm enforcement, payment data isolation gates, and edge cases
- Recursive dictionary payload sanitization with sensitive key redaction
- Safe logging filter and formatter preventing log leakage
"""

import io
import logging
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingModelLoadError,
    LLMAuthenticationError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    PlatformException,
    RedisConnectionError,
    RedisError,
    RedisTimeoutError,
)
from app.core.logging import (
    PIISanitizingFormatter,
    SafeLoggingFilter,
    configure_safe_logger,
)
from app.services.pii_masker import (
    PIIEntityType,
    PIIMasker,
    PIIMaskResult,
    get_pii_masker,
)


# ==============================================================================
# PHASE 1 TESTS: Configuration & Exceptions
# ==============================================================================


class TestConfiguration:
    """Verifies settings validation, defaults, and computed properties."""

    def test_default_configuration_values(self):
        """Asserts proper defaults for LLM, Embedding, and Redis settings."""
        settings = Settings()
        assert settings.PRIMARY_LLM_PROVIDER in ("gemini", "ollama")
        assert settings.GEMINI_MODEL == "gemini-3.8-flash"
        assert settings.OLLAMA_BASE_URL == "http://localhost:11434"
        assert settings.OLLAMA_MODEL == "llama3:latest"
        assert settings.ENABLE_OLLAMA_FALLBACK is True
        assert settings.LLM_TIMEOUT_SECONDS == 30.0
        assert settings.LLM_MAX_RETRIES == 3

        assert settings.EMBEDDING_MODEL_NAME == "sentence-transformers/all-MiniLM-L6-v2"
        assert settings.EMBEDDING_DIMENSION == 384
        assert settings.EMBEDDING_BATCH_SIZE == 32

        assert settings.REDIS_HOST == "localhost"
        assert settings.REDIS_PORT == 6379
        assert settings.REDIS_DB == 0
        assert settings.REDIS_SOCKET_TIMEOUT == 5.0
        assert settings.REDIS_CONNECT_TIMEOUT == 5.0
        assert settings.REDIS_MAX_CONNECTIONS == 50

    def test_redis_url_computed_without_password(self):
        """Asserts standard redis:// URL is constructed when no password is set."""
        cfg = Settings(
            REDIS_PASSWORD=None,
            REDIS_HOST="127.0.0.1",
            REDIS_PORT=6379,
            REDIS_DB=0,
            REDIS_URL=None,
        )
        assert cfg.redis_url == "redis://127.0.0.1:6379/0"

    def test_redis_url_computed_with_password(self):
        """Asserts password URL-encoding in computed redis:// URL."""
        cfg = Settings(
            REDIS_PASSWORD="secret@pass:word#123",
            REDIS_HOST="redis.internal",
            REDIS_PORT=6380,
            REDIS_DB=2,
            REDIS_URL=None,
        )
        assert cfg.redis_url == "redis://:secret%40pass%3Aword%23123@redis.internal:6380/2"

    def test_redis_url_explicit_override(self):
        """Asserts explicit REDIS_URL takes precedence over computed host/port/db."""
        custom_url = "rediss://cluster.example.com:6380/9?ssl_cert_reqs=required"
        cfg = Settings(REDIS_URL=custom_url)
        assert cfg.redis_url == custom_url

    def test_config_validation_rules(self):
        """Asserts invalid configurations raise descriptive ValidationError."""
        # Invalid LLM provider
        with pytest.raises(ValidationError) as exc_info:
            Settings(PRIMARY_LLM_PROVIDER="unsupported_provider")
        assert "PRIMARY_LLM_PROVIDER must be 'gemini' or 'ollama'" in str(exc_info.value)

        # Invalid non-positive embedding dimension
        with pytest.raises(ValidationError):
            Settings(EMBEDDING_DIMENSION=0)

        # Invalid negative embedding batch size
        with pytest.raises(ValidationError):
            Settings(EMBEDDING_BATCH_SIZE=-5)

        # Invalid zero/negative LLM timeout
        with pytest.raises(ValidationError):
            Settings(LLM_TIMEOUT_SECONDS=0.0)

        # Invalid negative retry count
        with pytest.raises(ValidationError):
            Settings(LLM_MAX_RETRIES=-1)

    def test_gemini_api_key_missing_resilience(self):
        """Asserts missing or empty Gemini API key does not crash configuration import."""
        cfg = Settings(GEMINI_API_KEY=None)
        assert cfg.GEMINI_API_KEY is None

    def test_alias_and_cross_spec_settings(self):
        """Asserts aliases and cross-spec aliases for retry limit, primary provider and fallback."""
        cfg1 = Settings(LLM_RETRY_LIMIT=5, LLM_PRIMARY_PROVIDER="ollama", LLM_FALLBACK_PROVIDER="none")
        assert cfg1.LLM_MAX_RETRIES == 5
        assert cfg1.LLM_RETRY_LIMIT == 5
        assert cfg1.PRIMARY_LLM_PROVIDER == "ollama"
        assert cfg1.LLM_PRIMARY_PROVIDER == "ollama"
        assert cfg1.LLM_FALLBACK_PROVIDER == "none"

        cfg2 = Settings(LLM_MAX_RETRIES=2, PRIMARY_LLM_PROVIDER="gemini")
        assert cfg2.LLM_RETRY_LIMIT == 2
        assert cfg2.LLM_PRIMARY_PROVIDER == "gemini"


class TestStandardizedExceptions:
    """Verifies exception inheritance, error codes, HTTP status codes, and serialization."""

    def test_exception_inheritance_and_hierarchy(self):
        """Verifies clean inheritance from PlatformException and domain bases."""
        assert issubclass(LLMError, PlatformException)
        assert issubclass(LLMProviderError, LLMError)
        assert issubclass(LLMTimeoutError, LLMError)
        assert issubclass(LLMAuthenticationError, LLMError)

        assert issubclass(EmbeddingError, PlatformException)
        assert issubclass(EmbeddingDimensionMismatchError, EmbeddingError)
        assert issubclass(EmbeddingModelLoadError, EmbeddingError)

        assert issubclass(RedisError, PlatformException)
        assert issubclass(RedisConnectionError, RedisError)
        assert issubclass(RedisTimeoutError, RedisError)

    def test_exception_status_codes_and_error_codes(self):
        """Verifies correct HTTP status code and error code mapping."""
        e1 = LLMProviderError(provider="gemini")
        assert e1.status_code == 502
        assert e1.error_code == "LLM_PROVIDER_ERROR"

        e2 = LLMTimeoutError(provider="ollama", timeout_seconds=30.0)
        assert e2.status_code == 504
        assert e2.error_code == "LLM_TIMEOUT"

        e3 = LLMAuthenticationError(provider="gemini")
        assert e3.status_code == 401
        assert e3.error_code == "LLM_AUTH_ERROR"

        e4 = EmbeddingDimensionMismatchError(expected_dim=384, actual_dim=768)
        assert e4.status_code == 500
        assert e4.error_code == "EMBEDDING_DIMENSION_MISMATCH"

        e5 = EmbeddingModelLoadError(model_name="all-MiniLM-L6-v2")
        assert e5.status_code == 503
        assert e5.error_code == "EMBEDDING_MODEL_LOAD_ERROR"

        e6 = RedisConnectionError(host="localhost", port=6379)
        assert e6.status_code == 503
        assert e6.error_code == "REDIS_CONNECTION_ERROR"

        e7 = RedisTimeoutError()
        assert e7.status_code == 504
        assert e7.error_code == "REDIS_TIMEOUT"

    def test_client_safe_serialization(self):
        """Verifies to_client_dict() provides structured, safe payloads for API callers."""
        err = EmbeddingDimensionMismatchError(expected_dim=384, actual_dim=512)
        payload = err.to_client_dict()

        assert "error" in payload
        assert payload["error"]["code"] == "EMBEDDING_DIMENSION_MISMATCH"
        assert payload["error"]["status_code"] == 500
        assert "expected 384, got 512" in payload["error"]["message"]
        assert payload["error"]["detail"] == {
            "expected_dimension": 384,
            "actual_dimension": 512,
        }
        # to_dict alias parity
        assert err.to_dict() == payload


# ==============================================================================
# PHASE 2 TESTS: PII Masking Service & Safe Logging
# ==============================================================================


class TestPIIMaskingService:
    """Verifies individual PII types, structured results, exact offsets, and Luhn checks."""

    @pytest.fixture
    def masker(self) -> PIIMasker:
        return get_pii_masker()

    def test_mask_credit_card_valid_luhn(self, masker: PIIMasker):
        """Asserts valid Visa test card is masked to last 4 digits and flags payment."""
        text = "Please charge card 4111 1111 1111 1111 for my order."
        res = masker.mask(text)

        assert "****-****-****-1111" in res.masked_text
        assert "4111 1111 1111" not in res.masked_text
        assert res.has_payment_data is True
        assert res.entity_counts["CREDIT_CARD"] == 1

        # Check exact offsets against original text
        card_entity = next(e for e in res.detected_entities if e.entity_type == PIIEntityType.CREDIT_CARD)
        assert text[card_entity.start : card_entity.end] == "4111 1111 1111 1111"

    def test_luhn_validation_rejects_fake_card(self, masker: PIIMasker):
        """Asserts invalid digit sequence is NOT masked as card and payment flag is False."""
        # 4111 1111 1111 1112 fails Luhn check
        fake_card_text = "Tracking reference 4111 1111 1111 1112 is registered."
        res = masker.mask(fake_card_text)

        assert "4111 1111 1111 1112" in res.masked_text
        assert res.has_payment_data is False
        assert res.entity_counts["CREDIT_CARD"] == 0
        assert len(res.detected_entities) == 0

    def test_mask_cvv(self, masker: PIIMasker):
        """Asserts CVV/CVC is redacted and flags payment data."""
        text = "The CVV: 789 and security code 1234 are required."
        res = masker.mask(text)

        assert "CVV: [REDACTED]" in res.masked_text
        assert "789" not in res.masked_text
        assert res.has_payment_data is True
        assert res.entity_counts["CVV"] >= 1

    def test_mask_ssn(self, masker: PIIMasker):
        """Asserts SSN is masked preserving only the last 4 digits."""
        text = "Customer identity verified with SSN 123-45-6789."
        res = masker.mask(text)

        assert "***-**-6789" in res.masked_text
        assert "123-45-" not in res.masked_text
        assert res.has_payment_data is False
        assert res.entity_counts["SSN"] == 1

        ssn_entity = next(e for e in res.detected_entities if e.entity_type == PIIEntityType.SSN)
        assert text[ssn_entity.start : ssn_entity.end] == "123-45-6789"

    def test_mask_email(self, masker: PIIMasker):
        """Asserts email is masked preserving first and last character of local part."""
        text = "Send confirmation to support.lead@enterprise.com."
        res = masker.mask(text)

        assert "s***d@enterprise.com" in res.masked_text
        assert "support.lead@" not in res.masked_text
        assert res.entity_counts["EMAIL"] == 1

        email_entity = next(e for e in res.detected_entities if e.entity_type == PIIEntityType.EMAIL)
        assert text[email_entity.start : email_entity.end] == "support.lead@enterprise.com"

    def test_mask_phone_number(self, masker: PIIMasker):
        """Asserts phone numbers are redacted."""
        text = "Call us at +1 (555) 234-5678 or 555-019-2834."
        res = masker.mask(text)

        assert "[PHONE_REDACTED]" in res.masked_text
        assert "555" not in res.masked_text
        assert res.entity_counts["PHONE"] == 2

    def test_combined_pii_masking_with_exact_original_offsets(self, masker: PIIMasker):
        """Asserts multiple distinct PII entities in one utterance have exact original offsets."""
        text = (
            "User alice@example.com called +1 (555) 234-5678 with SSN 987-65-4321 "
            "requesting refund to card 4111 1111 1111 1111, CVV: 456."
        )
        res = masker.mask(text)

        # All 5 types detected
        assert res.entity_counts["EMAIL"] == 1
        assert res.entity_counts["PHONE"] == 1
        assert res.entity_counts["SSN"] == 1
        assert res.entity_counts["CREDIT_CARD"] == 1
        assert res.entity_counts["CVV"] == 1
        assert res.has_payment_data is True

        # CRITICAL TEST: Every detected entity's (start, end) matches the original text slice
        for entity in res.detected_entities:
            extracted = text[entity.start : entity.end]
            if entity.entity_type == PIIEntityType.EMAIL:
                assert extracted == "alice@example.com"
            elif entity.entity_type == PIIEntityType.PHONE:
                assert extracted == "+1 (555) 234-5678"
            elif entity.entity_type == PIIEntityType.SSN:
                assert extracted == "987-65-4321"
            elif entity.entity_type == PIIEntityType.CREDIT_CARD:
                assert extracted == "4111 1111 1111 1111"
            elif entity.entity_type == PIIEntityType.CVV:
                assert extracted == "CVV: 456"

    def test_edge_cases_empty_none_and_nonstring(self, masker: PIIMasker):
        """Asserts graceful handling of None, empty strings, and non-string inputs."""
        # Empty string
        res_empty = masker.mask("")
        assert res_empty.masked_text == ""
        assert len(res_empty.detected_entities) == 0
        assert res_empty.has_payment_data is False

        # None value
        res_none = masker.mask(None)
        assert res_none.masked_text == ""
        assert len(res_none.detected_entities) == 0
        assert res_none.has_payment_data is False

        # Whitespace only
        res_ws = masker.mask("   ")
        assert res_ws.masked_text == "   "
        assert len(res_ws.detected_entities) == 0

        # Integer input coerced
        res_int = masker.mask(123456789)  # type: ignore[arg-type]
        assert res_int.masked_text == "123456789"

        # Text with no PII
        clean_text = "The quick brown fox jumps over the lazy dog."
        res_clean = masker.mask(clean_text)
        assert res_clean.masked_text == clean_text
        assert len(res_clean.detected_entities) == 0
        assert res_clean.has_payment_data is False

    def test_recursive_dictionary_sanitization(self, masker: PIIMasker):
        """Asserts nested dictionary payload sanitization with key redaction and value masking."""
        raw_payload = {
            "session_id": "sess-1234",
            "password": "SuperSecretPassword!",
            "client_secret": "oauth-client-secret-value",
            "user_data": {
                "name": "Jane Doe",
                "email": "jane.doe@enterprise.com",
                "contact_number": "555-123-4567",
                "api_key": "live_sk_abcdef123456",
                "notes": "Customer asked about card 4111 1111 1111 1111 expiration.",
            },
            "payment_records": [
                {
                    "card": "4111 1111 1111 1111",
                    "cvv": "987",
                    "amount": 199.99,
                    "currency": "USD",
                    "successful": True,
                },
                {
                    "card_type": "Visa",
                    "status": "SETTLED",
                    "refund_requested": None,
                },
            ],
            "metadata": {
                "author": "System Agent",
                "auth_token": "jwt-bearer-token-12345",
            },
        }

        sanitized = masker.sanitize_dict(raw_payload)

        # Sensitive keys must be fully redacted
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["client_secret"] == "[REDACTED]"
        assert sanitized["user_data"]["api_key"] == "[REDACTED]"
        assert sanitized["payment_records"][0]["card"] == "[REDACTED]"
        assert sanitized["payment_records"][0]["cvv"] == "[REDACTED]"
        assert sanitized["metadata"]["auth_token"] == "[REDACTED]"

        # Non-sensitive keys with PII in string values must be masked
        assert sanitized["user_data"]["email"] == "j***e@enterprise.com"
        assert sanitized["user_data"]["contact_number"] == "[PHONE_REDACTED]"
        assert "****-****-****-1111" in sanitized["user_data"]["notes"]
        assert "4111 1111" not in sanitized["user_data"]["notes"]

        # Legitimate keys containing subwords like 'author' or 'card_type' must be preserved
        assert sanitized["metadata"]["author"] == "System Agent"
        assert sanitized["payment_records"][1]["card_type"] == "Visa"

        # Primitive values (numbers, booleans, None) must be preserved
        assert sanitized["payment_records"][0]["amount"] == 199.99
        assert sanitized["payment_records"][0]["successful"] is True
        assert sanitized["payment_records"][1]["refund_requested"] is None

        # Original payload must NOT be mutated
        assert raw_payload["password"] == "SuperSecretPassword!"
        assert raw_payload["user_data"]["email"] == "jane.doe@enterprise.com"


class TestSafeLogging:
    """Verifies that SafeLoggingFilter and PIISanitizingFormatter block PII from log output."""

    def test_safe_logging_filter_and_formatter(self):
        """Asserts stream logs contain only masked/redacted PII across messages and arguments."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(PIISanitizingFormatter("%(levelname)s - %(message)s"))

        logger = logging.getLogger("test_safe_audit")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.addFilter(SafeLoggingFilter())

        # 1. Plain string message with card and email
        logger.info("Processing order for 4111 1111 1111 1111 and john.smith@company.com")

        # 2. String with arguments
        logger.info(
            "Customer phone %s provided SSN %s",
            "+1 (555) 234-5678",
            "123-45-6789",
        )

        # 3. Dict argument with credentials and PII
        logger.info(
            "Received payload: %s",
            {"password": "MySecretPass!123", "email": "customer@gmail.com", "card": "4111 1111 1111 1111"},
        )

        output = stream.getvalue()

        # Zero raw credit cards, SSNs, passwords, or emails in log output
        assert "4111 1111 1111 1111" not in output
        assert "123-45-6789" not in output
        assert "MySecretPass!123" not in output
        assert "john.smith@" not in output
        assert "customer@" not in output

        # Masked values must appear
        assert "****-****-****-1111" in output
        assert "j***h@company.com" in output
        assert "[PHONE_REDACTED]" in output
        assert "***-**-6789" in output
        assert "[REDACTED]" in output
        assert "c***r@gmail.com" in output

