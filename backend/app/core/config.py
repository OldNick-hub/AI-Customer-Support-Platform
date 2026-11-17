"""Application Configuration and Environment Settings.

Provides centralized, type-safe settings management via Pydantic BaseSettings.
All configuration parameters are read from environment variables or .env files.
"""

import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
from pydantic import AliasChoices, Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Application & Environment ---
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    APP_NAME: str = "Intelligent Customer Support AI Platform"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]

    # --- Security & Cryptography ---
    SECRET_KEY: str = "change_this_to_a_super_secure_random_hex_key_in_production"
    # 32-byte hex key (64 hex chars) for AES-256-GCM encryption
    ENCRYPTION_MASTER_KEY: str = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ENABLE_DEV_AUTH_ENDPOINTS: bool = True  # Safe local development persona endpoints

    # --- PostgreSQL Database ---
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "ai_customer_support"

    # Dedicated Migration Administrator Credentials (DDL / Schema Owner)
    MIGRATION_POSTGRES_USER: str = "migration_admin"
    MIGRATION_POSTGRES_PASSWORD: str = ""
    MIGRATION_DATABASE_URL: Optional[str] = None
    MIGRATION_SYNC_DATABASE_URL: Optional[str] = None

    # Optional direct override for tests or custom connection strings
    DATABASE_URL: Optional[str] = None
    SYNC_DATABASE_URL: Optional[str] = None

    # Connection pool tuning
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: float = 30.0
    DB_POOL_RECYCLE: int = 1800  # 30 minutes

    # --- Non-production Seed Credentials ---
    SEED_ADMIN_EMAIL: str = "admin@enterprise.internal"
    SEED_ADMIN_PASSWORD: str = "AdminDevTestPass!2026"
    SEED_SUPERVISOR_EMAIL: str = "supervisor@enterprise.internal"
    SEED_SUPERVISOR_PASSWORD: str = "SupervisorDevPass!2026"
    SEED_AGENT_EMAIL: str = "agent1@enterprise.internal"
    SEED_AGENT_PASSWORD: str = "AgentDevPass!2026"

    # --- AI Providers & LLM Gateway ---
    GEMINI_API_KEY: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_GEMINI_API_KEY"),
        description="Google Gemini API key for primary LLM inference",
    )
    GEMINI_MODEL: str = "gemini-3.8-flash"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3:latest"
    PRIMARY_LLM_PROVIDER: str = Field(
        default="gemini",
        validation_alias=AliasChoices("PRIMARY_LLM_PROVIDER", "LLM_PRIMARY_PROVIDER"),
        description="Primary LLM provider ('gemini' or 'ollama')",
    )
    ENABLE_OLLAMA_FALLBACK: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_OLLAMA_FALLBACK", "LLM_ENABLE_FALLBACK"),
        description="Whether to enable fallback to Ollama when primary provider fails",
    )
    LLM_FALLBACK_PROVIDER: str = Field(
        default="ollama",
        validation_alias=AliasChoices("LLM_FALLBACK_PROVIDER", "FALLBACK_LLM_PROVIDER"),
        description="Fallback LLM provider name ('ollama' or 'none')",
    )
    ENABLE_CARETAKER_FALLBACK: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_CARETAKER_FALLBACK", "CARETAKER_FALLBACK_ENABLED"),
        description="Whether to generate empathetic AI customer care responses when specific KB records are not indexed",
    )
    LLM_TIMEOUT_SECONDS: float = 30.0
    LLM_MAX_RETRIES: int = Field(
        default=3,
        validation_alias=AliasChoices("LLM_MAX_RETRIES", "LLM_RETRY_LIMIT"),
        description="Maximum transient retry attempts for LLM inference",
    )

    # --- Vector Embeddings ---
    EMBEDDING_PROVIDER: str = Field(
        default="mock",
        validation_alias=AliasChoices("EMBEDDING_PROVIDER"),
        description="Active embedding provider: 'gemini', 'mock', or 'sentence_transformers'",
    )
    EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384
    EMBEDDING_BATCH_SIZE: int = 32

    # --- Redis & Celery Infrastructure ---
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    REDIS_SOCKET_TIMEOUT: float = 5.0
    REDIS_CONNECT_TIMEOUT: float = 5.0
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_URL: Optional[str] = None
    REDIS_PUBSUB_INDEX_RELOAD_CHANNEL: str = "channel:knowledge:index_reload"
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None
    CELERY_TASK_DEFAULT_QUEUE: str = "resolution_actions"

    # --- Storage & Knowledge Base Directories ---
    STORAGE_PROVIDER: str = Field(
        default="local",
        validation_alias=AliasChoices("STORAGE_PROVIDER"),
        description="Active storage provider: 'local' (filesystem) or 'postgres' (BYTEA)",
    )
    STORAGE_LOCAL_ROOT: str = "./storage"
    STORAGE_MAX_QUOTA_BYTES: int = 400 * 1024 * 1024  # 400 MB hard application-level quota for staging
    STORAGE_MAX_FILE_SIZE_BYTES: int = 15 * 1024 * 1024  # 15 MB max file upload size
    KNOWLEDGE_DOCS_DIR: str = "./storage/raw_documents"
    QUARANTINE_DIR: str = "./storage/quarantine"
    FAISS_STORAGE_DIR: str = "./storage/indices"
    MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit for knowledge docs
    ALLOWED_DOCUMENT_EXTENSIONS: List[str] = [".md", ".txt", ".pdf"]
    KB_SCAN_LOCK_TIMEOUT_SECONDS: float = 10.0
    KB_SCAN_ADVISORY_LOCK_ID: int = 5432001
    KB_PROMOTION_ADVISORY_LOCK_ID: int = 5432002
    KB_RETRY_WORKER_ENABLED: bool = True
    KB_RETRY_SCAN_INTERVAL_SECONDS: float = 30.0
    KB_RETRY_LEASE_DURATION_SECONDS: float = 300.0
    KB_RETRY_HEARTBEAT_INTERVAL_SECONDS: float = 30.0
    KB_RETRY_MAX_OPERATIONAL_INTERRUPTIONS: int = 3
    KB_RETRY_MAX_RETRIES: int = 3

    # --- Benchmark Quality Gate Settings ---
    BENCHMARK_GROUNDING_PASS_SCORE: float = 0.90
    BENCHMARK_CATEGORY_GROUNDING_PASS_SCORE: float = 0.80
    BENCHMARK_RETRIEVAL_HIT_RATE_PASS_SCORE: float = 0.88
    BENCHMARK_P95_LATENCY_MAX_MS: float = 50.0
    BENCHMARK_CRITICAL_REGRESSION_TOLERANCE: int = 0
    BENCHMARK_SUITE_PATH: str = "backend/app/modules/knowledge/benchmarks/golden_qa_v1.json"

    # --- Maintenance Window Settings ---
    MAINTENANCE_WINDOW_ENABLED: bool = True
    MAINTENANCE_WINDOW_CRON: str = "0 2 * * 0"  # Default: Sunday 02:00 AM
    MAINTENANCE_WINDOW_DURATION_MINUTES: int = 120
    MAINTENANCE_WINDOW_TIMEZONE: str = "UTC"
    MAINTENANCE_WINDOW_ALLOWED_WEEKDAYS: List[int] = [6]  # 0=Monday, 6=Sunday
    MAINTENANCE_WINDOW_START_TIME: str = "02:00:00"
    MAINTENANCE_WINDOW_END_TIME: str = "04:00:00"
    MAINTENANCE_WINDOW_BLACKOUT_DATES: List[str] = []

    # --- Watchdog Canary Settings ---
    WATCHDOG_MONITORING_WINDOW_SECONDS: int = 300  # 5 minutes
    WATCHDOG_CHECK_INTERVAL_SECONDS: int = 30  # 10 checks total
    WATCHDOG_QUERY_TIMEOUT_SECONDS: float = 2.0
    WATCHDOG_CONSECUTIVE_FAILURE_THRESHOLD: int = 2
    WATCHDOG_TOTAL_FAILURE_THRESHOLD: int = 3

    # --- SLA Policies ---
    DEFAULT_SLA_TARGET_MINUTES: int = 240
    SLA_WARNING_THRESHOLD_PERCENT: int = 75
    SLA_PENDING_CUSTOMER_PAUSE: bool = True
    BUSINESS_HOURS_TIMEZONE: str = "America/New_York"
    BUSINESS_HOUR_START: str = "09:00:00"
    BUSINESS_HOUR_END: str = "17:00:00"

    # --- Intelligent Escalation & After-Hours ---
    ESCALATION_UNRESOLVED_NEGATIVE_MINUTES: int = 15
    ESCALATION_REPEATED_NEGATIVE_THRESHOLD: int = 2
    ESCALATION_FRUSTRATION_INDEX_THRESHOLD: float = 0.70
    ESCALATION_URGENCY_INDEX_THRESHOLD: float = 0.80
    ON_CALL_WORKFLOW_ENABLED: bool = True
    ON_CALL_WEBHOOK_URL: Optional[str] = None
    ON_CALL_API_KEY: Optional[str] = None

    # --- Multimodal Evidence Intake & OCR (Day 09) ---
    EVIDENCE_STORAGE_DIR: str = "./storage/evidence"
    EVIDENCE_MAX_FILE_SIZE_BYTES: int = 15 * 1024 * 1024  # 15 MB
    EVIDENCE_MAX_PDF_PAGES: int = 10
    EVIDENCE_ALLOWED_MIME_TYPES: List[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "application/pdf"]
    )
    EVIDENCE_RETENTION_DAYS: int = 30
    EVIDENCE_RETENTION_SCAN_INTERVAL_SECONDS: float = 3600.0
    EVIDENCE_RETENTION_BATCH_SIZE: int = 50
    EVIDENCE_RETENTION_MAX_RETRIES: int = 3
    EVIDENCE_SYNC_TIMEOUT_SECONDS: float = 30.0
    EVIDENCE_DEFAULT_OCR_PROVIDER: str = "auto"
    ENABLE_BACKGROUND_WORKERS: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_BACKGROUND_WORKERS", "WORKERS_ENABLED"),
        description="Whether to run in-process background worker loops (set False when using dedicated worker processes)",
    )
    REDIS_REQUIRED: bool = Field(
        default=False,
        validation_alias=AliasChoices("REDIS_REQUIRED"),
        description="Whether Redis connectivity is strictly mandatory for API readiness (HTTP 503 if unreachable)",
    )

    # --- HTTP Request Rate Limiting ---
    RATE_LIMIT_ENABLED: bool = Field(
        default=True,
        validation_alias=AliasChoices("RATE_LIMIT_ENABLED"),
        description="Whether application-level HTTP request rate limiting is enabled",
    )
    RATE_LIMIT_CHAT_PER_MINUTE: int = Field(
        default=30,
        validation_alias=AliasChoices("RATE_LIMIT_CHAT_PER_MINUTE"),
        description="Max chat turn queries per minute per client identity",
    )
    RATE_LIMIT_EVIDENCE_PER_MINUTE: int = Field(
        default=10,
        validation_alias=AliasChoices("RATE_LIMIT_EVIDENCE_PER_MINUTE"),
        description="Max multimodal evidence uploads per minute per client identity",
    )
    TRUSTED_PROXIES: List[str] = Field(
        default_factory=lambda: ["127.0.0.1", "::1"],
        validation_alias=AliasChoices("TRUSTED_PROXIES"),
        description="List of trusted reverse proxy IP addresses authorized to forward client IP headers",
    )

    # --- RAG Knowledge Assistant Settings ---
    RAG_TOP_K: int = 5
    RAG_SIMILARITY_THRESHOLD_DEFAULT: float = 0.72
    RAG_SIMILARITY_THRESHOLD_POLICY: float = 0.76
    RAG_SIMILARITY_THRESHOLD_FAQ: float = 0.70

    # --- Financial Resolution Safeguards & Smart INR Goodwill Credit Policy ---
    DEFAULT_CURRENCY: str = "INR"
    SUPPORTED_CURRENCIES: List[str] = ["INR", "USD", "EUR", "GBP", "CAD", "AUD", "JPY"]
    DEFAULT_DISCREPANCY_REFUND_AMOUNT: str = "499.00"

    # Smart INR Goodwill Credit Policy Thresholds & Caps
    GOODWILL_CREDIT_STANDARD_DEFAULT: str = "499.00"
    GOODWILL_CREDIT_STANDARD_MAX: str = "999.00"
    GOODWILL_CREDIT_VIP_DEFAULT: str = "799.00"
    GOODWILL_CREDIT_VIP_MAX: str = "1499.00"
    GOODWILL_CREDIT_ENTERPRISE_DEFAULT: str = "999.00"
    GOODWILL_CREDIT_ENTERPRISE_MAX: str = "2499.00"
    GOODWILL_CREDIT_ORDER_VALUE_CAP_PERCENT: str = "10.00"  # Max 10% of order value

    # Approval Matrix Thresholds (INR)
    GOODWILL_AUTO_RECOMMEND_THRESHOLD: str = "499.00"      # <= 499 automated recommendation
    GOODWILL_SUPERVISOR_THRESHOLD: str = "999.00"          # 500 - 999 Supervisor approval
    GOODWILL_SENIOR_SUPERVISOR_THRESHOLD: str = "1499.00"  # 1000 - 1499 Senior Supervisor
    # Above 1499 requires manual review (ADMIN role required)

    MAX_AUTONOMOUS_REFUND_AMOUNT: str = "0.00"  # Strict zero: all refunds require supervisor approval

    # --- Chat Idempotency & HMAC Key Ring ---
    IDEMPOTENCY_HMAC_ACTIVE_VERSION: str = "v1"
    IDEMPOTENCY_HMAC_KEY_RING: Dict[str, str] = Field(
        default_factory=lambda: {
            "v1": "9a3f2b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a"
        }
    )
    IDEMPOTENCY_LEASE_DURATION_SECONDS: int = 30
    IDEMPOTENCY_HEARTBEAT_INTERVAL_SECONDS: int = 10
    IDEMPOTENCY_RECORD_TTL_SECONDS: int = 86400  # 24 hours

    def validate_idempotency_hmac_key_ring(self) -> None:
        active_ver = self.IDEMPOTENCY_HMAC_ACTIVE_VERSION
        key_ring = self.IDEMPOTENCY_HMAC_KEY_RING
        if not key_ring or active_ver not in key_ring:
            raise RuntimeError(
                f"IDEMPOTENCY_HMAC active version '{active_ver}' not found in key ring."
            )
        for ver, key in key_ring.items():
            if "change_me_to_a_secure_hmac_secret" in key.lower():
                raise RuntimeError(
                    f"Placeholder HMAC key detected for version '{ver}'. Insecure secret prohibited."
                )
            if len(key.encode("utf-8")) < 32:
                raise RuntimeError(
                    f"HMAC key for version '{ver}' must be at least 32 bytes (got {len(key.encode('utf-8'))} bytes)."
                )

    def validate_production_configuration(self) -> None:
        """Enforces strict security lockdowns and configuration validation in production.

        Fails fast if:
        1. DEBUG is True.
        2. ENABLE_DEV_AUTH_ENDPOINTS is True.
        3. Insecure/placeholder SECRET_KEY or key length < 32 characters.
        4. Insecure/placeholder ENCRYPTION_MASTER_KEY or invalid 64-hex-char representation.
        5. Insecure/placeholder or empty POSTGRES_PASSWORD.
        6. Wildcard '*' CORS_ORIGINS configured.
        """
        env = (self.ENVIRONMENT or "development").strip().lower()
        if env not in ("production", "staging"):
            return

        errors: List[str] = []

        if not getattr(self, "RATE_LIMIT_ENABLED", True):
            errors.append(
                "RATE_LIMIT_ENABLED must be set to True in production and staging environments."
            )

        if env != "production":
            if errors:
                formatted_errors = "\n  - " + "\n  - ".join(errors)
                raise ValueError(
                    f"Staging startup validation failed with {len(errors)} security error(s):{formatted_errors}"
                )
            return

        if self.DEBUG:
            errors.append("DEBUG mode must be set to False in production.")

        if self.ENABLE_DEV_AUTH_ENDPOINTS:
            errors.append("ENABLE_DEV_AUTH_ENDPOINTS must be set to False in production.")

        sec_key = self.SECRET_KEY or ""
        sec_lower = sec_key.lower()
        if (
            "change_this" in sec_lower
            or "placeholder" in sec_lower
            or "dev_secret" in sec_lower
            or len(sec_key) < 32
        ):
            errors.append(
                "Insecure or placeholder SECRET_KEY detected. A strong, random key of at least 32 characters is required."
            )

        enc_key = self.ENCRYPTION_MASTER_KEY or ""
        enc_lower = enc_key.lower()
        if (
            "0123456789abcdef" in enc_lower
            or "placeholder" in enc_lower
            or len(enc_key) != 64
        ):
            errors.append(
                "Insecure or invalid ENCRYPTION_MASTER_KEY detected. Must be a 64-character hex string (32 bytes)."
            )
        else:
            try:
                bytes.fromhex(enc_key)
            except ValueError:
                errors.append(
                    "ENCRYPTION_MASTER_KEY is not a valid hexadecimal string."
                )

        db_pwd = self.POSTGRES_PASSWORD or ""
        if not db_pwd or "placeholder" in db_pwd.lower():
            errors.append(
                "POSTGRES_PASSWORD must be configured with a non-empty, non-placeholder value in production."
            )

        if any(origin.strip() == "*" for origin in self.CORS_ORIGINS):
            errors.append(
                "Wildcard CORS_ORIGINS ('*') is prohibited in production when credentials are supported."
            )

        prov = (self.EMBEDDING_PROVIDER or "").strip().lower()
        if prov == "mock":
            errors.append(
                "EMBEDDING_PROVIDER 'mock' is prohibited in production. "
                "A live provider ('gemini') must be configured."
            )
        elif prov == "gemini":
            if not (self.GEMINI_API_KEY or "").strip():
                errors.append(
                    "GEMINI_API_KEY must be configured with a non-empty value in production "
                    "when EMBEDDING_PROVIDER is 'gemini'."
                )

        stor_prov = (self.STORAGE_PROVIDER or "").strip().lower()
        if stor_prov == "local":
            errors.append(
                "STORAGE_PROVIDER 'local' is prohibited in production. "
                "A persistent storage provider ('postgres') must be configured."
            )

        if errors:
            formatted_errors = "\n  - " + "\n  - ".join(errors)
            env_label = "Production" if env == "production" else "Staging"
            raise ValueError(
                f"{env_label} startup validation failed with {len(errors)} security error(s):{formatted_errors}"
            )

    def validate_embedding_configuration(self) -> None:
        """Validates embedding provider selection and credentials by environment.

        Enforces:
        1. Staging and production explicitly reject EMBEDDING_PROVIDER='mock'.
        2. Staging and production reject missing or empty GEMINI_API_KEY when EMBEDDING_PROVIDER='gemini'.
        3. Unsupported EMBEDDING_PROVIDER names fail validation across all environments.
        4. Development and testing environments allow 'mock' and 'gemini'.
        """
        env = (self.ENVIRONMENT or "development").strip().lower()
        provider = (self.EMBEDDING_PROVIDER or "").strip().lower()

        allowed_providers = ("gemini", "mock", "sentence_transformers")
        if provider not in allowed_providers:
            raise ValueError(
                f"Unsupported EMBEDDING_PROVIDER '{self.EMBEDDING_PROVIDER}'. "
                f"Must be one of: {', '.join(allowed_providers)}."
            )

        if env in ("staging", "production"):
            if provider == "mock":
                raise ValueError(
                    f"EMBEDDING_PROVIDER 'mock' is prohibited in {env} environment. "
                    "A production-grade embedding provider ('gemini') must be configured."
                )
            if provider == "gemini":
                key = (self.GEMINI_API_KEY or "").strip()
                if not key:
                    raise ValueError(
                        f"GEMINI_API_KEY is required in {env} environment when "
                        "EMBEDDING_PROVIDER is 'gemini'."
                    )

    def validate_storage_configuration(self) -> None:
        """Validates storage provider selection by environment.

        Enforces:
        1. Staging and production explicitly reject STORAGE_PROVIDER='local'.
        2. Development and testing allow 'local' or 'postgres'.
        3. Unsupported STORAGE_PROVIDER values fail validation across all environments.
        """
        env = (self.ENVIRONMENT or "development").strip().lower()
        provider = (self.STORAGE_PROVIDER or "").strip().lower()

        allowed_providers = ("local", "postgres")
        if provider not in allowed_providers:
            raise ValueError(
                f"Unsupported STORAGE_PROVIDER '{self.STORAGE_PROVIDER}'. "
                f"Must be one of: {', '.join(allowed_providers)}."
            )

        if env in ("staging", "production") and provider == "local":
            raise ValueError(
                f"STORAGE_PROVIDER 'local' is prohibited in {env} environment. "
                "A persistent storage provider ('postgres') must be configured."
            )

    def __init__(self, **data: Any) -> None:
        # Resolve default STORAGE_PROVIDER based on environment if not explicitly passed
        env = (data.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT") or "development").strip().lower()
        if "STORAGE_PROVIDER" not in data and "STORAGE_PROVIDER" not in os.environ:
            if env in ("staging", "production"):
                data["STORAGE_PROVIDER"] = "postgres"
            else:
                data["STORAGE_PROVIDER"] = "local"
        super().__init__(**data)
        self.validate_idempotency_hmac_key_ring()
        self.validate_embedding_configuration()
        self.validate_storage_configuration()
        self.validate_production_configuration()

    # --- Validators ---
    @field_validator("STORAGE_PROVIDER")
    @classmethod
    def validate_storage_provider(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("STORAGE_PROVIDER must be a non-empty string.")
        v_norm = v.strip().lower()
        allowed = ("local", "postgres")
        if v_norm not in allowed:
            raise ValueError(
                f"Unsupported STORAGE_PROVIDER '{v}'. Must be one of: {', '.join(allowed)}."
            )
        return v_norm

    @field_validator("EMBEDDING_PROVIDER")
    @classmethod
    def validate_embedding_provider(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("EMBEDDING_PROVIDER must be a non-empty string.")
        v_norm = v.strip().lower()
        allowed = ("gemini", "mock", "sentence_transformers")
        if v_norm not in allowed:
            raise ValueError(
                f"Unsupported EMBEDDING_PROVIDER '{v}'. Must be one of: {', '.join(allowed)}."
            )
        return v_norm
    @field_validator("PRIMARY_LLM_PROVIDER")
    @classmethod
    def validate_primary_llm_provider(cls, v: str) -> str:
        v_norm = v.strip().lower()
        if v_norm not in ("gemini", "ollama"):
            raise ValueError(
                f"PRIMARY_LLM_PROVIDER must be 'gemini' or 'ollama', got '{v}'"
            )
        return v_norm

    @field_validator("EMBEDDING_DIMENSION")
    @classmethod
    def validate_embedding_dimension(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        return v

    @field_validator("EMBEDDING_BATCH_SIZE")
    @classmethod
    def validate_embedding_batch_size(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be a positive integer.")
        return v

    @field_validator("LLM_TIMEOUT_SECONDS")
    @classmethod
    def validate_llm_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive.")
        return v

    @field_validator("LLM_MAX_RETRIES")
    @classmethod
    def validate_llm_retries(cls, v: int) -> int:
        if v < 0:
            raise ValueError("LLM_MAX_RETRIES cannot be negative.")
        return v

    @field_validator("TRUSTED_PROXIES", mode="before")
    @classmethod
    def parse_trusted_proxies(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            v_clean = v.strip()
            if not v_clean:
                return ["127.0.0.1", "::1"]
            if v_clean.startswith("[") and v_clean.endswith("]"):
                try:
                    import json
                    parsed = json.loads(v_clean)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [x.strip() for x in v_clean.split(",") if x.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        return ["127.0.0.1", "::1"]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """Returns the async database URL (asyncpg or override)."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{password}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Returns the synchronous database URL (psycopg2 or override)."""
        if self.SYNC_DATABASE_URL:
            return self.SYNC_DATABASE_URL
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+psycopg2://{user}:{password}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_async_database_url(self) -> str:
        """Returns the async database URL for Alembic migration admin."""
        if self.MIGRATION_DATABASE_URL:
            return self.MIGRATION_DATABASE_URL
        user = quote_plus(self.MIGRATION_POSTGRES_USER)
        password = quote_plus(self.MIGRATION_POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{password}@" 
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_sync_database_url(self) -> str:
        """Returns the synchronous database URL for Alembic migration admin."""
        if self.MIGRATION_SYNC_DATABASE_URL:
            return self.MIGRATION_SYNC_DATABASE_URL
        user = quote_plus(self.MIGRATION_POSTGRES_USER)
        password = quote_plus(self.MIGRATION_POSTGRES_PASSWORD)
        return (
            f"postgresql+psycopg2://{user}:{password}@" 
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        """Returns the Redis connection URL with credentials if configured."""
        if self.REDIS_URL:
            return self.REDIS_URL
        if self.REDIS_PASSWORD:
            password = quote_plus(self.REDIS_PASSWORD)
            return (
                f"redis://:{password}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
            )
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def LLM_RETRY_LIMIT(self) -> int:
        """Alias for LLM_MAX_RETRIES for backwards and cross-spec compatibility."""
        return self.LLM_MAX_RETRIES

    @property
    def LLM_PRIMARY_PROVIDER(self) -> str:
        """Alias for PRIMARY_LLM_PROVIDER for backwards and cross-spec compatibility."""
        return self.PRIMARY_LLM_PROVIDER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def encryption_key_bytes(self) -> bytes:
        """Decodes the 64-hex-char ENCRYPTION_MASTER_KEY into 32 raw bytes."""
        try:
            raw = bytes.fromhex(self.ENCRYPTION_MASTER_KEY)
            if len(raw) != 32:
                raise ValueError
            return raw
        except (ValueError, TypeError):
            # If not hex, check if raw UTF-8 string is 32 bytes
            raw = self.ENCRYPTION_MASTER_KEY.encode("utf-8")
            if len(raw) != 32:
                raise ValueError(
                    "ENCRYPTION_MASTER_KEY must be either a 64-character hexadecimal string "
                    "or a 32-byte UTF-8 string for AES-256-GCM."
                )
            return raw


settings = Settings()


def validate_production_environment(settings_obj: Optional[Settings] = None) -> List[str]:
    """Inspects settings and returns a list of security configuration errors without raising."""
    cfg = settings_obj or settings
    errors: List[str] = []
    if cfg.DEBUG:
        errors.append("DEBUG is True (must be False in production).")
    if cfg.ENABLE_DEV_AUTH_ENDPOINTS:
        errors.append("ENABLE_DEV_AUTH_ENDPOINTS is True (must be False in production).")
    sec_key = cfg.SECRET_KEY or ""
    sec_lower = sec_key.lower()
    if (
        "change_this" in sec_lower
        or "placeholder" in sec_lower
        or "dev_secret" in sec_lower
        or len(sec_key) < 32
    ):
        errors.append("SECRET_KEY is insecure, placeholder, or shorter than 32 characters.")
    enc_key = cfg.ENCRYPTION_MASTER_KEY or ""
    enc_lower = enc_key.lower()
    if (
        "0123456789abcdef" in enc_lower
        or "placeholder" in enc_lower
        or len(enc_key) != 64
    ):
        errors.append("ENCRYPTION_MASTER_KEY is insecure, placeholder, or not a 64-character hex string.")
    db_pwd = cfg.POSTGRES_PASSWORD or ""
    if not db_pwd or "placeholder" in db_pwd.lower():
        errors.append("POSTGRES_PASSWORD is empty or contains placeholder text.")
    if any(origin.strip() == "*" for origin in cfg.CORS_ORIGINS):
        errors.append("CORS_ORIGINS contains wildcard '*' (prohibited with credentials in production).")
    if getattr(cfg, "EMBEDDING_PROVIDER", "").lower() == "mock":
        errors.append("EMBEDDING_PROVIDER 'mock' is prohibited in production.")
    if getattr(cfg, "EMBEDDING_PROVIDER", "").lower() == "gemini" and not (cfg.GEMINI_API_KEY or "").strip():
        errors.append("GEMINI_API_KEY is required when EMBEDDING_PROVIDER is 'gemini'.")
    if getattr(cfg, "STORAGE_PROVIDER", "").lower() == "local":
        errors.append("STORAGE_PROVIDER 'local' is prohibited in production.")
    if not getattr(cfg, "RATE_LIMIT_ENABLED", True):
        errors.append("RATE_LIMIT_ENABLED is False (must be True in production and staging).")
    return errors

