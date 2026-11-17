"""Intelligent Ticket Synthesis and Clarification Schema

Revision ID: 008_intelligent_ticket_synthesis
Revises: 007_evidence_purge_reliability
Create Date: 2026-11-06
"""

from alembic import op
import sqlalchemy as sa

revision = "008_intelligent_ticket_synthesis"
down_revision = "007_evidence_purge_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add synthesis_status to tickets table
    op.add_column(
        "tickets",
        sa.Column(
            "synthesis_status",
            sa.String(50),
            server_default="DRAFT",
            nullable=False,
        ),
    )
    op.create_index("ix_tickets_synthesis_status", "tickets", ["synthesis_status"])

    # 2. Create ticket_syntheses table
    op.create_table(
        "ticket_syntheses",
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("problem_statement", sa.Text(), nullable=False),
        sa.Column("product_or_service", sa.String(500), nullable=True),
        sa.Column("reference_identifiers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("issue_category", sa.String(100), nullable=False),
        sa.Column("issue_subcategory", sa.String(100), nullable=True),
        sa.Column("resolution_sought", sa.Text(), nullable=True),
        sa.Column("actions_already_attempted", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("linked_evidence_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("missing_fields", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("contradictions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("completeness_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("completeness_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("conversation_context_summary", sa.Text(), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("extraction_provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("llm_model_used", sa.String(100), nullable=False, server_default="fallback_heuristic"),
        sa.Column("synthesis_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_syntheses_ticket_id", "ticket_syntheses", ["ticket_id"], unique=True)
    op.create_index("ix_ticket_syntheses_category", "ticket_syntheses", ["issue_category"])

    # 3. Create ticket_clarification_requests table
    op.create_table(
        "ticket_clarification_requests",
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("synthesis_id", sa.String(36), sa.ForeignKey("ticket_syntheses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("targeted_missing_fields", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(50), nullable=False, server_default="PENDING_CUSTOMER"),
        sa.Column("customer_response", sa.Text(), nullable=True),
        sa.Column("completeness_score_before", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("completeness_score_after", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ticket_id", "round_number", name="uq_ticket_clarification_round"),
    )
    op.create_index("ix_ticket_clarifications_ticket_id", "ticket_clarification_requests", ["ticket_id"])
    op.create_index("ix_ticket_clarification_status", "ticket_clarification_requests", ["status"])
    op.create_index(
        "ix_ticket_clarifications_ticket_round",
        "ticket_clarification_requests",
        ["ticket_id", "round_number"],
    )


def downgrade() -> None:
    op.drop_table("ticket_clarification_requests")
    op.drop_index("ix_ticket_syntheses_category", "ticket_syntheses")
    op.drop_index("ix_ticket_syntheses_ticket_id", "ticket_syntheses")
    op.drop_table("ticket_syntheses")
    op.drop_index("ix_tickets_synthesis_status", "tickets")
    op.drop_column("tickets", "synthesis_status")

