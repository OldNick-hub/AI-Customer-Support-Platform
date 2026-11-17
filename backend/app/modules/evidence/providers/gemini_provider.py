"""Google Gemini Multimodal Vision OCR Provider.

Provides multimodal image and document analysis using Google Gemini models
when an API key is configured in settings.
"""

import base64
import logging
from typing import Optional

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.core.pii import PIIMaskingEngine
from app.modules.evidence.providers.base import BaseOCRProvider, DocumentExtractionResult
from app.modules.evidence.schemas import PageExtractionMetadata

logger = configure_safe_logger(logging.getLogger(__name__))


class GeminiVisionOCRProvider(BaseOCRProvider):
    """Multimodal document understanding powered by Google Gemini Vision."""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model_name = model_name or getattr(settings, "GEMINI_MODEL", "gemini-3.8-flash")

    @property
    def provider_name(self) -> str:
        return "gemini_vision"

    @property
    def is_available(self) -> bool:
        """True if a valid, non-placeholder Gemini API key is configured."""
        if not self.api_key or not isinstance(self.api_key, str):
            return False
        cleaned = self.api_key.strip()
        return bool(cleaned and not cleaned.lower().startswith("your_"))

    async def extract_document(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str = "document",
    ) -> DocumentExtractionResult:
        """Executes vision OCR against Gemini API or raises if unconfigured."""
        if not self.is_available:
            raise RuntimeError(
                "GeminiVisionOCRProvider requires GEMINI_API_KEY to be set in environment or configuration."
            )

        logger.info("Calling Gemini Vision OCR for file '%s' (%s, %d bytes)", file_name, mime_type, len(file_bytes))

        try:
            # We use httpx to issue direct REST call to Gemini Vision generateContent
            import httpx

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
            b64_data = base64.b64encode(file_bytes).decode("utf-8")

            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": (
                                    "You are an authoritative document OCR and financial extraction engine. "
                                    "Extract all visible text from this document verbatim. "
                                    "Preserve line breaks, labels, and numbers accurately."
                                )
                            },
                            {
                                "inline_data": {
                                    "mime_type": mime_type,
                                    "data": b64_data,
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.0,
                    "maxOutputTokens": 4096,
                },
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()

            extracted_text = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                extracted_text = "".join(p.get("text", "") for p in parts)

            return DocumentExtractionResult(
                raw_text=extracted_text,
                page_count=1,
                pages=[
                    PageExtractionMetadata(
                        page_number=1,
                        char_count=len(extracted_text),
                        confidence=0.95,
                    )
                ],
                is_scanned=False,
                provider_used=self.provider_name,
                raw_confidence=0.95,
            )

        except Exception as exc:
            safe_err = PIIMaskingEngine.mask_text(str(exc))
            logger.error("Gemini Vision OCR extraction failed: %s", safe_err)
            raise RuntimeError(f"Gemini Vision extraction failed: {safe_err}") from exc

