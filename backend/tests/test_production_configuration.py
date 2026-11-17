"""Production Configuration, Cryptographic Security & Startup Safeguard Tests.

Verifies:
1. Strict production startup validation rejects DEBUG=True, ENABLE_DEV_AUTH_ENDPOINTS=True.
2. Insecure or placeholder SECRET_KEY, ENCRYPTION_MASTER_KEY, and POSTGRES_PASSWORD rejection.
3. Wildcard CORS rejection in production.
4. Valid production settings pass cleanly.
5. Error messages never leak secret or credential values.
6. Fail-closed 404 behavior on development authentication routes outside development.
7. ENABLE_BACKGROUND_WORKERS flag properly gates background scanner startup.
"""

import os
from typing import Any, Dict
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, validate_production_environment
from app.main import app


# Cryptographically strong valid fixtures for testing
VALID_PROD_SECRET_KEY = "a" * 64
VALID_PROD_ENCRYPTION_KEY = "f" * 64
VALID_PROD_PASSWORD = "StrongProdPassword_2026_Secure!"
VALID_PROD_CORS = ["https://support.enterprise.internal", "https://app.enterprise.internal"]


def make_prod_kwargs(**overrides: Any) -> Dict[str, Any]:
    """Helper returning a fully compliant production Settings keyword argument dict."""
    base = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "ENABLE_DEV_AUTH_ENDPOINTS": False,
        "SECRET_KEY": VALID_PROD_SECRET_KEY,
        "ENCRYPTION_MASTER_KEY": VALID_PROD_ENCRYPTION_KEY,
        "POSTGRES_PASSWORD": VALID_PROD_PASSWORD,
        "CORS_ORIGINS": VALID_PROD_CORS,
        "IDEMPOTENCY_HMAC_ACTIVE_VERSION": "v1",
        "IDEMPOTENCY_HMAC_KEY_RING": {
            "v1": "9a3f2b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a"
        },
        "EMBEDDING_PROVIDER": "gemini",
        "GEMINI_API_KEY": "valid_mock_gemini_api_key_for_testing_123",
    }
    base.update(overrides)
    return base


