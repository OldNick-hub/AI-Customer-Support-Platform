"""Evidence OCR Providers Package."""

from app.modules.evidence.providers.base import BaseOCRProvider, DocumentExtractionResult
from app.modules.evidence.providers.gemini_provider import GeminiVisionOCRProvider
from app.modules.evidence.providers.mock_provider import DeterministicMockOCRProvider
from app.modules.evidence.providers.pdf_extractor import PDFTextExtractor
from app.modules.evidence.providers.receipt_parser import RuleBasedReceiptParser

__all__ = [
    "BaseOCRProvider",
    "DocumentExtractionResult",
    "RuleBasedReceiptParser",
    "PDFTextExtractor",
    "DeterministicMockOCRProvider",
    "GeminiVisionOCRProvider",
]

