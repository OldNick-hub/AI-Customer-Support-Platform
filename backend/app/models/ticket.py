"""Support Ticketing, SLA Calendar, and Escalation Audit Models.

Models intelligent support tickets, dynamic priority scoring, business hour SLA calculations,
configurable PENDING_CUSTOMER pause handling, and immutable audit logs.
"""

import uuid
from datetime import datetime, time
from typing import List, Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import (
    Base,
    GUID,
    JSONBType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    utc_now,
)


class SLACalendar(Base, UUIDPrimaryKeyMixin):
    """Operational business hours and holiday calendar for SLA calculations."""

    __tablename__ = "sla_calendars"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC", nullable=False)
    work_start_time: Mapped[time] = mapped_column(
        Time, default=time(9, 0, 0), nullable=False
    )
    work_end_time: Mapped[time] = mapped_column(
        Time, default=time(17, 0, 0), nullable=False
    )
    # Days of week: 1=Monday, 7=Sunday
    work_days: Mapped[list] = mapped_column(JSONBType, default=lambda: [1, 2, 3, 4, 5], nullable=False)
    # List of ISO dates: ['2026-11-26', '2026-12-25']
    holidays: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    tickets: Mapped[List["Ticket"]] = relationship("Ticket", back_populates="sla_calendar")


class Ticket(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Central support ticket entity with dynamic priority and SLA calendar awareness."""

    __tablename__ = "tickets"

    ticket_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )  # E.g., 'TCK-202611-00042'
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customers.id"), nullable=False, index=True
    )
    assigned_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    masked_summary: Mapped[str] = mapped_column(Text, nullable=False)  # PII-free summary
    priority: Mapped[str] = mapped_column(
        String(20), default="MEDIUM", nullable=False, index=True
    )  # 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL', 'URGENT'
    priority_score: Mapped[float] = mapped_column(Float, default=2.5, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="OPEN", nullable=False, index=True
    )  # 'OPEN', 'PENDING_CUSTOMER', 'IN_PROGRESS', 'RESOLVED', 'ESCALATED', 'CLOSED'
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    cluster_group_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True, index=True
    )  # For deduplication grouping

    sla_calendar_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("sla_calendars.id"), nullable=False, index=True
    )
    sla_target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    sla_warning_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    sla_breach_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    sla_warning_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Configurable PENDING_CUSTOMER pause handling
    sla_paused_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_paused_duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="tickets")  # type: ignore[name-defined]
    conversation: Mapped[Optional["Conversation"]] = relationship(  # type: ignore[name-defined]
        "Conversation", back_populates="tickets"
    )
    assigned_agent: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", back_populates="assigned_tickets"
    )
    sla_calendar: Mapped["SLACalendar"] = relationship("SLACalendar", back_populates="tickets", lazy="selectin")
    escalation_audits: Mapped[List["EscalationAudit"]] = relationship(
        "EscalationAudit", back_populates="ticket", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[List["TicketEvent"]] = relationship(
        "TicketEvent", back_populates="ticket", cascade="all, delete-orphan"
    )
    evidence_files: Mapped[List["EvidenceFile"]] = relationship(  # type: ignore[name-defined]
        "EvidenceFile", back_populates="ticket"
    )

    __table_args__ = (
        Index("idx_tickets_assigned_status", "assigned_agent_id", "status"),
        Index("idx_tickets_sla_warning_check", "sla_warning_at", "sla_warning_sent", "status"),
        Index("idx_tickets_sla_breach_check", "sla_breach_at", "sla_breached", "status"),
    )


class EscalationAudit(Base, UUIDPrimaryKeyMixin):
    """Deterministic audit record for all manual and automated ticket escalations."""

    __tablename__ = "escalation_audits"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_condition: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )  # 'SLA_BREACH', 'SENTIMENT_FRUSTRATION', 'HIGH_RISK_SAFETY', 'INACTIVITY_TIMEOUT', 'MANUAL_OVERRIDE'
    previous_state: Mapped[str] = mapped_column(String(50), nullable=False)
    new_state: Mapped[str] = mapped_column(String(50), nullable=False)
    escalation_reason: Mapped[str] = mapped_column(Text, nullable=False)
    escalation_payload: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    escalated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="escalation_audits")
    escalated_by_user: Mapped[Optional["User"]] = relationship("User")  # type: ignore[name-defined]


class TicketEvent(Base, UUIDPrimaryKeyMixin):
    """Audit log of ticket lifecycle events (status changes, re-assignments, comments)."""

    __tablename__ = "ticket_events"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    event_payload: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="events")
