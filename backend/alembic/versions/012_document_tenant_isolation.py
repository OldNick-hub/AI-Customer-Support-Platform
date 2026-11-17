"""Document and Chunk Tenant Isolation Schema.

Revision ID: 012_document_tenant_isolation
Revises: 011_storage_objects
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa

revision = "012_document_tenant_isolation"
down_revision = "011_storage_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("tenant_id", sa.String(100), nullable=False, server_default="default"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.add_column(
        "document_chunks",
        sa.Column("tenant_id", sa.String(100), nullable=False, server_default="default"),
    )
    op.create_index("ix_document_chunks_tenant_id", "document_chunks", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_document_chunks_tenant_id", table_name="document_chunks")
    op.drop_column("document_chunks", "tenant_id")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_column("documents", "tenant_id")

