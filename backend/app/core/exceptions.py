"""Standardized Platform Exceptions.

Provides a unified exception hierarchy with structured error codes, HTTP status codes,
and client-safe serialization to prevent exposing raw infrastructure errors, credentials,
or stack traces to API consumers.
"""

from typing import Any, Dict, Optional


class PlatformException(Exception):
    """Base exception for all domain and infrastructure errors in the platform."""

    status_code: int = 500
    error_code: str = "PLATFORM_ERROR"
    default_message: str = "An internal platform error occurred."

    def __init__(
        self,
        message: Optional[str] = None,
        detail: Optional[Any] = None,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
    ):
        self.message = message or self.default_message
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        super().__init__(self.message)

    def to_client_dict(self) -> Dict[str, Any]:
        """Serializes to a client-safe dictionary for API responses.

        Guarantees sensitive internal details, database credentials, or stack traces
        are not leaked to clients.
        """
        payload: Dict[str, Any] = {
            "error": {
                "code": self.error_code,
                "message": self.message,
                "status_code": self.status_code,
            }
        }
        if self.detail is not None:
            payload["error"]["detail"] = self.detail
        return payload

    def to_dict(self) -> Dict[str, Any]:
        """Convenience alias for to_client_dict()."""
        return self.to_client_dict()


# --- LLM Gateway Exceptions ---

class LLMError(PlatformException):
    """Base class for LLM gateway errors."""

    status_code: int = 502
    error_code: str = "LLM_ERROR"
    default_message: str = "An error occurred while interacting with the LLM provider."


class LLMProviderError(LLMError):
    """Raised when an LLM provider returns an unexpected error or fails generation."""

    status_code: int = 502
    error_code: str = "LLM_PROVIDER_ERROR"
    default_message: str = "The LLM provider returned an unrecoverable error."

    def __init__(
        self,
        message: Optional[str] = None,
        provider: Optional[str] = None,
        detail: Optional[Any] = None,
    ):
        self.provider = provider
        msg = (
            message
            or (f"LLM provider '{provider}' encountered a failure." if provider else self.default_message)
        )
        super().__init__(message=msg, detail=detail)


