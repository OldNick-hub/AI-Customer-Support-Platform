"""Multimodal Security Guard and Advanced Evidence Cross-Verification Schema Additions

Revision ID: 006_multimodal_security_guard
Revises: 005_evidence_reconciliation
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "006_multimodal_security_guard"
down_revision = "005_evidence_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. evidence_files: add purged_at timestamp for retention lifecycle
    op.add_column(
        "evidence_files",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_files_purged_at",
        "evidence_files",
        ["purged_at"],
    )

    # 2. evidence_verifications: add secondary_evidence_file_id and security_flags
    op.add_column(
        "evidence_verifications",
        sa.Column(
            "secondary_evidence_file_id",
            sa.String(36),
            sa.ForeignKey("evidence_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_evidence_verifications_secondary_evidence_file_id",
        "evidence_verifications",
        ["secondary_evidence_file_id"],
    )
    op.add_column(
        "evidence_verifications",
        sa.Column("security_flags", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("evidence_verifications", "security_flags")
    op.drop_index(
        "ix_evidence_verifications_secondary_evidence_file_id",
        table_name="evidence_verifications",
    )
    op.drop_column("evidence_verifications", "secondary_evidence_file_id")

    op.drop_index("ix_evidence_files_purged_at", table_name="evidence_files")
    op.drop_column("evidence_files", "purged_at")

