"""Zero-Cost Binary Object Storage Schema (BYTEA).

Revision ID: 011_storage_objects
Revises: 010_ticket_resolution
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa

revision = "011_storage_objects"
down_revision = "010_ticket_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_objects",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("key", sa.String(512), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False, server_default="application/octet-stream"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_storage_objects_tenant_key", "storage_objects", ["tenant_id", "key"], unique=True)
    op.create_index("ix_storage_objects_tenant_id", "storage_objects", ["tenant_id"])
    op.create_index("ix_storage_objects_key", "storage_objects", ["key"])
    op.create_index("ix_storage_objects_sha256", "storage_objects", ["sha256_hash"])
    op.create_index("ix_storage_objects_tenant_size", "storage_objects", ["tenant_id", "file_size_bytes"])


def downgrade() -> None:
    op.drop_index("ix_storage_objects_tenant_size", table_name="storage_objects")
    op.drop_index("ix_storage_objects_sha256", table_name="storage_objects")
    op.drop_index("ix_storage_objects_key", table_name="storage_objects")
    op.drop_index("ix_storage_objects_tenant_id", table_name="storage_objects")
    op.drop_index("uq_storage_objects_tenant_key", table_name="storage_objects")
    op.drop_table("storage_objects")