class LLMTimeoutError(LLMError):
    """Raised when an LLM request exceeds configured timeout limits."""

    status_code: int = 504
    error_code: str = "LLM_TIMEOUT"
    default_message: str = "The request to the LLM provider timed out."

    def __init__(
        self,
        message: Optional[str] = None,
        provider: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        msg = (
            message
            or (
                f"LLM provider '{provider}' timed out after {timeout_seconds}s."
                if provider and timeout_seconds
                else self.default_message
            )
        )
        super().__init__(
            message=msg,
            detail={"timeout_seconds": timeout_seconds} if timeout_seconds is not None else None,
        )


class LLMAuthenticationError(LLMError):
    """Raised when LLM provider authentication or API key validation fails."""

    status_code: int = 401
    error_code: str = "LLM_AUTH_ERROR"
    default_message: str = "Authentication failed for the LLM provider. Verify API keys."

    def __init__(
        self,
        message: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.provider = provider
        msg = (
            message
            or (
                f"Invalid or missing API credentials for LLM provider '{provider}'."
                if provider
                else self.default_message
            )
        )
        super().__init__(message=msg)


# --- Vector Embedding Exceptions ---

class EmbeddingError(PlatformException):
    """Base class for vector embedding errors."""

    status_code: int = 500
    error_code: str = "EMBEDDING_ERROR"
    default_message: str = "An error occurred in the embedding pipeline."


class EmbeddingDimensionMismatchError(EmbeddingError):
    """Raised when generated embedding vector dimension does not match index dimension."""

    status_code: int = 500
    error_code: str = "EMBEDDING_DIMENSION_MISMATCH"
    default_message: str = "Embedding dimension does not match configured vector dimension."

    def __init__(
        self,
        expected_dim: int,
        actual_dim: int,
        message: Optional[str] = None,
    ):
        self.expected_dim = expected_dim
        self.actual_dim = actual_dim
        msg = (
            message
            or f"Embedding dimension mismatch: expected {expected_dim}, got {actual_dim}."
        )
        super().__init__(
            message=msg,
            detail={"expected_dimension": expected_dim, "actual_dimension": actual_dim},
        )


class EmbeddingModelLoadError(EmbeddingError):
    """Raised when the embedding model cannot be loaded into memory."""

    status_code: int = 503
    error_code: str = "EMBEDDING_MODEL_LOAD_ERROR"
    default_message: str = "Failed to load the embedding model."

    def __init__(self, model_name: str, message: Optional[str] = None):
        self.model_name = model_name
        msg = message or f"Failed to initialize embedding model '{model_name}'."
        super().__init__(message=msg, detail={"model_name": model_name})


# --- Redis Infrastructure Exceptions ---

class RedisError(PlatformException):
    """Base class for Redis infrastructure errors."""

    status_code: int = 503
    error_code: str = "REDIS_ERROR"
    default_message: str = "Redis cache/broker encountered an error."


class RedisConnectionError(RedisError):
    """Raised when connection to Redis fails or cannot be established."""

    status_code: int = 503
    error_code: str = "REDIS_CONNECTION_ERROR"
    default_message: str = "Unable to connect to Redis service."

    def __init__(
        self,
        message: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        msg = (
            message
            or (f"Could not connect to Redis at {host}:{port}." if host and port else self.default_message)
        )
        super().__init__(message=msg)


class RedisTimeoutError(RedisError):
    """Raised when a Redis command or connection handshake times out."""

    status_code: int = 504
    error_code: str = "REDIS_TIMEOUT"
    default_message: str = "Redis operation timed out."


# --- Knowledge Base & Document Pipeline Exceptions ---

class KnowledgeError(PlatformException):
    """Base class for knowledge base pipeline errors."""

    status_code: int = 500
    error_code: str = "KNOWLEDGE_ERROR"
    default_message: str = "An error occurred in the knowledge base pipeline."


class DocumentCorruptError(KnowledgeError):
    """Raised when a document is corrupted, malformed, or unparseable."""

    status_code: int = 400
    error_code: str = "DOCUMENT_CORRUPT"
    default_message: str = "The document file is corrupted, malformed, or unparseable."


class DocumentOversizedError(KnowledgeError):
    """Raised when a document exceeds the configured maximum file size."""

    status_code: int = 413
    error_code: str = "DOCUMENT_OVERSIZED"
    default_message: str = "The document exceeds the maximum allowed file size."


class DocumentSecurityError(KnowledgeError):
    """Raised when suspicious instructions, adversarial injection, or malware is detected."""

    status_code: int = 422
    error_code: str = "DOCUMENT_SECURITY_ERROR"
    default_message: str = "The document failed security checks due to suspicious instructions or injection markers."


class DocumentQuarantinedError(KnowledgeError):
    """Raised when an operation is attempted on a quarantined document."""

    status_code: int = 422
    error_code: str = "DOCUMENT_QUARANTINED"
    default_message: str = "The document is quarantined and cannot be processed."


class KnowledgeConcurrencyError(KnowledgeError):
    """Raised when a concurrent knowledge base scan or modification is already in progress."""

    status_code: int = 409
    error_code: str = "KNOWLEDGE_CONCURRENCY_CONFLICT"
    default_message: str = "A knowledge base scan or index operation is currently in progress."


class KnowledgeLockTimeoutError(KnowledgeConcurrencyError):
    """Raised when acquiring the distributed knowledge base lock exceeds the timeout limit."""

    status_code: int = 409
    error_code: str = "KNOWLEDGE_LOCK_TIMEOUT"
    default_message: str = "Timed out waiting to acquire knowledge base scan lock."


class CandidateIndexBuildError(KnowledgeError):
    """Raised when candidate vector index construction, embedding, or serialization fails."""

    status_code: int = 500
    error_code: str = "CANDIDATE_INDEX_BUILD_ERROR"
    default_message: str = "Failed to construct candidate FAISS vector index snapshot."


class IndexIntegrityError(KnowledgeError):
    """Raised when an index file is corrupted, missing, or fails cryptographic/vector integrity checks."""

    status_code: int = 500
    error_code: str = "INDEX_INTEGRITY_ERROR"
    default_message: str = "Vector index failed cryptographic, structural, or dimension integrity validation."


class BenchmarkEvaluationError(KnowledgeError):
    """Base class for knowledge index benchmark evaluation errors."""

    status_code: int = 422
    error_code: str = "BENCHMARK_EVALUATION_ERROR"
    default_message: str = "Golden benchmark evaluation failed."


class BenchmarkGateFailedError(BenchmarkEvaluationError):
    """Raised when a candidate index fails one or more benchmark quality gates."""

    status_code: int = 422
    error_code: str = "BENCHMARK_GATE_FAILED"
    default_message: str = "Candidate index failed quality gate thresholds."


class BenchmarkInconclusiveError(BenchmarkEvaluationError):
    """Raised when benchmark evaluation is inconclusive due to judge failures or timeouts."""

    status_code: int = 422
    error_code: str = "BENCHMARK_INCONCLUSIVE"
    default_message: str = "Benchmark evaluation was inconclusive and cannot be used for promotion."


class MaintenanceWindowError(KnowledgeError):
    """Raised when index promotion is attempted outside the configured maintenance window."""

    status_code: int = 409
    error_code: str = "MAINTENANCE_WINDOW_VIOLATION"
    default_message: str = "Operation is only permitted during scheduled maintenance windows."


class IndexPromotionError(KnowledgeError):
    """Raised when atomic candidate index promotion fails."""

    status_code: int = 500
    error_code: str = "INDEX_PROMOTION_ERROR"
    default_message: str = "Failed to promote candidate vector index to active status."


class UnauthorizedPromotionError(KnowledgeError):
    """Raised when an unauthorized user attempts candidate index promotion or override."""

    status_code: int = 403
    error_code: str = "PROMOTION_UNAUTHORIZED"
    default_message: str = "User is not authorized to promote knowledge base index snapshots."


class IndexWatchdogRollbackError(KnowledgeError):
    """Raised when the automated canary watchdog detects index health degradation and triggers rollback."""

    status_code: int = 500
    error_code: str = "WATCHDOG_ROLLBACK_TRIGGERED"
    default_message: str = "Automated rollback triggered due to canary health check degradation."


class RollbackExecutionError(KnowledgeError):
    """Raised when an automated or manual rollback operation fails."""

    status_code: int = 500
    error_code: str = "ROLLBACK_EXECUTION_ERROR"
    default_message: str = "Failed to execute knowledge index rollback."


# --- Task 4: Advanced RAG Knowledge Assistant Exceptions ---

class RAGError(PlatformException):
    """Base class for RAG retrieval and knowledge assistant errors."""

    status_code: int = 500
    error_code: str = "RAG_ERROR"
    default_message: str = "An error occurred during RAG retrieval or processing."


class RAGAuthorizationError(RAGError):
    """Raised when a query or caller is denied access to knowledge resources."""

    status_code: int = 403
    error_code: str = "RAG_AUTHORIZATION_DENIED"
    default_message: str = "Access to requested knowledge resources is denied."


class RAGSecurityQuarantineError(RAGError):
    """Raised when knowledge content or chunks are blocked due to security quarantine."""

    status_code: int = 422
    error_code: str = "RAG_CONTENT_QUARANTINED"
    default_message: str = "Knowledge content has been quarantined due to security policy."


class RAGPolicyConflictError(RAGError):
    """Raised when contradictory policy evidence prevents definitive answers."""

    status_code: int = 409
    error_code: str = "RAG_POLICY_CONFLICT"
    default_message: str = "Contradictory policy evidence detected across applicable documents."


class RAGSanitizationError(RAGError):
    """Raised when an input query or content fails security sanitization."""

    status_code: int = 422
    error_code: str = "RAG_SANITIZATION_FAILED"
    default_message: str = "Input failed security sanitization."


# --- Task 6: Multilingual & Conversation Lifecycle Exceptions ---

class LifecycleError(PlatformException):
    """Base class for session and conversation lifecycle errors."""

    status_code: int = 400
    error_code: str = "LIFECYCLE_ERROR"
    default_message: str = "A conversation lifecycle error occurred."


class SessionNotFoundError(LifecycleError):
    status_code: int = 404
    error_code: str = "SESSION_NOT_FOUND"
    default_message: str = "Customer session not found."


class SessionExpiredError(LifecycleError):
    status_code: int = 401
    error_code: str = "SESSION_EXPIRED"
    default_message: str = "Customer session authentication token has expired."


class SessionArchivedError(LifecycleError):
    status_code: int = 401
    error_code: str = "SESSION_ARCHIVED"
    default_message: str = "Customer session is archived and cannot authenticate or be restored."


class SessionRestorationWindowExpiredError(LifecycleError):
    status_code: int = 401
    error_code: str = "RESTORATION_WINDOW_EXPIRED"
    default_message: str = "Customer session restoration window (24 hours) has expired."


class CrossCustomerRestorationError(LifecycleError):
    status_code: int = 403
    error_code: str = "CROSS_CUSTOMER_RESTORATION_FORBIDDEN"
    default_message: str = "Cross-customer session restoration is strictly forbidden."




