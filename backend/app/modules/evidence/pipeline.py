"""Multimodal Evidence Processing and Defense Pipeline.

Orchestrates:
1. Multi-engine OCR and PDF text extraction.
2. Visual and textual prompt-injection defense (adversarial instruction containment).
3. PII and PCI-DSS payment data sanitization before logging or indexing.
4. Structured entity parsing and field-level confidence scoring.
"""

from dataclasses import dataclass
import logging
import re
from typing import List, Optional, Tuple

from app.core.logging import configure_safe_logger
from app.core.pii import PIIMaskingEngine
from app.modules.evidence.providers.base import BaseOCRProvider, DocumentExtractionResult
from app.modules.evidence.providers.mock_provider import DeterministicMockOCRProvider
from app.modules.evidence.providers.pdf_extractor import PDFTextExtractor
from app.modules.evidence.providers.receipt_parser import RuleBasedReceiptParser
from app.modules.evidence.schemas import StructuredInvoiceData

logger = configure_safe_logger(logging.getLogger(__name__))


# Adversarial Prompt Injection Patterns for Visual & Document Contexts
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\b(?:system\s*override|admin\s*override)\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:rules|policies|guidelines)\b", re.IGNORECASE),
    re.compile(r"\b(?:developer\s*mode|god\s*mode|dan\s*mode)\b", re.IGNORECASE),
    re.compile(r"\b(?:waive\s+all\s+return\s+policies|bypass\s+verification)\b", re.IGNORECASE),
    re.compile(r"\b(?:approve\s+(?:claim|refund)\s+automatically|give\s+full\s+refund\s+unconditionally)\b", re.IGNORECASE),
    re.compile(r"\b(?:you\s+are\s+now\s+an\s+unrestricted\s+ai|jailbreak)\b", re.IGNORECASE),
]


@dataclass
class PipelineProcessingResult:
    """Result returned by the evidence processing pipeline."""

    status: str  # 'VERIFIED', 'FLAGGED_UNSAFE', 'FAILED'
    is_malicious: bool
    quarantine_reason: Optional[str]
    processing_error: Optional[str]
    raw_ocr_text: str
    pii_redacted_text: str
    structured_entities: StructuredInvoiceData
    ocr_confidence: float


class EvidenceProcessingPipeline:
    """High-assurance document analysis pipeline with multi-layer security protections."""

    def __init__(
        self,
        ocr_provider: Optional[BaseOCRProvider] = None,
        pdf_extractor: Optional[PDFTextExtractor] = None,
        receipt_parser: Optional[RuleBasedReceiptParser] = None,
    ):
        self.ocr_provider = ocr_provider or DeterministicMockOCRProvider()
        self.pdf_extractor = pdf_extractor or PDFTextExtractor()
        self.receipt_parser = receipt_parser or RuleBasedReceiptParser()

    async def process_document(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str = "document",
    ) -> PipelineProcessingResult:
        """Executes full extraction, security scanning, and structured parsing."""
        try:
            # 1. Extraction: PDF vs Image
            extraction_result: DocumentExtractionResult
            if mime_type == "application/pdf":
                pdf_result = await self.pdf_extractor.extract_document(
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    file_name=file_name,
                )
                if pdf_result.is_scanned:
                    # Fallback to OCR provider for scanned PDFs
                    logger.info("PDF '%s' is scanned without digital text; invoking OCR fallback.", file_name)
                    ocr_res = await self.ocr_provider.extract_document(file_bytes, mime_type, file_name)
                    extraction_result = DocumentExtractionResult(
                        raw_text=ocr_res.raw_text,
                        page_count=pdf_result.page_count,
                        pages=pdf_result.pages,
                        is_scanned=True,
                        provider_used=f"{pdf_result.provider_used}+{ocr_res.provider_used}",
                        raw_confidence=ocr_res.raw_confidence,
                    )
                else:
                    extraction_result = pdf_result
            else:
                # Images: JPEG / PNG
                extraction_result = await self.ocr_provider.extract_document(
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    file_name=file_name,
                )

            raw_text = extraction_result.raw_text

            # 2. Security Defense: Adversarial Prompt Injection Scanning
            is_malicious, quarantine_reason = self._scan_prompt_injection(raw_text)
            if is_malicious:
                logger.warning(
                    "Prompt injection or adversarial directive detected in evidence file '%s': %s",
                    file_name,
                    quarantine_reason,
                )

            # 3. Privacy & Compliance: PII and Payment Card Data Masking
            pii_redacted_text = PIIMaskingEngine.mask(raw_text)

            # 4. Structured Parsing: Financial Fields & Line Items
            structured_data = self.receipt_parser.parse(
                text=pii_redacted_text,
                page_count=extraction_result.page_count,
                pages=extraction_result.pages,
                is_scanned=extraction_result.is_scanned,
                provider_used=extraction_result.provider_used,
            )
            structured_data.pii_redacted_text = pii_redacted_text

            # 5. Status Determination
            if is_malicious:
                final_status = "FLAGGED_UNSAFE"
            else:
                final_status = "VERIFIED"

            return PipelineProcessingResult(
                status=final_status,
                is_malicious=is_malicious,
                quarantine_reason=quarantine_reason,
                processing_error=None,
                raw_ocr_text=raw_text,
                pii_redacted_text=pii_redacted_text,
                structured_entities=structured_data,
                ocr_confidence=extraction_result.raw_confidence,
            )

        except Exception as exc:
            logger.error("Evidence extraction pipeline failure for file '%s': %s", file_name, exc, exc_info=True)
            empty_structured = StructuredInvoiceData(
                missing_fields=["order_id", "total_amount"],
                warnings=[f"Processing error: {str(exc)}"],
            )
            return PipelineProcessingResult(
                status="FAILED",
                is_malicious=False,
                quarantine_reason=None,
                processing_error=f"Extraction failure: {str(exc)}",
                raw_ocr_text="",
                pii_redacted_text="",
                structured_entities=empty_structured,
                ocr_confidence=0.0,
            )

    def _scan_prompt_injection(self, text: str) -> Tuple[bool, Optional[str]]:
        """Scans extracted text for adversarial directives and prompt overrides."""
        if not text:
            return False, None

        for pattern in PROMPT_INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                matched_token = match.group(0)
                return True, f"ADVERSARIAL_DIRECTIVE_DETECTED: Suspicious prompt directive '{matched_token}'"

        return False, None

