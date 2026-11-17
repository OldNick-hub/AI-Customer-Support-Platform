"""Vector Embedding Service & Provider Abstraction.

Provides:
1. BaseEmbeddingProvider: Clean abstract provider interface.
2. GeminiEmbeddingProvider: Google Gemini API provider targeting gemini-embedding-2 (768-d).
3. MockEmbeddingProvider: Zero-network, deterministic unit-vector provider for offline tests.
4. SentenceTransformerEmbeddingProvider: Lazy-loaded local inference provider.
5. EmbeddingService: Central facade with dimension validation, batching, and error handling.
"""

from abc import ABC, abstractmethod
import asyncio
import hashlib
import logging
import math
import os
import random
import threading
from typing import Any, Dict, List, Optional, Union

import httpx

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    EmbeddingAPIError,
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatchError,
    EmbeddingModelLoadError,
)
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


# =============================================================================
# Abstract Base Embedding Provider
# =============================================================================

class BaseEmbeddingProvider(ABC):
    """Abstract contract for vector embedding providers."""

    @property
    @abstractmethod
    def expected_dimension(self) -> int:
        """Returns the output vector dimensionality of the provider."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Returns the active model name or identifier."""
        ...

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """Returns True if the provider is initialized and ready for inference."""
        ...

    @abstractmethod
    def get_model(self) -> Any:
        """Returns the underlying model, client, or provider reference."""
        ...

    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generates unit-normalized vector embeddings for a batch of strings."""
        ...

    @abstractmethod
    async def embed_text(self, text: str) -> List[float]:
        """Generates a single unit-normalized vector embedding for a string."""
        ...


# =============================================================================
# Mock Embedding Provider (Offline Development & Testing)
# =============================================================================

class MockEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic, zero-dependency offline mock embedding provider for tests.

    Produces stable, unit-normalized vectors derived from input text SHA-256 digests.
    Requires zero network access, zero external credentials, and zero PyTorch dependencies.
    """

    def __init__(
        self,
        dimension: int = 384,
        model_name: str = "mock-embedding-2",
    ) -> None:
        self._dimension = dimension
        self._model_name = model_name

    @property
    def expected_dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_loaded(self) -> bool:
        return True

    def get_model(self) -> Any:
        return self

    def _generate_vector(self, text: str) -> List[float]:
        """Generates a deterministic, unit-normalized float vector based on text hash."""
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:8], byteorder="big", signed=False)
        rng = random.Random(seed)

        # Generate Gaussian random variables for uniform spherical distribution
        raw = [rng.gauss(0.0, 1.0) for _ in range(self._dimension)]
        norm = math.sqrt(sum(x * x for x in raw))
        if norm == 0.0:
            raw[0] = 1.0
            norm = 1.0
        return [float(x / norm) for x in raw]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if texts is None or not isinstance(texts, list):
            raise ValueError("Input texts must be a list of non-empty strings.")
        if len(texts) == 0:
            return []

        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Batch text at index {idx} cannot be empty, None, or whitespace-only."
                )

        return [self._generate_vector(t) for t in texts]

    async def embed_text(self, text: str) -> List[float]:
        if text is None or not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding text input cannot be empty, None, or whitespace-only.")
        return self._generate_vector(text)


# =============================================================================
# Google Gemini Embedding Provider (gemini-embedding-2)
# =============================================================================

