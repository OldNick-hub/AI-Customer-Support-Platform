"""Digital PDF Document Parser and Text Extractor.

Extracts page-by-page text, rotation orientation, image presence, and character counts
using pypdf. Detects scanned documents to trigger OCR fallbacks.
"""

import io
import logging
from typing import List

import pypdf

from app.core.logging import configure_safe_logger
from app.modules.evidence.providers.base import BaseOCRProvider, DocumentExtractionResult
from app.modules.evidence.schemas import PageExtractionMetadata

logger = configure_safe_logger(logging.getLogger(__name__))


class PDFTextExtractor(BaseOCRProvider):
    """Extracts embedded digital text and page structure from PDF documents."""

    @property
    def provider_name(self) -> str:
        return "pypdf_extractor"

    async def extract_document(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str = "document.pdf",
    ) -> DocumentExtractionResult:
        """Parses PDF bytes and extracts text and per-page metadata."""
        if mime_type != "application/pdf":
            raise ValueError(f"PDFTextExtractor only supports 'application/pdf', received '{mime_type}'.")

        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        page_count = len(reader.pages)
        pages_meta: List[PageExtractionMetadata] = []
        full_text_parts: List[str] = []
        total_chars = 0
        has_any_images = False

        for idx, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            char_count = len(page_text.strip())
            total_chars += char_count

            # Orientation
            rotation = page.rotation if hasattr(page, "rotation") else 0
            if rotation is None:
                rotation = 0

            # Images
            page_has_images = len(page.images) > 0 if hasattr(page, "images") else False
            if page_has_images:
                has_any_images = True

            pages_meta.append(
                PageExtractionMetadata(
                    page_number=idx,
                    char_count=char_count,
                    confidence=0.98 if char_count > 0 else 0.50,
                    orientation_degrees=int(rotation),
                    has_images=page_has_images,
                )
            )

            if page_text:
                full_text_parts.append(f"--- Page {idx} ---\n{page_text}")

        full_text = "\n\n".join(full_text_parts)

        # Scanned document detection: if pages contain images but virtually no digital text
        is_scanned = (total_chars < 30) and has_any_images

        return DocumentExtractionResult(
            raw_text=full_text,
            page_count=page_count,
            pages=pages_meta,
            is_scanned=is_scanned,
            provider_used=self.provider_name,
            raw_confidence=0.95 if not is_scanned else 0.50,
        )

