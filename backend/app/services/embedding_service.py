"""Vector Embedding Service.

Provides singleton-loaded, async-threadpool Sentence-Transformer embeddings,
dimension validation (384-d), batch processing, and standardized exception handling.
"""

import asyncio
import logging
import threading
from typing import List, Optional

from sentence_transformers import SentenceTransformer

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    EmbeddingDimensionMismatchError,
    EmbeddingModelLoadError,
)
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class EmbeddingService:
    """Enterprise embedding service with singleton model caching and async threadpool execution."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        model_name: Optional[str] = None,
        expected_dimension: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.model_name = model_name or self.settings.EMBEDDING_MODEL_NAME
        self.expected_dimension = expected_dimension or self.settings.EMBEDDING_DIMENSION
        self.batch_size = batch_size or self.settings.EMBEDDING_BATCH_SIZE

        self._model: Optional[SentenceTransformer] = None
        self._model_lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        """Returns True if the model is currently loaded in memory."""
        return self._model is not None

    def _get_model(self) -> SentenceTransformer:
        """Loads and returns the SentenceTransformer model (thread-safe singleton)."""
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    try:
                        logger.info(
                            "Loading SentenceTransformer embedding model '%s' (CPU-only)...",
                            self.model_name,
                        )
                        # Force CPU execution to prevent GPU memory allocation / CUDA attempts
                        self._model = SentenceTransformer(self.model_name, device="cpu")
                        logger.info(
                            "Embedding model '%s' successfully loaded into memory.",
                            self.model_name,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to load embedding model '%s': %s",
                            self.model_name,
                            str(exc),
                        )
                        raise EmbeddingModelLoadError(
                            model_name=self.model_name,
                            message=f"Failed to load embedding model '{self.model_name}': {exc}",
                        ) from exc
        return self._model

    def _encode_sync(self, texts: List[str]) -> List[List[float]]:
        """Synchronous inference helper executed inside asyncio.to_thread."""
        model = self._get_model()

        try:
            embeddings = model.encode(
                texts,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,  # Normalized for Cosine/FAISS IndexFlatIP
            )
        except Exception as exc:
            logger.error("Error during model inference: %s", str(exc))
            raise

        results: List[List[float]] = []
        for idx, vec in enumerate(embeddings):
            vec_list = vec.tolist()
            actual_dim = len(vec_list)
            if actual_dim != self.expected_dimension:
                raise EmbeddingDimensionMismatchError(
                    expected_dim=self.expected_dimension,
                    actual_dim=actual_dim,
                    message=(
                        f"Embedding dimension mismatch at index {idx}: "
                        f"expected {self.expected_dimension}, got {actual_dim}."
                    ),
                )
            results.append(vec_list)

        return results

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generates normalized vector embeddings for a list of input texts.

        Runs blocking model inference in a background threadpool to prevent blocking the event loop.
        """
        if texts is None or not isinstance(texts, list):
            raise ValueError("Input texts must be a list of non-empty strings.")

        if len(texts) == 0:
            return []

        # Validate inputs
        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Batch text at index {idx} cannot be empty, None, or whitespace-only."
                )

        # Offload CPU-heavy inference to threadpool
        return await asyncio.to_thread(self._encode_sync, texts)

    async def embed_text(self, text: str) -> List[float]:
        """Generates a single normalized vector embedding for an input string."""
        if text is None or not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding text input cannot be empty, None, or whitespace-only.")

        results = await self.embed_batch([text])
        return results[0]


_default_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Returns a singleton instance of the EmbeddingService."""
    global _default_embedding_service
    if _default_embedding_service is None:
        _default_embedding_service = EmbeddingService()
    return _default_embedding_service

