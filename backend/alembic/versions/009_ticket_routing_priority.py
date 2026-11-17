"""Intelligent Ticket Routing and Priority Schema

Revision ID: 009_ticket_routing_priority
Revises: 008_intelligent_ticket_synthesis
Create Date: 2026-11-06
"""

from alembic import op
import sqlalchemy as sa

revision = "009_ticket_routing_priority"
down_revision = "008_intelligent_ticket_synthesis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add assigned_team, priority_explanation, and routing_status to tickets
    op.add_column(
        "tickets",
        sa.Column("assigned_team", sa.String(100), nullable=True),
    )
    op.create_index("ix_tickets_assigned_team", "tickets", ["assigned_team"])

    op.add_column(
        "tickets",
        sa.Column("priority_explanation", sa.Text(), nullable=True),
    )

    op.add_column(
        "tickets",
        sa.Column(
            "routing_status",
            sa.String(50),
            server_default="UNASSIGNED",
            nullable=False,
        ),
    )
    op.create_index("ix_tickets_routing_status", "tickets", ["routing_status"])


def downgrade() -> None:
    op.drop_index("ix_tickets_routing_status", table_name="tickets")
    op.drop_column("tickets", "routing_status")

    op.drop_column("tickets", "priority_explanation")

    op.drop_index("ix_tickets_assigned_team", table_name="tickets")
    op.drop_column("tickets", "assigned_team")

