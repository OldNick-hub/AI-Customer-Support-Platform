"""Application Configuration and Environment Settings.

Provides centralized, type-safe settings management via Pydantic BaseSettings.
All configuration parameters are read from environment variables or .env files.
"""

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
    LLM_TIMEOUT_SECONDS: float = 30.0
    LLM_MAX_RETRIES: int = Field(
        default=3,
        validation_alias=AliasChoices("LLM_MAX_RETRIES", "LLM_RETRY_LIMIT"),
        description="Maximum transient retry attempts for LLM inference",
    )

    # --- Vector Embeddings ---
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

    # --- Storage & Knowledge Base Directories ---
    KNOWLEDGE_DOCS_DIR: str = "./storage/raw_documents"
    QUARANTINE_DIR: str = "./storage/quarantine"
    FAISS_STORAGE_DIR: str = "./storage/indices"
    MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit for knowledge docs
    ALLOWED_DOCUMENT_EXTENSIONS: List[str] = [".md", ".txt", ".pdf"]
    KB_SCAN_LOCK_TIMEOUT_SECONDS: float = 10.0
    KB_SCAN_ADVISORY_LOCK_ID: int = 5432001
    KB_PROMOTION_ADVISORY_LOCK_ID: int = 5432002

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

    # --- RAG Knowledge Assistant Settings ---
    RAG_TOP_K: int = 5
    RAG_SIMILARITY_THRESHOLD_DEFAULT: float = 0.72
    RAG_SIMILARITY_THRESHOLD_POLICY: float = 0.76
    RAG_SIMILARITY_THRESHOLD_FAQ: float = 0.70

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

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.validate_idempotency_hmac_key_ring()

    # --- Validators ---
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
