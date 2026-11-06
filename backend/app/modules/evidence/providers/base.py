"""Base OCR and Document Extraction Provider Interface.

Defines the pluggable extraction contract enabling zero-rewrite engine swaps
(e.g., local heuristic, PDF parser, Google Gemini Vision, or Cloud Vision).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from app.modules.evidence.schemas import PageExtractionMetadata


@dataclass
class DocumentExtractionResult:
    """Standardized document extraction payload returned by all providers."""

    raw_text: str
    page_count: int = 1
    pages: List[PageExtractionMetadata] = field(default_factory=list)
    is_scanned: bool = False
    provider_used: str = "base"
    raw_confidence: float = 1.0


class BaseOCRProvider(ABC):
    """Abstract contract for document parsing and optical character recognition."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identifier name of the OCR engine."""
        pass

    @abstractmethod
    async def extract_document(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str = "document",
    ) -> DocumentExtractionResult:
        """Extracts text, page breakdown, and visual structure from document bytes.

        Args:
            file_bytes: Raw plaintext document or image bytes.
            mime_type: Validated MIME type (image/jpeg, image/png, application/pdf).
            file_name: Client or internal filename.

        Returns:
            DocumentExtractionResult with raw text and page metadata.
        """
        pass

