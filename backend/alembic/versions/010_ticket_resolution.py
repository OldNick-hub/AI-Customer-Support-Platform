"""Intelligent Ticket Resolution and Action Orchestration Schema

Revision ID: 010_intelligent_ticket_resolution
Revises: 009_ticket_routing_priority
Create Date: 2026-11-06
"""

from alembic import op
import sqlalchemy as sa

revision = "010_ticket_resolution"
down_revision = "009_ticket_routing_priority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add resolution_status to tickets table
    op.add_column(
        "tickets",
        sa.Column(
            "resolution_status",
            sa.String(50),
            server_default="UNRESOLVED",
            nullable=False,
        ),
    )
    op.create_index("ix_tickets_resolution_status", "tickets", ["resolution_status"])

    # 2. Create ticket_resolutions table
    op.create_table(
        "ticket_resolutions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "ticket_id",
            sa.String(36),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(50), server_default="DRAFT", nullable=False),
        sa.Column("playbook_category", sa.String(100), nullable=False),
        sa.Column("recommended_resolution", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("eligibility_decision", sa.String(50), server_default="ELIGIBLE", nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("supporting_evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("rejection_or_escalation_reason", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(100), nullable=True),
        sa.Column("resolution_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_resolutions_ticket_id", "ticket_resolutions", ["ticket_id"], unique=True)
    op.create_index("ix_ticket_resolutions_status", "ticket_resolutions", ["status"])
    op.create_index("ix_ticket_resolutions_category", "ticket_resolutions", ["playbook_category"])

    # 3. Create resolution_actions table
    op.create_table(
        "resolution_actions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "resolution_id",
            sa.String(36),
            sa.ForeignKey("ticket_resolutions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            sa.String(36),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("action_key", sa.String(150), nullable=False),
        sa.Column("action_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(50), server_default="PROPOSED", nullable=False),
        sa.Column("is_sensitive", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_reason", sa.Text(), nullable=True),
        sa.Column("rejected_by", sa.String(100), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("execution_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("execution_error", sa.Text(), nullable=True),
        sa.Column("execution_result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("resolution_id", "action_key", name="uq_resolution_action_key"),
    )
    op.create_index("ix_resolution_actions_resolution_id", "resolution_actions", ["resolution_id"])
    op.create_index("ix_resolution_actions_ticket_id", "resolution_actions", ["ticket_id"])
    op.create_index("ix_resolution_actions_status", "resolution_actions", ["status"])
    op.create_index("ix_resolution_actions_action_key", "resolution_actions", ["action_key"])

    # 4. Grant explicit permissions to app_user if role exists
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_resolutions TO app_user;
            GRANT SELECT, INSERT, UPDATE, DELETE ON resolution_actions TO app_user;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    # 3. Drop resolution_actions
    op.drop_index("ix_resolution_actions_action_key", table_name="resolution_actions")
    op.drop_index("ix_resolution_actions_status", table_name="resolution_actions")
    op.drop_index("ix_resolution_actions_ticket_id", table_name="resolution_actions")
    op.drop_index("ix_resolution_actions_resolution_id", table_name="resolution_actions")
    op.drop_table("resolution_actions")

    # 2. Drop ticket_resolutions
    op.drop_index("ix_ticket_resolutions_category", table_name="ticket_resolutions")
    op.drop_index("ix_ticket_resolutions_status", table_name="ticket_resolutions")
    op.drop_index("ix_ticket_resolutions_ticket_id", table_name="ticket_resolutions")
    op.drop_table("ticket_resolutions")

    # 1. Drop resolution_status from tickets
    op.drop_index("ix_tickets_resolution_status", table_name="tickets")
    op.drop_column("tickets", "resolution_status")
