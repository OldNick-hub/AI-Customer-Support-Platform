"""Ticket Synthesis and Clarification ORM Models.

Stores structured extraction results from LLM-based conversation analysis,
completeness evaluation outcomes, and per-round customer clarification state.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSONBType, UUIDPrimaryKeyMixin, utc_now


class TicketSynthesis(Base, UUIDPrimaryKeyMixin):
    """Structured extraction of support issue details from a customer conversation.

    One-to-one with Ticket. Created or updated each time the synthesis engine
    processes the conversation. Synthesis version is incremented on updates so
    history is preserved via TicketEvent audit records.
    """

    __tablename__ = "ticket_syntheses"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True, unique=True
    )

    # --- Core Problem Extraction ---
    problem_statement: Mapped[str] = mapped_column(Text, nullable=False)
    product_or_service: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # List of {type: str, value: str} dicts — order IDs, invoice numbers, tracking IDs, etc.
    reference_identifiers: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    issue_category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    issue_subcategory: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # What the customer expects to happen (refund, replacement, explanation, etc.)
    resolution_sought: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # List of plain-text strings — steps the customer already tried
    actions_already_attempted: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # List of evidence_file UUIDs (as strings) referenced in the conversation
    linked_evidence_ids: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # --- Completeness Assessment ---
    # List of field names that are absent but required for the category
    missing_fields: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # List of {field, description} dicts where extracted values are contradictory
    contradictions: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    completeness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completeness_reasons: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # --- Context & Provenance ---
    conversation_context_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 0.0 – 1.0: confidence in the extraction quality
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # {field_name: "explicit" | "inferred"} for every extracted field
    extraction_provenance: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    llm_model_used: Mapped[str] = mapped_column(String(100), default="fallback_heuristic", nullable=False)

    # Incremented each time the synthesis is re-run for this ticket
    synthesis_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="ticket_synthesis"
    )
    clarification_requests: Mapped[List["TicketClarificationRequest"]] = relationship(
        "TicketClarificationRequest",
        back_populates="synthesis",
        cascade="all, delete-orphan",
        order_by="TicketClarificationRequest.round_number",
    )

    __table_args__ = (
        Index("ix_ticket_syntheses_category", "issue_category"),
    )


class TicketClarificationRequest(Base, UUIDPrimaryKeyMixin):
    """A single round of targeted clarification questions sent to the customer.

    Multiple rounds may exist for a ticket as the customer progressively provides
    missing information. Each round captures the generated questions, the
    customer's response, and the status transition.
    """

    __tablename__ = "ticket_clarification_requests"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    synthesis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), ForeignKey("ticket_syntheses.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # 1-based: each new clarification round increments
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # List of {field: str, question: str, priority: int (1=highest)} objects
    questions: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    # Which missing fields triggered this round
    targeted_missing_fields: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    status: Mapped[str] = mapped_column(
        String(50), default="PENDING_CUSTOMER", nullable=False, index=True
    )  # 'PENDING_CUSTOMER', 'ANSWERED', 'SUPERSEDED', 'SKIPPED'

    customer_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Completeness score BEFORE this clarification round
    completeness_score_before: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Completeness score AFTER re-evaluation (populated when ANSWERED)
    completeness_score_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    synthesis: Mapped[Optional["TicketSynthesis"]] = relationship(
        "TicketSynthesis", back_populates="clarification_requests"
    )
    ticket: Mapped["Ticket"] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="clarification_requests"
    )

    __table_args__ = (
        Index("ix_ticket_clarifications_ticket_round", "ticket_id", "round_number"),
        UniqueConstraint("ticket_id", "round_number", name="uq_ticket_clarification_round"),
    )