class GeminiEmbeddingProvider(BaseEmbeddingProvider):
    """Google Gemini Embedding API provider utilizing gemini-embedding-2.

    Supports:
    - Native asynchronous execution via google-genai SDK client.aio.
    - Dynamic Matryoshka Representation Learning (MRL) output dimensionality (e.g. 768).
    - Batching of chunk texts up to batch_size per API call.
    - Bounded retries with exponential backoff and randomized jitter on rate limits (429/503).
    - Strict credential validation and sanitized error logging.
    """

    DEFAULT_MODEL: str = "gemini-embedding-2"
    DEFAULT_DIMENSION: int = 768

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        dimension: Optional[int] = None,
        batch_size: int = 32,
        max_retries: int = 4,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        task_type: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self._model_name = model_name or self.DEFAULT_MODEL
        self._dimension = dimension or self.DEFAULT_DIMENSION
        self._batch_size = max(1, batch_size)
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._task_type = task_type
        self._api_key = api_key

        if client is not None:
            self._client = client
        else:
            if not self._api_key:
                raise EmbeddingConfigurationError(
                    "GEMINI_API_KEY is required for GeminiEmbeddingProvider. "
                    "Set GEMINI_API_KEY in environment or pass api_key explicitly."
                )
            try:
                from google import genai
                self._client = genai.Client(api_key=self._api_key)
            except Exception as exc:
                logger.error("Failed to initialize google-genai Client: %s", str(exc))
                raise EmbeddingConfigurationError(
                    f"Failed to initialize Gemini Client: {exc}"
                ) from exc

    @property
    def expected_dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_loaded(self) -> bool:
        return self._client is not None

    def get_model(self) -> Any:
        return self._client

    async def _embed_batch_with_retry(
        self,
        batch_texts: List[str],
        config: Any,
    ) -> List[List[float]]:
        """Invokes Gemini embed_content with exponential backoff and jitter for transient errors."""
        try:
            from google.genai.errors import APIError
        except ImportError:
            APIError = Exception  # type: ignore[assignment,misc]

        attempt = 0
        while True:
            try:
                # Call native async embed_content on client.aio.models
                response = await self._client.aio.models.embed_content(
                    model=self._model_name,
                    contents=batch_texts,
                    config=config,
                )

                if not response or not hasattr(response, "embeddings") or not response.embeddings:
                    raise EmbeddingAPIError("Gemini embedding API returned empty response.")

                embeddings_list = response.embeddings
                if len(embeddings_list) != len(batch_texts):
                    raise EmbeddingAPIError(
                        f"Gemini API returned {len(embeddings_list)} embeddings for {len(batch_texts)} texts."
                    )

                results: List[List[float]] = []
                for idx, emb in enumerate(embeddings_list):
                    vec = emb.values
                    if len(vec) != self._dimension:
                        raise EmbeddingDimensionMismatchError(
                            expected_dim=self._dimension,
                            actual_dim=len(vec),
                            message=(
                                f"Gemini embedding dimension mismatch at index {idx}: "
                                f"expected {self._dimension}, got {len(vec)}."
                            ),
                        )
                    # Unit normalization check (gemini-embedding-2 auto-normalizes for specified dimensions)
                    norm = math.sqrt(sum(x * x for x in vec))
                    if abs(norm - 1.0) > 1e-3 and norm > 0.0:
                        vec = [x / norm for x in vec]
                    results.append(vec)

                return results

            except EmbeddingDimensionMismatchError:
                raise
            except Exception as exc:
                is_transient = False
                status_code = getattr(exc, "status", None) or getattr(exc, "status_code", None)

                # Identify transient error codes (429 Rate Limit / Quota, 5xx Server Errors)
                if isinstance(exc, APIError) or status_code is not None:
                    if status_code in (429, 500, 502, 503, 504):
                        is_transient = True
                elif isinstance(
                    exc,
                    (httpx.TimeoutException, httpx.NetworkError, ConnectionError, TimeoutError),
                ):
                    is_transient = True

                attempt += 1
                if is_transient and attempt <= self._max_retries:
                    delay = min(
                        self._max_delay,
                        self._base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5),
                    )
                    logger.warning(
                        "Transient error from Gemini embedding API (status=%s, attempt=%d/%d). "
                        "Retrying in %.2fs: %s",
                        status_code,
                        attempt,
                        self._max_retries,
                        delay,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Gemini embedding API failed after %d attempts (status=%s): %s",
                        attempt,
                        status_code,
                        type(exc).__name__,
                    )
                    raise EmbeddingAPIError(
                        message=f"Gemini embedding API error: {exc}",
                        detail={"status_code": status_code, "model": self._model_name},
                    ) from exc

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if texts is None or not isinstance(texts, list):
            raise ValueError("Input texts must be a list of non-empty strings.")
        if len(texts) == 0:
            return []

        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Batch text at index {idx} cannot be empty, None, or whitespace-only."
                )

        try:
            from google.genai.types import EmbedContentConfig
        except ImportError as exc:
            raise EmbeddingConfigurationError(f"google-genai types not available: {exc}") from exc

        config = EmbedContentConfig(
            output_dimensionality=self._dimension,
            task_type=self._task_type,
        )

        all_embeddings: List[List[float]] = []
        for i in range(0, len(texts), self._batch_size):
            chunk = texts[i : i + self._batch_size]
            batch_result = await self._embed_batch_with_retry(chunk, config)
            all_embeddings.extend(batch_result)

        return all_embeddings

    async def embed_text(self, text: str) -> List[float]:
        if text is None or not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding text input cannot be empty, None, or whitespace-only.")
        results = await self.embed_batch([text])
        return results[0]


