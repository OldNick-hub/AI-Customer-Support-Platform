"""Multimodal Evidence Intake, OCR & Reconciliation Module.

Provides:
- Strict file signature and path-traversal validation.
- AES-256-GCM encrypted persistence at rest.
- Pluggable OCR provider abstraction (PDF parser, rule-based receipt parser, deterministic mock, and Gemini Vision).
- Visual and textual prompt injection containment.
- PII and PCI-DSS payment data masking.
- Synchronous and asynchronous evidence processing workflows.
- Natural-language claim extraction engine (claims, amounts, dates, IDs, intents).
- Evidence reconciliation and intelligent mismatch detection.
- Clarification lifecycle (PENDING_CUSTOMER -> RESOLVED / SUPERSEDED).
"""

from app.modules.evidence.claims import (
    ClaimExtractionEngine,
    ClaimType,
    CustomerClaim,
    ExtractedClaimsPayload,
)
from app.modules.evidence.clarification import (
    ClarificationEngine,
    ClarificationRequest,
    ClarificationResponsePayload,
    ClarificationStatus,
)
from app.modules.evidence.pipeline import EvidenceProcessingPipeline
from app.modules.evidence.reconciler import (
    EvidenceReconciliationEngine,
    FieldComparisonResult,
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.modules.evidence.reconciliation_service import (
    ReconciliationAuthorizationError,
    ReconciliationNotFoundException,
    ReconciliationService,
    default_reconciliation_service,
)
from app.modules.evidence.worker import ReconciliationWorker
from app.modules.evidence.schemas import (
    ClarificationItemResponse,
    ClarificationResponseRequest,
    EvidenceDetailResponse,
    EvidenceExtractionResponse,
    EvidenceStatusResponse,
    EvidenceUploadResponse,
    ReconciliationJobStatusResponse,
    ReconciliationRequest,
    ReconciliationResponse,
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
    # Reconciliation & Mismatch Detection
    "ClaimType",
    "CustomerClaim",
    "ExtractedClaimsPayload",
    "ClaimExtractionEngine",
    "ReconciliationOutcome",
    "FieldComparisonResult",
    "ReconciliationResult",
    "EvidenceReconciliationEngine",
    "ClarificationStatus",
    "ClarificationRequest",
    "ClarificationResponsePayload",
    "ClarificationEngine",
    "ReconciliationRequest",
    "ReconciliationResponse",
    "ReconciliationJobStatusResponse",
    "ClarificationItemResponse",
    "ClarificationResponseRequest",
    "ReconciliationService",
    "default_reconciliation_service",
    "ReconciliationNotFoundException",
    "ReconciliationAuthorizationError",
    "ReconciliationWorker",
]
