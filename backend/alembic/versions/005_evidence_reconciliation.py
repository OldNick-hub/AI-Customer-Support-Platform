"""Evidence Reconciliation and Intelligent Mismatch Detection Schema Additions

Revision ID: 005_evidence_reconciliation
Revises: 004_evidence_intake_ocr
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "005_evidence_reconciliation"
down_revision = "004_evidence_intake_ocr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. evidence_verifications: add conversational context, reconciliation verdicts, clarification status & job tracking
    op.add_column(
        "evidence_verifications",
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("reconciliation_status", sa.String(50), server_default="MATCH", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("clarification_status", sa.String(50), server_default="NONE", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("extracted_claims", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("field_comparisons", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("overall_confidence", sa.Float(), server_default="1.0", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("job_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("job_status", sa.String(50), server_default="COMPLETED", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("error_details", sa.Text(), nullable=True),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("evidence_verifications", "verified_at", nullable=True)

    # 2. Indexes for efficient lookup & idempotency
    op.create_index(
        "ix_evidence_verifications_conversation_id",
        "evidence_verifications",
        ["conversation_id"],
    )
    op.create_index(
        "ix_evidence_verifications_message_id",
        "evidence_verifications",
        ["message_id"],
    )
    op.create_index(
        "ix_evidence_verifications_reconciliation_status",
        "evidence_verifications",
        ["reconciliation_status"],
    )
    op.create_index(
        "ix_evidence_verifications_clarification_status",
        "evidence_verifications",
        ["clarification_status"],
    )
    op.create_index(
        "ix_evidence_verifications_job_id",
        "evidence_verifications",
        ["job_id"],
    )
    op.create_index(
        "ix_evidence_verifications_job_status",
        "evidence_verifications",
        ["job_status"],
    )
    op.create_index(
        "ix_evidence_verifications_idempotency_key",
        "evidence_verifications",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_evidence_verifications_conv_idem",
        "evidence_verifications",
        ["conversation_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_verifications_conv_idem", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_idempotency_key", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_job_status", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_job_id", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_clarification_status", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_reconciliation_status", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_message_id", table_name="evidence_verifications")
    op.drop_index("ix_evidence_verifications_conversation_id", table_name="evidence_verifications")

    op.alter_column("evidence_verifications", "verified_at", nullable=False)
    op.drop_column("evidence_verifications", "processing_started_at")
    op.drop_column("evidence_verifications", "created_at")
    op.drop_column("evidence_verifications", "resolved_at")
    op.drop_column("evidence_verifications", "error_details")
    op.drop_column("evidence_verifications", "idempotency_key")
    op.drop_column("evidence_verifications", "retry_count")
    op.drop_column("evidence_verifications", "job_status")
    op.drop_column("evidence_verifications", "job_id")
    op.drop_column("evidence_verifications", "overall_confidence")
    op.drop_column("evidence_verifications", "field_comparisons")
    op.drop_column("evidence_verifications", "extracted_claims")
    op.drop_column("evidence_verifications", "clarification_status")
    op.drop_column("evidence_verifications", "reconciliation_status")
    op.drop_column("evidence_verifications", "message_id")
    op.drop_column("evidence_verifications", "conversation_id")
