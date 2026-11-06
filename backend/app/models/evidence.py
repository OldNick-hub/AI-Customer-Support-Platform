"""Multimodal Evidence Files, OCR Extracted Entities, and Claim Verifications.

Models persistent disk staging, structured OCR parsing, visual prompt injection verdicts,
and automated 30-day retention lifecycles.
"""

import uuid
from datetime import date, datetime
from typing import List, Optional
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSONBType, UUIDPrimaryKeyMixin, utc_now


class EvidenceFile(Base, UUIDPrimaryKeyMixin):
    """Customer uploaded receipt, screenshot, or PDF invoice with persistent staging."""

    __tablename__ = "evidence_files"

    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticket_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Staged to disk before background processing
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), default="STAGED", nullable=False, index=True
    )  # 'STAGED', 'ANALYZING', 'VERIFIED', 'CLAIM_CONFLICT', 'FLAGGED_UNSAFE', 'FAILED', 'PURGED', 'EXPIRED'
    is_malicious: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    quarantine_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )  # Configurable TTL (default 30 days)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    message: Mapped[Optional["Message"]] = relationship(  # type: ignore[name-defined]
        "Message", back_populates="evidence_files"
    )
    conversation: Mapped[Optional["Conversation"]] = relationship(  # type: ignore[name-defined]
        "Conversation", back_populates="evidence_files"
    )
    customer: Mapped[Optional["Customer"]] = relationship(  # type: ignore[name-defined]
        "Customer", back_populates="evidence_files"
    )
    ticket: Mapped[Optional["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="evidence_files"
    )
    extracted_entities: Mapped[List["ExtractedEvidenceEntity"]] = relationship(
        "ExtractedEvidenceEntity", back_populates="evidence_file", cascade="all, delete-orphan", lazy="selectin"
    )
    verifications: Mapped[List["EvidenceVerification"]] = relationship(
        "EvidenceVerification", back_populates="evidence_file", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_evidence_files_conv_hash", "conversation_id", "sha256_hash"),
    )


class ExtractedEvidenceEntity(Base, UUIDPrimaryKeyMixin):
    """Structured entities parsed from uploaded documents by Vision / OCR."""

    __tablename__ = "extracted_evidence_entities"

    evidence_file_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("evidence_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extracted_order_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )
    extracted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    extracted_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    extracted_currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    extracted_product_sku: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extracted_error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    raw_ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ocr_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    pii_redacted_text: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_metadata: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    evidence_file: Mapped["EvidenceFile"] = relationship(
        "EvidenceFile", back_populates="extracted_entities"
    )


class EvidenceVerification(Base, UUIDPrimaryKeyMixin):
    """Cross-verification check comparing extracted document entities against conversational claims."""

    __tablename__ = "evidence_verifications"

    evidence_file_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("evidence_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    matches_order_id: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    matches_amount: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    matches_product: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    overall_match_status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # 'VERIFIED_MATCH', 'PARTIAL_MISMATCH', 'TOTAL_CONFLICT', 'LOW_QUALITY'
    reconciliation_status: Mapped[str] = mapped_column(
        String(50), default="MATCH", nullable=False, index=True
    )  # 'MATCH', 'MISMATCH', 'MISSING_FROM_EVIDENCE', 'MISSING_FROM_MESSAGE', 'UNCERTAIN', 'INSUFFICIENT_EVIDENCE'
    clarification_status: Mapped[str] = mapped_column(
        String(50), default="NONE", nullable=False, index=True
    )  # 'NONE', 'PENDING_CUSTOMER', 'RESOLVED', 'SUPERSEDED'
    clarification_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_claims: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    field_comparisons: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    overall_confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    job_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    job_status: Mapped[str] = mapped_column(
        String(50), default="COMPLETED", nullable=False, index=True
    )  # 'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    evidence_file: Mapped["EvidenceFile"] = relationship(
        "EvidenceFile", back_populates="verifications"
    )
    conversation: Mapped[Optional["Conversation"]] = relationship(  # type: ignore[name-defined]
        "Conversation", back_populates="verifications"
    )
    message: Mapped[Optional["Message"]] = relationship(  # type: ignore[name-defined]
        "Message", back_populates="verifications"
    )

    __table_args__ = (
        Index("ix_evidence_verifications_conv_idem", "conversation_id", "idempotency_key"),
    )
