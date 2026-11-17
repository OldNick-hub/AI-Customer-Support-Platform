"""Knowledge Base Ingestion Retries Schema.

Revision ID: 013_kb_ingestion_retries
Revises: 012_document_tenant_isolation
Create Date: 2026-11-08
"""

from alembic import op
import sqlalchemy as sa
from app.db.base import JSONBType

revision = "013_kb_ingestion_retries"
down_revision = "012_document_tenant_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_document_ingestion_jobs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("document_code", sa.String(100), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="RETRY_SCHEDULED"),
        sa.Column("failure_type", sa.String(50), nullable=False, server_default="RETRYABLE"),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interruption_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initial_failure_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retry_metadata", JSONBType, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("retry_count >= 0 AND retry_count <= 3", name="chk_kb_ingestion_jobs_retry_count"),
        sa.CheckConstraint("interruption_count >= 0 AND interruption_count <= 3", name="chk_kb_ingestion_jobs_interruption_count"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_kb_ingestion_jobs_tenant_idempotency"),
    )

    op.create_index("ix_kb_ingestion_jobs_tenant_id", "kb_document_ingestion_jobs", ["tenant_id"])
    op.create_index("ix_kb_ingestion_jobs_status", "kb_document_ingestion_jobs", ["status"])
    op.create_index("ix_kb_ingestion_jobs_schedule", "kb_document_ingestion_jobs", ["status", "next_retry_at"])
    op.create_index("ix_kb_ingestion_jobs_lease", "kb_document_ingestion_jobs", ["status", "lease_expires_at"])
    op.create_index("ix_kb_ingestion_jobs_tenant_doc", "kb_document_ingestion_jobs", ["tenant_id", "document_code"])
    op.create_index("ix_kb_ingestion_jobs_sha256", "kb_document_ingestion_jobs", ["sha256_hash"])
    op.create_index("ix_kb_ingestion_jobs_document_id", "kb_document_ingestion_jobs", ["document_id"])

    # Grant explicit permissions to app_user if role exists
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON kb_document_ingestion_jobs TO app_user;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_index("ix_kb_ingestion_jobs_document_id", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_sha256", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_tenant_doc", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_lease", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_schedule", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_status", table_name="kb_document_ingestion_jobs")
    op.drop_index("ix_kb_ingestion_jobs_tenant_id", table_name="kb_document_ingestion_jobs")
    op.drop_table("kb_document_ingestion_jobs")
