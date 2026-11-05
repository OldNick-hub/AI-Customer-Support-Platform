"""Database Base Models, Custom Dialect Types, and Common Mixins.

Defines the SQLAlchemy 2.0 DeclarativeBase, cross-database UUID and JSONB types
(seamlessly operating on PostgreSQL in production and SQLite in testing),
and shared timestamp/UUID mixins.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import BigInteger, DateTime, Sequence, TypeDecorator, String, JSON
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB as PG_JSONB, ARRAY as PG_ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class GUID(TypeDecorator):
    """Platform-independent GUID/UUID type.

    Stores UUIDs as CHAR(36)/VARCHAR(36) across dialects, while transparently
    handling Python uuid.UUID objects in the application layer.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONBType(TypeDecorator):
    """Platform-independent JSONB type.

    Uses PostgreSQL's JSONB for indexing and GIN queries in production,
    and standard JSON in SQLite for test execution.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_JSONB(none_as_null=True))
        else:
            return dialect.type_descriptor(JSON())


class StringArrayType(TypeDecorator):
    """Platform-independent array of strings.

    Serializes list of strings as JSON across all dialects, ensuring full compatibility
    with PostgreSQL JSON columns and SQLite JSON.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(JSON())


def utc_now() -> datetime:
    """Returns current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# PostgreSQL sequence for FAISS vector IDs
faiss_vector_seq = Sequence("document_chunks_faiss_vector_id_seq", start=1, increment=1)


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy 2.0 entities."""
    pass


class UUIDPrimaryKeyMixin:
    """Mixin that injects a UUIDv4 primary key."""

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )


class TimestampMixin:
    """Mixin providing timezone-aware created_at and updated_at audit columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

