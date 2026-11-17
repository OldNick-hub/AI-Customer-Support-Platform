"""Pydantic Request & Response Schemas for Multimodal Evidence Intake & OCR.

Provides strict, validated, typed schemas for:
- File upload responses
- Evidence metadata and status
- Structured invoice and receipt entities
- Page-level extraction details
- Extraction warnings and confidence scores
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field, field_validator


class FinancialVerificationLevel(str, Enum):
    """Explicit trust hierarchy for financial entity and reconciliation claims."""

    EXTRACTED = "EXTRACTED"
    DOCUMENT_CORROBORATED = "DOCUMENT_CORROBORATED"
    PAYMENT_INDEPENDENTLY_VERIFIED = "PAYMENT_INDEPENDENTLY_VERIFIED"


class TransactionIdentifierType(str, Enum):
    """Categorized payment and transaction reference types."""

    ORDER_ID = "ORDER_ID"
    INVOICE_NUMBER = "INVOICE_NUMBER"
    TRANSACTION_ID = "TRANSACTION_ID"
    AUTHORIZATION_CODE = "AUTHORIZATION_CODE"
    PAYMENT_REFERENCE = "PAYMENT_REFERENCE"
    CHARGE_ID = "CHARGE_ID"
    UNKNOWN = "UNKNOWN"


class ReceiptLineItem(BaseModel):
    """Individual line item extracted from an invoice or receipt."""

    product_name: str = Field(..., description="Product description or title")
    quantity: Optional[float] = Field(default=1.0, description="Extracted quantity")
    unit_price: Optional[float] = Field(default=None, description="Price per unit")
    total_price: Optional[float] = Field(default=None, description="Line item total price")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence score (0.0-1.0)")


class PageExtractionMetadata(BaseModel):
    """Page-level extraction details for multi-page documents (PDFs)."""

    page_number: int = Field(..., ge=1, description="1-indexed page number")
    char_count: int = Field(default=0, ge=0, description="Character count extracted from page")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Page extraction confidence")
    orientation_degrees: Optional[int] = Field(default=0, description="Detected rotation (0, 90, 180, 270)")
    has_images: bool = Field(default=False, description="Whether page contains embedded images")


class StructuredInvoiceData(BaseModel):
    """Structured fields parsed from document images or PDFs."""

    order_id: Optional[str] = Field(default=None, description="Extracted customer order identifier (e.g. ORD-12345)")
    invoice_number: Optional[str] = Field(default=None, description="Vendor or merchant invoice reference number")
    invoice_date: Optional[date] = Field(default=None, description="Normalized invoice issuance date")
    transaction_date: Optional[date] = Field(default=None, description="Normalized payment transaction date")
    total_amount: Optional[float] = Field(default=None, ge=0.0, description="Final gross total payable amount")
    currency: Optional[str] = Field(default=None, description="3-letter ISO-4217 currency code (USD, EUR, GBP, INR)")
    tax_amount: Optional[float] = Field(default=None, ge=0.0, description="Extracted sales tax, VAT, or GST amount")
    product_name: Optional[str] = Field(default=None, description="Primary product or service description")
    product_quantity: Optional[float] = Field(default=None, description="Primary product quantity")
    product_price: Optional[float] = Field(default=None, description="Primary product price")
    merchant_name: Optional[str] = Field(default=None, description="Seller, store, vendor, or merchant organization name")
    line_items: List[ReceiptLineItem] = Field(default_factory=list, description="Itemized product purchase lines")
    transaction_identifier_type: Optional[TransactionIdentifierType] = Field(
        default=None, description="Preserved classification of extracted transaction reference"
    )

    # Confidence and Validation Meta
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Aggregate extraction confidence")
    field_confidence: Dict[str, float] = Field(default_factory=dict, description="Field-by-field confidence scores")
    uncertain_fields: List[str] = Field(default_factory=list, description="Fields below confidence threshold (< 0.70)")
    missing_fields: List[str] = Field(default_factory=list, description="Required or standard fields not found in document")
    warnings: List[str] = Field(default_factory=list, description="Data ambiguity or validation warnings")

    # Document Structure Meta
    page_count: int = Field(default=1, ge=1, description="Total document pages processed")
    pages: List[PageExtractionMetadata] = Field(default_factory=list, description="Per-page extraction breakdown")
    is_scanned: bool = Field(default=False, description="True if document required OCR instead of embedded digital text")
    provider_used: str = Field(default="mock", description="OCR or extraction engine that parsed the document")
    sha256_hash: Optional[str] = Field(default=None, description="SHA-256 hash of the evidence file payload")
    pii_redacted_text: str = Field(default="", description="Full extracted text with customer PII sanitized")


class EvidenceUploadResponse(BaseModel):
    """Response returned upon successful evidence file upload."""

    evidence_id: str = Field(..., description="Unique UUID of the stored evidence record")
    conversation_id: str = Field(..., description="Associated conversation thread UUID")
    customer_id: Optional[str] = Field(default=None, description="Owner customer profile UUID")
    file_name: str = Field(..., description="Sanitized client filename")
    mime_type: str = Field(..., description="Authoritative detected MIME type")
    file_size_bytes: int = Field(..., description="Stored file size in bytes")
    sha256_hash: str = Field(..., description="Cryptographic SHA-256 fingerprint of file payload")
    status: str = Field(..., description="Processing status ('STAGED', 'ANALYZING', 'VERIFIED', 'FLAGGED_UNSAFE')")
    is_malicious: bool = Field(default=False, description="True if document triggered visual prompt injection or malware rules")
    quarantine_reason: Optional[str] = Field(default=None, description="Reason if document was flagged unsafe")
    processing_error: Optional[str] = Field(default=None, description="Optional error or timeout note")
    retention_expires_at: datetime = Field(..., description="30-day retention expiration timestamp (UTC)")
    created_at: datetime = Field(..., description="Upload timestamp (UTC)")
    structured_entities: Optional[StructuredInvoiceData] = Field(
        default=None, description="Structured parsed entities if processed synchronously"
    )


class EvidenceDetailResponse(BaseModel):
    """Detailed evidence record with processing status and audit timestamps."""

    evidence_id: str
    conversation_id: Optional[str] = None
    customer_id: Optional[str] = None
    message_id: Optional[str] = None
    ticket_id: Optional[str] = None
    file_name: str
    mime_type: str
    file_size_bytes: int
    sha256_hash: str
    status: str
    is_malicious: bool
    quarantine_reason: Optional[str] = None
    processing_error: Optional[str] = None
    retention_expires_at: datetime
    created_at: datetime
    processed_at: Optional[datetime] = None


class EvidenceStatusResponse(BaseModel):
    """Lightweight polling response for background evidence processing status."""

    evidence_id: str
    status: str
    is_malicious: bool
    quarantine_reason: Optional[str] = None
    processing_error: Optional[str] = None
    processed_at: Optional[datetime] = None


class EvidenceExtractionResponse(BaseModel):
    """Full structured extraction results for an evidence record."""

    evidence_id: str
    status: str
    extracted_at: datetime
    ocr_confidence: float
    pii_redacted_text: str
    entities: StructuredInvoiceData


class EvidenceListItem(BaseModel):
    """Sanitized evidence list summary item for agent/staff review."""

    id: str
    evidence_id: str
    conversation_id: Optional[str] = None
    customer_id: Optional[str] = None
    message_id: Optional[str] = None
    ticket_id: Optional[str] = None
    file_name: str
    file_type: str = "UNKNOWN"
    file_size_bytes: int
    mime_type: str
    classification: str = "EVIDENCE"
    verification_status: str = "STAGED"
    status: str = "STAGED"
    uploaded_at: datetime
    created_at: datetime
    processed_at: Optional[datetime] = None
    is_quarantined: bool = False
    is_malicious: bool = False
    quarantine_reason: Optional[str] = None
    processing_error: Optional[str] = None
    ocr_extracted_text: Optional[str] = None
    sha256_hash: Optional[str] = None
    retention_expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EvidenceListResponse(BaseModel):
    """Paginated collection of authorized evidence records."""

    items: List[EvidenceListItem]
    total_count: int
    limit: int
    offset: int


class ReconciliationRequest(BaseModel):
    """Payload to initiate reconciliation of conversational claim against document evidence."""

    conversation_id: str = Field(..., description="Target conversation UUID")
    evidence_id: str = Field(..., description="Target evidence file UUID")
    secondary_evidence_id: Optional[str] = Field(default=None, description="Optional secondary evidence file UUID for multi-document verification")
    claim_text: str = Field(..., min_length=1, description="Customer statement or assertion to verify")
    message_id: Optional[str] = Field(default=None, description="Optional associated message UUID")
    idempotency_key: Optional[str] = Field(default=None, description="Optional idempotency key to prevent duplicate runs")
    async_job: bool = Field(default=False, description="Whether to queue processing in the background")


class ReconciliationResponse(BaseModel):
    """Detailed reconciliation verdict comparing conversational claim against evidence."""

    verification_id: str
    evidence_id: str
    secondary_evidence_id: Optional[str] = None
    conversation_id: str
    message_id: Optional[str] = None
    job_id: Optional[str] = None
    job_status: str
    reconciliation_status: str
    overall_match_status: str
    overall_confidence: float
    summary: str
    clarification_status: str
    clarification_prompt: Optional[str] = None
    field_comparisons: List[Dict[str, Any]] = Field(default_factory=list)
    extracted_claims: List[Dict[str, Any]] = Field(default_factory=list)
    security_flags: List[str] = Field(default_factory=list)
    is_quarantined: bool = False
    verification_level: FinancialVerificationLevel = FinancialVerificationLevel.DOCUMENT_CORROBORATED
    is_refund_authorized: bool = Field(
        default=False,
        description="Whether a financial refund is authorized. Document-level corroboration alone CANNOT authorize refunds.",
    )
    requires_authoritative_verification: bool = Field(
        default=True,
        description="Whether external gateway settlement or human review is required before refund execution.",
    )
    created_at: Optional[datetime] = None
    processing_started_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


class ReconciliationJobStatusResponse(BaseModel):
    """Polling response for asynchronous reconciliation job progress."""

    job_id: str
    job_status: str
    verification_id: Optional[str] = None
    reconciliation_status: Optional[str] = None
    error_details: Optional[str] = None
    retry_count: int = 0
    updated_at: datetime


class ClarificationItemResponse(BaseModel):
    """Individual active or historical clarification request."""

    verification_id: str
    conversation_id: str
    status: str
    prompt_message: str
    mismatched_fields: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    uncertain_fields: List[str] = Field(default_factory=list)
    created_at: datetime
    resolved_at: Optional[datetime] = None


class ClarificationResponseRequest(BaseModel):
    """Customer submission to answer or clarify a discrepancy."""

    response_text: str = Field(..., min_length=1, description="Customer explanation or corrected data")
    attachment_evidence_id: Optional[str] = Field(default=None, description="Optional new evidence file ID uploaded")