# =============================================================================
# SentenceTransformer Provider (Lazy-Loaded Local Inference)
# =============================================================================

class SentenceTransformerEmbeddingProvider(BaseEmbeddingProvider):
    """Local inference provider utilizing SentenceTransformers.

    Lazy-loads PyTorch and SentenceTransformer on first inference to prevent
    unnecessary memory allocation during module import.
    """

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        expected_dimension: int = 384,
        batch_size: int = 32,
    ) -> None:
        self._model_name = model_name
        self._expected_dimension = expected_dimension
        self._batch_size = batch_size
        self._model: Optional[Any] = None
        self._model_lock = threading.Lock()

    @property
    def expected_dimension(self) -> int:
        return self._expected_dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def get_model(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    try:
                        logger.info(
                            "Lazy-loading SentenceTransformer model '%s' (CPU-only)...",
                            self._model_name,
                        )
                        from sentence_transformers import SentenceTransformer
                        self._model = SentenceTransformer(self._model_name, device="cpu")
                        logger.info("SentenceTransformer model '%s' successfully loaded.", self._model_name)
                    except Exception as exc:
                        logger.error("Failed to load SentenceTransformer model '%s': %s", self._model_name, str(exc))
                        raise EmbeddingModelLoadError(
                            model_name=self._model_name,
                            message=f"Failed to load embedding model '{self._model_name}': {exc}",
                        ) from exc
        return self._model

    def _encode_sync(self, texts: List[str]) -> List[List[float]]:
        model = self.get_model()
        try:
            embeddings = model.encode(
                texts,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except Exception as exc:
            logger.error("Error during model inference: %s", str(exc))
            raise

        results: List[List[float]] = []
        for idx, vec in enumerate(embeddings):
            vec_list = vec.tolist()
            actual_dim = len(vec_list)
            if actual_dim != self._expected_dimension:
                raise EmbeddingDimensionMismatchError(
                    expected_dim=self._expected_dimension,
                    actual_dim=actual_dim,
                    message=(
                        f"Embedding dimension mismatch at index {idx}: "
                        f"expected {self._expected_dimension}, got {actual_dim}."
                    ),
                )
            results.append(vec_list)
        return results

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if texts is None or not isinstance(texts, list):
            raise ValueError("Input texts must be a list of non-empty strings.")
        if len(texts) == 0:
            return []

        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Batch text at index {idx} cannot be empty, None, or whitespace-only."
                )

        return await asyncio.to_thread(self._encode_sync, texts)

    async def embed_text(self, text: str) -> List[float]:
        if text is None or not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding text input cannot be empty, None, or whitespace-only.")
        results = await self.embed_batch([text])
        return results[0]


# =============================================================================
# Enterprise Embedding Service (Facade)
# =============================================================================

class EmbeddingService:
    """Enterprise embedding service with pluggable provider architecture and dimension validation."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        provider: Optional[BaseEmbeddingProvider] = None,
        model_name: Optional[str] = None,
        expected_dimension: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.model_name = model_name or self.settings.EMBEDDING_MODEL_NAME
        self.expected_dimension = expected_dimension or self.settings.EMBEDDING_DIMENSION
        self.batch_size = batch_size or self.settings.EMBEDDING_BATCH_SIZE

        if provider is not None:
            self._provider = provider
        else:
            self._provider = self._resolve_provider()

    def _resolve_provider(self) -> BaseEmbeddingProvider:
        """Resolves embedding provider based on configuration."""
        env = (getattr(self.settings, "ENVIRONMENT", "development") or "development").strip().lower()
        provider_type = (getattr(self.settings, "EMBEDDING_PROVIDER", "mock") or "mock").strip().lower()

        if env in ("staging", "production") and provider_type == "mock":
            raise EmbeddingConfigurationError(
                f"EMBEDDING_PROVIDER 'mock' is prohibited in {env} environment. "
                "A live provider ('gemini') must be configured."
            )

        if provider_type == "gemini":
            return GeminiEmbeddingProvider(
                api_key=self.settings.GEMINI_API_KEY,
                model_name=self.model_name,
                dimension=self.expected_dimension,
                batch_size=self.batch_size,
            )
        elif provider_type == "sentence_transformers":
            return SentenceTransformerEmbeddingProvider(
                model_name=self.model_name,
                expected_dimension=self.settings.EMBEDDING_DIMENSION,
                batch_size=self.batch_size,
            )
        elif provider_type == "mock":
            return MockEmbeddingProvider(
                dimension=self.settings.EMBEDDING_DIMENSION,
                model_name=self.model_name,
            )
        else:
            raise EmbeddingConfigurationError(
                f"Unknown EMBEDDING_PROVIDER '{provider_type}'. Must be 'gemini', 'mock', or 'sentence_transformers'."
            )

    @property
    def provider(self) -> BaseEmbeddingProvider:
        """Returns the active embedding provider instance."""
        return self._provider

    @property
    def is_loaded(self) -> bool:
        """Returns True if the underlying provider is loaded and ready."""
        return self._provider.is_loaded

    def _get_model(self) -> Any:
        """Backwards compatibility helper: returns underlying provider/model reference."""
        return self._provider.get_model()

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generates normalized vector embeddings for a list of input texts.

        Validates inputs, delegates to active provider, and enforces dimension invariants.
        """
        if texts is None or not isinstance(texts, list):
            raise ValueError("Input texts must be a list of non-empty strings.")
        if len(texts) == 0:
            return []

        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Batch text at index {idx} cannot be empty, None, or whitespace-only."
                )

        embeddings = await self._provider.embed_batch(texts)

        # Enforce dimension invariants on output
        for idx, vec in enumerate(embeddings):
            actual_dim = len(vec)
            if actual_dim != self.expected_dimension:
                raise EmbeddingDimensionMismatchError(
                    expected_dim=self.expected_dimension,
                    actual_dim=actual_dim,
                    message=(
                        f"Embedding dimension mismatch at index {idx}: "
                        f"expected {self.expected_dimension}, got {actual_dim}."
                    ),
                )

        return embeddings

    async def embed_text(self, text: str) -> List[float]:
        """Generates a single normalized vector embedding for an input string."""
        if text is None or not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding text input cannot be empty, None, or whitespace-only.")
        results = await self.embed_batch([text])
        return results[0]

    async def embed(self, text: str) -> List[float]:
        """Convenience alias for embed_text, preserving caller backwards compatibility."""
        return await self.embed_text(text)


# =============================================================================
# Singleton Accessor & Registry
# =============================================================================

_default_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Returns a singleton instance of the EmbeddingService."""
    global _default_embedding_service
    if _default_embedding_service is None:
        _default_embedding_service = EmbeddingService()
    return _default_embedding_service


def reset_embedding_service() -> None:
    """Resets the singleton instance for isolated test fixtures."""
    global _default_embedding_service
    _default_embedding_service = None
