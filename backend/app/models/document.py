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
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
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

    title: Mapped[str] = mapped_column(String(500), nullable=False)
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

    __table_args__ = (
        Index("idx_chunks_version_idx", "version_id", "chunk_index"),
    )
