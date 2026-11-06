"""Multimodal Evidence Intake and OCR Schema Additions

Revision ID: 004_evidence_intake_ocr
Revises: 003_escalation_after_hours
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "004_evidence_intake_ocr"
down_revision = "003_escalation_after_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. evidence_files: add conversation_id and customer_id for direct ownership & authorization
    op.add_column(
        "evidence_files",
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_files",
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_files",
        sa.Column("processing_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "evidence_files",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_evidence_files_conversation_id",
        "evidence_files",
        ["conversation_id"],
    )
    op.create_index(
        "ix_evidence_files_customer_id",
        "evidence_files",
        ["customer_id"],
    )
    op.create_index(
        "ix_evidence_files_conv_hash",
        "evidence_files",
        ["conversation_id", "sha256_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_files_conv_hash", table_name="evidence_files")
    op.drop_index("ix_evidence_files_customer_id", table_name="evidence_files")
    op.drop_index("ix_evidence_files_conversation_id", table_name="evidence_files")

    op.drop_column("evidence_files", "processed_at")
    op.drop_column("evidence_files", "processing_error")
    op.drop_column("evidence_files", "customer_id")
    op.drop_column("evidence_files", "conversation_id")

