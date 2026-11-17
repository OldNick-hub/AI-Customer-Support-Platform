"""Evidence Purge Reliability and Bounded Retry Schema Additions

Revision ID: 007_evidence_purge_reliability
Revises: 006_multimodal_security_guard
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "007_evidence_purge_reliability"
down_revision = "006_multimodal_security_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. evidence_files: add purge_retry_count and last_purge_attempt_at
    op.add_column(
        "evidence_files",
        sa.Column("purge_retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "evidence_files",
        sa.Column("last_purge_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. evidence_files: add compound retention index
    op.create_index(
        "ix_evidence_files_retention_lookup",
        "evidence_files",
        ["status", "retention_expires_at", "purge_retry_count"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_files_retention_lookup", table_name="evidence_files")
    op.drop_column("evidence_files", "last_purge_attempt_at")
    op.drop_column("evidence_files", "purge_retry_count")

