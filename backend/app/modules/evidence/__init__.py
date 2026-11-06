"""Multimodal Evidence Intake & OCR Module.

Provides:
- Strict file signature and path-traversal validation.
- AES-256-GCM encrypted persistence at rest.
- Pluggable OCR provider abstraction (PDF parser, rule-based receipt parser, deterministic mock, and Gemini Vision).
- Visual and textual prompt injection containment.
- PII and PCI-DSS payment data masking.
- Synchronous and asynchronous evidence processing workflows.
"""

from app.modules.evidence.pipeline import EvidenceProcessingPipeline
from app.modules.evidence.schemas import (
    EvidenceDetailResponse,
    EvidenceExtractionResponse,
    EvidenceStatusResponse,
    EvidenceUploadResponse,
    StructuredInvoiceData,
)
from app.modules.evidence.service import EvidenceAuthorizationError, EvidenceService
from app.modules.evidence.storage import EvidenceStorageManager, default_evidence_storage
from app.modules.evidence.validator import (
    EmptyFileError,
    EvidenceValidationError,
    FileValidator,
    OversizedFileError,
    PageCountExceededError,
    PathTraversalAttemptError,
    SignatureMismatchError,
    UnsupportedMediaTypeError,
    ValidatedFileMetadata,
)

__all__ = [
    "EvidenceService",
    "EvidenceStorageManager",
    "default_evidence_storage",
    "FileValidator",
    "ValidatedFileMetadata",
    "EvidenceValidationError",
    "EmptyFileError",
    "OversizedFileError",
    "UnsupportedMediaTypeError",
    "SignatureMismatchError",
    "PageCountExceededError",
    "PathTraversalAttemptError",
    "EvidenceAuthorizationError",
    "EvidenceProcessingPipeline",
    "EvidenceUploadResponse",
    "EvidenceDetailResponse",
    "EvidenceStatusResponse",
    "EvidenceExtractionResponse",
    "StructuredInvoiceData",
]