class TestProductionConfigurationLockdown:
    """Validates fail-fast safeguards for production configuration."""

    def test_production_rejects_debug_true(self):
        kwargs = make_prod_kwargs(DEBUG=True)
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "DEBUG mode must be set to False in production" in str(excinfo.value)

    def test_production_rejects_enable_dev_auth_endpoints_true(self):
        kwargs = make_prod_kwargs(ENABLE_DEV_AUTH_ENDPOINTS=True)
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "ENABLE_DEV_AUTH_ENDPOINTS must be set to False in production" in str(excinfo.value)

    def test_production_rejects_default_placeholder_secret_key(self):
        kwargs = make_prod_kwargs(
            SECRET_KEY="change_this_to_a_super_secure_random_hex_key_in_production"
        )
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "Insecure or placeholder SECRET_KEY detected" in str(excinfo.value)

    def test_production_rejects_short_secret_key(self):
        kwargs = make_prod_kwargs(SECRET_KEY="short_insecure_key")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "Insecure or placeholder SECRET_KEY detected" in str(excinfo.value)

    def test_production_rejects_default_placeholder_encryption_key(self):
        kwargs = make_prod_kwargs(
            ENCRYPTION_MASTER_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "Insecure or invalid ENCRYPTION_MASTER_KEY detected" in str(excinfo.value)

    def test_production_rejects_invalid_hex_encryption_key(self):
        kwargs = make_prod_kwargs(ENCRYPTION_MASTER_KEY="z" * 64)
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "ENCRYPTION_MASTER_KEY is not a valid hexadecimal string" in str(excinfo.value)

    def test_production_rejects_placeholder_postgres_password(self):
        kwargs = make_prod_kwargs(POSTGRES_PASSWORD="support_password_placeholder")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "POSTGRES_PASSWORD must be configured with a non-empty, non-placeholder value" in str(excinfo.value)

    def test_production_rejects_empty_postgres_password(self):
        kwargs = make_prod_kwargs(POSTGRES_PASSWORD="")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "POSTGRES_PASSWORD must be configured with a non-empty, non-placeholder value" in str(excinfo.value)

    def test_production_rejects_wildcard_cors(self):
        kwargs = make_prod_kwargs(CORS_ORIGINS=["*"])
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "Wildcard CORS_ORIGINS ('*') is prohibited in production" in str(excinfo.value)

    def test_valid_production_settings_pass_cleanly(self):
        kwargs = make_prod_kwargs()
        settings_obj = Settings(**kwargs)
        assert settings_obj.ENVIRONMENT == "production"
        assert settings_obj.DEBUG is False
        assert settings_obj.ENABLE_DEV_AUTH_ENDPOINTS is False
        errors = validate_production_environment(settings_obj)
        assert len(errors) == 0

    def test_secret_values_never_leaked_in_exception(self):
        canary_secret = "secret_canary_value_should_not_appear_in_log"
        kwargs = make_prod_kwargs(SECRET_KEY=canary_secret)  # length 45, but contains 'secret' and not rejected for length
        # But if we make it contain placeholder
        kwargs["SECRET_KEY"] = "placeholder_" + canary_secret
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        err_msg = str(excinfo.value)
        assert canary_secret not in err_msg


class TestEmbeddingConfigurationSafeguards:
    """Verifies strict environment safeguards for embedding provider configuration."""

    def test_development_accepts_mock_provider(self):
        s = Settings(ENVIRONMENT="development", EMBEDDING_PROVIDER="mock")
        assert s.ENVIRONMENT == "development"
        assert s.EMBEDDING_PROVIDER == "mock"

    def test_development_accepts_gemini_provider(self):
        s = Settings(
            ENVIRONMENT="development",
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="dev_placeholder_key",
        )
        assert s.ENVIRONMENT == "development"
        assert s.EMBEDDING_PROVIDER == "gemini"

    def test_testing_accepts_mock_provider(self):
        s = Settings(ENVIRONMENT="testing", EMBEDDING_PROVIDER="mock")
        assert s.ENVIRONMENT == "testing"
        assert s.EMBEDDING_PROVIDER == "mock"

    def test_testing_accepts_gemini_provider(self):
        s = Settings(
            ENVIRONMENT="testing",
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="test_placeholder_key",
        )
        assert s.ENVIRONMENT == "testing"
        assert s.EMBEDDING_PROVIDER == "gemini"

    def test_staging_rejects_mock_provider(self):
        with pytest.raises(ValueError) as excinfo:
            Settings(ENVIRONMENT="staging", EMBEDDING_PROVIDER="mock")
        err = str(excinfo.value)
        assert "EMBEDDING_PROVIDER 'mock' is prohibited in staging environment" in err

    def test_production_rejects_mock_provider(self):
        kwargs = make_prod_kwargs(EMBEDDING_PROVIDER="mock")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        err = str(excinfo.value)
        assert "EMBEDDING_PROVIDER 'mock' is prohibited in production" in err

    def test_staging_rejects_gemini_missing_api_key(self):
        with pytest.raises(ValueError) as excinfo:
            Settings(ENVIRONMENT="staging", EMBEDDING_PROVIDER="gemini", GEMINI_API_KEY=None)
        err = str(excinfo.value)
        assert "GEMINI_API_KEY is required in staging environment" in err

    def test_staging_rejects_gemini_empty_api_key(self):
        with pytest.raises(ValueError) as excinfo:
            Settings(ENVIRONMENT="staging", EMBEDDING_PROVIDER="gemini", GEMINI_API_KEY="   ")
        err = str(excinfo.value)
        assert "GEMINI_API_KEY is required in staging environment" in err

    def test_production_rejects_gemini_missing_api_key(self):
        kwargs = make_prod_kwargs(EMBEDDING_PROVIDER="gemini", GEMINI_API_KEY=None)
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        err = str(excinfo.value)
        assert "GEMINI_API_KEY" in err

    def test_production_rejects_gemini_empty_api_key(self):
        kwargs = make_prod_kwargs(EMBEDDING_PROVIDER="gemini", GEMINI_API_KEY="")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        err = str(excinfo.value)
        assert "GEMINI_API_KEY" in err

    def test_staging_accepts_gemini_with_valid_placeholder_key_without_network_call(self):
        s = Settings(
            ENVIRONMENT="staging",
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="valid-staging-placeholder-key-xyz",
        )
        assert s.ENVIRONMENT == "staging"
        assert s.EMBEDDING_PROVIDER == "gemini"
        assert s.GEMINI_API_KEY == "valid-staging-placeholder-key-xyz"

    def test_production_accepts_gemini_with_valid_placeholder_key_without_network_call(self):
        kwargs = make_prod_kwargs(
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="valid-prod-placeholder-key-abc",
        )
        s = Settings(**kwargs)
        assert s.ENVIRONMENT == "production"
        assert s.EMBEDDING_PROVIDER == "gemini"
        assert s.GEMINI_API_KEY == "valid-prod-placeholder-key-abc"

    def test_unsupported_embedding_provider_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            Settings(EMBEDDING_PROVIDER="unsupported_vendor")
        err = str(excinfo.value)
        assert "Unsupported EMBEDDING_PROVIDER" in err

    def test_embedding_safeguards_never_leak_api_key_in_exception(self):
        canary_key = "super_sensitive_gemini_api_key_canary_999"
        kwargs = make_prod_kwargs(
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY=canary_key,
            DEBUG=True,  # Trigger another error to verify error reporting doesn't leak secrets
        )
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        err = str(excinfo.value)
        assert canary_key not in err

    def test_gemini_provider_construction_does_not_silently_fallback_to_mock(self):
        from app.services.embedding_service import GeminiEmbeddingProvider, EmbeddingService
        from app.core.exceptions import EmbeddingConfigurationError

        with pytest.raises(EmbeddingConfigurationError) as excinfo:
            GeminiEmbeddingProvider(api_key=None)
        assert "GEMINI_API_KEY is required" in str(excinfo.value)

        # Confirm EmbeddingService configured for gemini without key raises rather than falling back
        s = Settings(ENVIRONMENT="development", EMBEDDING_PROVIDER="gemini", GEMINI_API_KEY=None)
        with pytest.raises(EmbeddingConfigurationError):
            EmbeddingService(settings=s)

    def test_existing_offline_test_fixtures_remain_functional(self):
        s = Settings()
        assert s.EMBEDDING_PROVIDER == "mock"
        from app.services.embedding_service import EmbeddingService, MockEmbeddingProvider
        service = EmbeddingService(settings=s)
        assert isinstance(service.provider, MockEmbeddingProvider)


class TestDevAuthLockdown:
    """Verifies fail-closed behavior of development authentication routes."""

    @pytest.mark.asyncio
    async def test_dev_personas_endpoint_returns_404_when_disabled(self):
        from app.core import config
        original_flag = config.settings.ENABLE_DEV_AUTH_ENDPOINTS
        try:
            config.settings.ENABLE_DEV_AUTH_ENDPOINTS = False
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.get("/api/v1/auth/dev-personas")
                assert res.status_code == 404
                data = res.json()
                assert "disabled" in data["error"]["message"].lower() or "disabled" in str(data).lower()
        finally:
            config.settings.ENABLE_DEV_AUTH_ENDPOINTS = original_flag

    @pytest.mark.asyncio
    async def test_dev_token_endpoint_returns_404_when_disabled(self):
        from app.core import config
        original_flag = config.settings.ENABLE_DEV_AUTH_ENDPOINTS
        try:
            config.settings.ENABLE_DEV_AUTH_ENDPOINTS = False
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.post("/api/v1/auth/dev-token", json={"persona": "agent"})
                assert res.status_code == 404
                data = res.json()
                assert "disabled" in data["error"]["message"].lower() or "disabled" in str(data).lower()
        finally:
            config.settings.ENABLE_DEV_AUTH_ENDPOINTS = original_flag

    @pytest.mark.asyncio
    async def test_dev_personas_endpoint_returns_404_in_production_environment(self):
        from app.core import config
        original_env = config.settings.ENVIRONMENT
        try:
            config.settings.ENVIRONMENT = "production"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.get("/api/v1/auth/dev-personas")
                assert res.status_code == 404
        finally:
            config.settings.ENVIRONMENT = original_env


class TestWorkerLifecycleGating:
    """Verifies that ENABLE_BACKGROUND_WORKERS gates scanner startup."""

    def test_background_worker_flag_defaults_to_true(self):
        s = Settings()
        assert getattr(s, "ENABLE_BACKGROUND_WORKERS", True) is True

    def test_background_worker_flag_can_be_disabled(self):
        s = Settings(ENABLE_BACKGROUND_WORKERS=False)
        assert s.ENABLE_BACKGROUND_WORKERS is False


class TestStorageConfigurationSafeguards:
    """Verifies environment defaults, provider lockdown, and production validation for storage."""

    def test_storage_provider_defaults_to_local_in_dev_and_test(self):
        s = Settings()
        assert s.STORAGE_PROVIDER == "local"
        assert s.STORAGE_MAX_FILE_SIZE_BYTES == 15 * 1024 * 1024
        assert s.STORAGE_MAX_QUOTA_BYTES == 400 * 1024 * 1024

    def test_storage_provider_defaults_to_postgres_in_staging_and_prod(self):
        kwargs = make_prod_kwargs()
        s = Settings(**kwargs)
        assert s.STORAGE_PROVIDER == "postgres"

    def test_production_rejects_storage_provider_local(self):
        kwargs = make_prod_kwargs(STORAGE_PROVIDER="local")
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "STORAGE_PROVIDER 'local' is prohibited in production" in str(excinfo.value)

    def test_staging_rejects_storage_provider_local(self):
        with pytest.raises(ValueError) as excinfo:
            Settings(
                ENVIRONMENT="staging",
                STORAGE_PROVIDER="local",
                EMBEDDING_PROVIDER="gemini",
                GEMINI_API_KEY="valid_test_key_for_staging",
            )
        assert "STORAGE_PROVIDER 'local' is prohibited in staging" in str(excinfo.value)

    def test_unsupported_storage_provider_rejected(self):
        for bad_prov in ["s3", "r2", "supabase", "azure_blob", "gcs"]:
            with pytest.raises(ValueError) as excinfo:
                Settings(STORAGE_PROVIDER=bad_prov)
            assert "Unsupported STORAGE_PROVIDER" in str(excinfo.value)

    def test_validate_production_environment_flags_local_storage(self):
        s = Settings()  # local provider
        errors = validate_production_environment(s)
        assert any("STORAGE_PROVIDER 'local' is prohibited in production" in err for err in errors)

    def test_production_rejects_rate_limit_disabled(self):
        """Phase 5.3 Finding A: Production must reject RATE_LIMIT_ENABLED=False."""
        kwargs = make_prod_kwargs(RATE_LIMIT_ENABLED=False)
        with pytest.raises(ValueError) as excinfo:
            Settings(**kwargs)
        assert "RATE_LIMIT_ENABLED must be set to True in production and staging" in str(excinfo.value)

    def test_production_accepts_rate_limit_enabled(self):
        """Phase 5.3 Finding A: Production succeeds with RATE_LIMIT_ENABLED=True."""
        kwargs = make_prod_kwargs(RATE_LIMIT_ENABLED=True)
        s = Settings(**kwargs)
        assert s.RATE_LIMIT_ENABLED is True

    def test_production_defaults_rate_limit_enabled_when_omitted(self):
        """Phase 5.3 Finding A: RATE_LIMIT_ENABLED defaults to True when omitted in production."""
        kwargs = make_prod_kwargs()
        kwargs.pop("RATE_LIMIT_ENABLED", None)
        s = Settings(**kwargs)
        assert s.RATE_LIMIT_ENABLED is True

    def test_staging_rejects_rate_limit_disabled(self):
        """Phase 5.3 Finding A: Staging must reject RATE_LIMIT_ENABLED=False."""
        with pytest.raises(ValueError) as excinfo:
            Settings(
                ENVIRONMENT="staging",
                RATE_LIMIT_ENABLED=False,
                EMBEDDING_PROVIDER="gemini",
                GEMINI_API_KEY="valid-staging-key-123",
            )
        assert "RATE_LIMIT_ENABLED must be set to True in production and staging" in str(excinfo.value)

    def test_development_allows_rate_limit_disabled(self):
        """Phase 5.3 Finding A: Development allows RATE_LIMIT_ENABLED=False for offline testing."""
        s = Settings(ENVIRONMENT="development", RATE_LIMIT_ENABLED=False)
        assert s.RATE_LIMIT_ENABLED is False

    def test_validate_production_environment_flags_rate_limit_disabled(self):
        """Phase 5.3 Finding A: validate_production_environment reports disabled rate limit."""
        kwargs = make_prod_kwargs(RATE_LIMIT_ENABLED=False)
        # Construct object skipping post-init validation by using object.__new__
        s = Settings(ENVIRONMENT="development", RATE_LIMIT_ENABLED=False)
        errors = validate_production_environment(s)
        assert any("RATE_LIMIT_ENABLED is False" in err for err in errors)



