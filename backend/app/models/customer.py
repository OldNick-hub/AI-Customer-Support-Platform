"""Customer Profiles, Sessions, Conversations, and Dialog Messages.

Models conversational state, 30-minute idle session lifecycle, and PII-masked message persistence.
"""

import uuid
from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSONBType, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class Customer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Customer profile representing external CRM/e-commerce customers."""

    __tablename__ = "customers"

    external_id: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    customer_tier: Mapped[str] = mapped_column(
        String(50), default="STANDARD", nullable=False, index=True
    )  # 'STANDARD', 'VIP', 'ENTERPRISE'
    metadata_: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    sessions: Mapped[List["Session"]] = relationship("Session", back_populates="customer")
    conversations: Mapped[List["Conversation"]] = relationship(
        "Conversation", back_populates="customer"
    )
    tickets: Mapped[List["Ticket"]] = relationship("Ticket", back_populates="customer")  # type: ignore[name-defined]


class Session(Base, UUIDPrimaryKeyMixin):
    """Customer session enforcing 30-minute idle expiry and 24-hour summary restoration."""

    __tablename__ = "sessions"

    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_token: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    current_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="ACTIVE", nullable=False, index=True
    )  # 'ACTIVE', 'IDLE_CLOSED', 'ARCHIVED'
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    context_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    customer: Mapped[Optional["Customer"]] = relationship("Customer", back_populates="sessions")
    conversations: Mapped[List["Conversation"]] = relationship(
        "Conversation", back_populates="session", cascade="all, delete-orphan"
    )


class Conversation(Base, UUIDPrimaryKeyMixin):
    """Encapsulates an ongoing multi-turn support dialogue thread."""

    __tablename__ = "conversations"

    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    primary_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="OPEN", nullable=False, index=True
    )  # 'OPEN', 'RESOLVED', 'ESCALATED', 'ABANDONED'
    overall_sentiment_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_escalated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    negative_detected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    session: Mapped["Session"] = relationship("Session", back_populates="conversations")
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer", back_populates="conversations"
    )
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan", lazy="selectin"
    )
    tickets: Mapped[List["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="conversation"
    )
    escalation_audits: Mapped[List["EscalationAudit"]] = relationship(  # type: ignore[name-defined]
        "EscalationAudit", back_populates="conversation"
    )


class Message(Base, UUIDPrimaryKeyMixin):
    """Individual dialog message with PII masked by default and optional authenticated raw encryption."""

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # 'CUSTOMER', 'AI_AGENT', 'HUMAN_AGENT', 'SYSTEM'
    sender_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    # Masked content stored by default (GDPR and PCI-DSS compliance)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional AES-256-GCM encrypted raw content for compliance/forensic audit
    encrypted_raw_content: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    detected_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    language_confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turn_sequence_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")
    sentiment_log: Mapped[Optional["SentimentLog"]] = relationship(  # type: ignore[name-defined]
        "SentimentLog", back_populates="message", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    evidence_files: Mapped[List["EvidenceFile"]] = relationship(  # type: ignore[name-defined]
        "EvidenceFile", back_populates="message"
    )

    __table_args__ = (
        Index("idx_messages_conv_created", "conversation_id", "created_at"),
        Index("ix_messages_turn_seq", "conversation_id", "turn_sequence_number"),
        UniqueConstraint("id", "conversation_id", name="uq_messages_id_conversation"),
        Index(
            "uq_messages_conv_turn_sender",
            "conversation_id",
            "turn_sequence_number",
            "sender_type",
            unique=True,
            postgresql_where=text("turn_sequence_number IS NOT NULL"),
        ),
    )
