"""Intelligent Escalation & After-Hours Handling Schema Additions

Revision ID: 003_escalation_after_hours
Revises: 002_chat_idempotency_and_outbox
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "003_escalation_after_hours"
down_revision = "002_chat_idempotency_and_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. conversations: track when negative sentiment was first detected
    op.add_column(
        "conversations",
        sa.Column("negative_detected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_conversations_negative_detected",
        "conversations",
        ["status", "is_escalated", "negative_detected_at"],
    )

    # 2. escalation_audits: allow nullable ticket_id for direct conversation escalations
    op.alter_column(
        "escalation_audits",
        "ticket_id",
        existing_type=sa.String(36),
        nullable=True,
    )

    # 3. escalation_audits: link directly to conversation
    op.add_column(
        "escalation_audits",
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_escalation_audits_conversation_id",
        "escalation_audits",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_escalation_audits_conversation_id", table_name="escalation_audits")
    op.drop_column("escalation_audits", "conversation_id")

    op.alter_column(
        "escalation_audits",
        "ticket_id",
        existing_type=sa.String(36),
        nullable=False,
    )

    op.drop_index("ix_conversations_negative_detected", table_name="conversations")
    op.drop_column("conversations", "negative_detected_at")
