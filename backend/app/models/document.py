"""Knowledge Base Document Catalog, Immutable Versions, and Vector Chunks.

Defines authoritative documents, temporal policies, chunk mappings with BIGINT faiss_vector_id,
and GIN-indexed metadata for pre-filtered retrieval.
"""

import uuid
from datetime import date, datetime
from typing import List, Optional
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from app.db.base import (
    Base,
    GUID,
    JSONBType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    faiss_vector_seq,
    utc_now,
)


class Document(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Authoritative knowledge base document entity."""

    __tablename__ = "documents"

    tenant_id: Mapped[str] = mapped_column(
        String(100), default="default", server_default="default", nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("tenant_id", "default")
        super().__init__(*args, **kwargs)
    document_code: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )  # E.g., 'POL-REFUND-US-2026'
    category: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )  # 'POLICY', 'TROUBLESHOOTING', 'FAQ', 'PRODUCT_MANUAL'
    product_line: Mapped[str] = mapped_column(
        String(100), default="GLOBAL", nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(
        String(50), default="GLOBAL", nullable=False, index=True
    )  # 'US', 'EU', 'APAC', 'GLOBAL'
    access_level: Mapped[str] = mapped_column(
        String(50), default="PUBLIC", nullable=False, index=True
    )  # 'PUBLIC', 'REGISTERED', 'VIP', 'INTERNAL_AGENT'
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    versions: Mapped[List["DocumentVersion"]] = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )


class DocumentVersion(Base, UUIDPrimaryKeyMixin):
    """Immutable version of a knowledge base document with temporal validity."""

    __tablename__ = "document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="STAGED", nullable=False, index=True
    )  # 'STAGED', 'ACTIVE', 'SUPERSEDED', 'QUARANTINED'
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_until: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True
    )  # NULL indicates indefinite
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    document: Mapped["Document"] = relationship("Document", back_populates="versions")
    chunks: Mapped[List["DocumentChunk"]] = relationship(
        "DocumentChunk", back_populates="version", cascade="all, delete-orphan", lazy="selectin"
    )
    creator: Mapped[Optional["User"]] = relationship("User")  # type: ignore[name-defined]

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_doc_version"),
        Index("idx_doc_versions_active", "document_id", "is_current"),
        Index("idx_doc_versions_temporal", "effective_from", "effective_until"),
    )


class DocumentChunk(Base, UUIDPrimaryKeyMixin):
    """Token-split text chunk mapped 1:1 to a 64-bit integer FAISS vector ID."""

    __tablename__ = "document_chunks"

    tenant_id: Mapped[str] = mapped_column(
        String(100), default="default", server_default="default", nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict] = mapped_column(JSONBType, nullable=False)
    faiss_vector_id: Mapped[int] = mapped_column(
        BigInteger,
        faiss_vector_seq,
        unique=True,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    version: Mapped["DocumentVersion"] = relationship("DocumentVersion", back_populates="chunks")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("tenant_id", "default")
        super().__init__(*args, **kwargs)

    __table_args__ = (
        Index("idx_chunks_version_idx", "version_id", "chunk_index"),
    )


class KBDocumentIngestionJob(Base, UUIDPrimaryKeyMixin):
    """Persistent, fault-tolerant Knowledge Base document ingestion retry job.

    Tracks automated retries at 15m, 30m, and 60m intervals, worker leases,
    heartbeats, CAS fencing, and operational interruptions (up to 3 lifetime).
    """

    __tablename__ = "kb_document_ingestion_jobs"

    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    document_code: Mapped[str] = mapped_column(String(100), nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(
        String(50), default="RETRY_SCHEDULED", nullable=False
    )  # 'RETRY_SCHEDULED', 'PROCESSING', 'COMPLETED', 'DEAD_LETTER'
    failure_type: Mapped[str] = mapped_column(
        String(50), default="RETRYABLE", nullable=False
    )  # 'RETRYABLE', 'PERMANENT', 'INTERRUPTED'
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    interruption_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    initial_failure_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dead_lettered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    retry_metadata: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    document: Mapped[Optional["Document"]] = relationship("Document")

    @validates("tenant_id")
    def validate_tenant(self, key: str, value: str) -> str:
        from app.services.storage_service import validate_tenant_id
        return validate_tenant_id(value)

    @validates("storage_key")
    def validate_key(self, key: str, value: str) -> str:
        from app.services.storage_service import validate_storage_key
        return validate_storage_key(value)

    @validates("idempotency_key")
    def validate_idempotency(self, key: str, value: str) -> str:
        if not value or not isinstance(value, str) or not value.strip():
            raise ValueError("idempotency_key must be a non-empty string.")
        return value.strip()

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_kb_ingestion_jobs_tenant_idempotency"),
        CheckConstraint("retry_count >= 0 AND retry_count <= 3", name="chk_kb_ingestion_jobs_retry_count"),
        CheckConstraint("interruption_count >= 0 AND interruption_count <= 3", name="chk_kb_ingestion_jobs_interruption_count"),
        Index("ix_kb_ingestion_jobs_tenant_id", "tenant_id"),
        Index("ix_kb_ingestion_jobs_status", "status"),
        Index("ix_kb_ingestion_jobs_schedule", "status", "next_retry_at"),
        Index("ix_kb_ingestion_jobs_lease", "status", "lease_expires_at"),
        Index("ix_kb_ingestion_jobs_tenant_doc", "tenant_id", "document_code"),
        Index("ix_kb_ingestion_jobs_sha256", "sha256_hash"),
        Index("ix_kb_ingestion_jobs_document_id", "document_id"),
    )
