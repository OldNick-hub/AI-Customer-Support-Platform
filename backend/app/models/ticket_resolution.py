"""Ticket Resolution and Action Orchestration ORM Models.

Stores structured resolution recommendations, playbook evaluation outcomes,
orchestrated actions with approval states, and verification details.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSONBType, UUIDPrimaryKeyMixin, utc_now


class TicketResolution(Base, UUIDPrimaryKeyMixin):
    """Structured resolution verdict and playbook guidance for a support ticket.

    One-to-one with Ticket. Orthogonal to operational ticket status, tracking
    the lifecycle of resolution evaluation, proposed actions, and verification.
    """

    __tablename__ = "ticket_resolutions"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True, unique=True
    )

    # Resolution lifecycle state
    # 'DRAFT', 'EVALUATED', 'IN_PROGRESS', 'AWAITING_APPROVAL', 'VERIFYING', 'RESOLVED', 'ESCALATED', 'FAILED'
    status: Mapped[str] = mapped_column(
        String(50), default="DRAFT", nullable=False, index=True
    )

    # Playbook category evaluated
    playbook_category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Human-readable recommended resolution plan
    recommended_resolution: Mapped[str] = mapped_column(Text, nullable=False)

    # Confidence score [0.0 - 1.0]
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Deterministic eligibility verdict: 'ELIGIBLE', 'INELIGIBLE', 'NEEDS_APPROVAL', 'NEEDS_INFORMATION', 'ESCALATE'
    eligibility_decision: Mapped[str] = mapped_column(
        String(50), default="ELIGIBLE", nullable=False, index=True
    )

    # True if any required action involves sensitive financial, security, or irreversible changes
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Auditable facts and evidence references supporting the decision
    supporting_evidence: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # Explanation if resolution is rejected or escalated
    rejection_or_escalation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Verification stage metadata
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Incremented if re-evaluated
    resolution_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="ticket_resolution"
    )
    actions: Mapped[List["ResolutionAction"]] = relationship(
        "ResolutionAction",
        back_populates="resolution",
        cascade="all, delete-orphan",
        order_by="ResolutionAction.created_at",
        lazy="selectin",
    )


class ResolutionAction(Base, UUIDPrimaryKeyMixin):
    """An idempotent, auditable action orchestrated as part of a ticket resolution.

    May require supervisor approval if marked sensitive (financial, security, or irreversible).
    """

    __tablename__ = "resolution_actions"

    resolution_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ticket_resolutions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Action type identifier, e.g. 'SEND_STATUS_UPDATE', 'ISSUE_REFUND', 'RESET_CREDENTIALS'
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Idempotency key unique per resolution
    action_key: Mapped[str] = mapped_column(String(150), nullable=False, index=True)

    # Structured parameters for execution
    action_params: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    # Action execution state
    # 'PROPOSED', 'AWAITING_APPROVAL', 'APPROVED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED'
    status: Mapped[str] = mapped_column(
        String(50), default="PROPOSED", nullable=False, index=True
    )

    # Sensitivity and approval flags
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Approval trail
    approved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Rejection trail
    rejected_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Execution tracking & bounded retries
    execution_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    execution_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_result: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    resolution: Mapped["TicketResolution"] = relationship(
        "TicketResolution", back_populates="actions"
    )
    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="resolution_actions"
    )

    __table_args__ = (
        UniqueConstraint("resolution_id", "action_key", name="uq_resolution_action_key"),
    )
