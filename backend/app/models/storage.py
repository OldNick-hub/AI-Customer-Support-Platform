"""Storage Object Database Model for PostgreSQL BYTEA Persistence.

Provides a dedicated database model for storing encrypted binary payloads,
content types, metadata, and checksums under tenant isolation.
"""

from typing import Any, Dict
from sqlalchemy import BigInteger, Index, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONBType, TimestampMixin, UUIDPrimaryKeyMixin


class StorageObject(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Dedicated model for binary object storage backed by PostgreSQL BYTEA."""

    __tablename__ = "storage_objects"

    tenant_id: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default", index=True
    )
    key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/octet-stream"
    )
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(
        JSONBType, nullable=False, default=dict
    )

    __table_args__ = (
        Index("uq_storage_objects_tenant_key", "tenant_id", "key", unique=True),
        Index("ix_storage_objects_tenant_size", "tenant_id", "file_size_bytes"),
    )

