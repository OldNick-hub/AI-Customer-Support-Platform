"""Add custom_title to conversations table.

Revision ID: 014_conversation_custom_title
Revises: 013_kb_ingestion_retries
Create Date: 2026-11-14
"""

from alembic import op
import sqlalchemy as sa

revision = "014_conversation_custom_title"
down_revision = "013_kb_ingestion_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("custom_title", sa.String(255), nullable=True),
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                GRANT UPDATE (custom_title) ON conversations TO app_user;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                REVOKE UPDATE (custom_title) ON conversations FROM app_user;
            END IF;
        END $$;
    """)
    op.drop_column("conversations", "custom_title")
