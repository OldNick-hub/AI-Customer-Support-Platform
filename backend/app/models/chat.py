"""Chat Idempotency & Outbox Domain Entities.

Implements two-phase committed idempotency reservations, heartbeat leases,
and transactional outbox events with RESTRICT message deletion boundaries.
"""

from datetime import datetime
from typing import Any, Dict, Optional
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, GUID, JSONBType, UUIDPrimaryKeyMixin, utc_now


class ChatIdempotencyRecord(Base, UUIDPrimaryKeyMixin):
    """Tracks two-phase chat turn reservation, heartbeat leases, and encrypted cached responses."""

    __tablename__ = "chat_idempotency_records"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="IN_PROGRESS", nullable=False)
    owner_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_response_payload: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    response_status_code: Mapped[Optional[int]] = mapped_column(Integer, default=200, nullable=True)
    customer_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    agent_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    turn_sequence_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("customer_id", "session_id", "idempotency_key", name="uq_chat_idempotency_cust_sess_key"),
        Index("ix_chat_idempotency_cust_sess", "customer_id", "session_id"),
        Index("ix_chat_idempotency_conv_id", "conversation_id"),
        Index("ix_chat_idempotency_lease", "status", "lease_expires_at"),
        Index("ix_chat_idempotency_expires", "expires_at"),
    )


class ChatOutboxEvent(Base, UUIDPrimaryKeyMixin):
    """Transactional outbox staging entity for conversation turns with RESTRICT deletion boundaries."""

    __tablename__ = "chat_outbox_events"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    customer_message_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_message_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), default="CONVERSATION_TURN_COMPLETED", nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONBType, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_cust_msg_conv",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agent_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_agent_msg_conv",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("conversation_id", "sequence_number", name="uq_chat_outbox_conv_seq"),
        UniqueConstraint("customer_message_id", name="uq_chat_outbox_cust_msg"),
        UniqueConstraint("agent_message_id", name="uq_chat_outbox_agent_msg"),
        Index("ix_chat_outbox_status_retry", "status", "next_retry_at"),
        Index("ix_chat_outbox_conv_seq", "conversation_id", "sequence_number"),
        Index("ix_chat_outbox_cust_sess", "customer_id", "session_id"),
    )

