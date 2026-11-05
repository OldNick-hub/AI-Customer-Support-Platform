"""Initial Database Schema Migration.

Revision ID: 001_initial_schema
Revises: None
Create Date: 2026-11-01 10:00:00.000000

Creates all 20 authoritative domain tables, constraints, indexes,
and the document_chunks_faiss_vector_id_seq sequence.
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic
revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. FAISS Vector ID Sequence for PostgreSQL
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.schema.CreateSequence(sa.Sequence("document_chunks_faiss_vector_id_seq", start=1, increment=1)))

    # 1. roles
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(50), unique=True, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
    )
    op.create_index("ix_roles_name", "roles", ["name"])

    # 2. users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("department", sa.String(100), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("max_active_tickets", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_on_call", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_is_active", "users", ["is_active"])
    op.create_index("ix_users_is_on_call", "users", ["is_on_call"])

    # 3. user_roles
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 4. customers
    op.create_table(
        "customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(128), unique=True, nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("customer_tier", sa.String(50), nullable=False, server_default="STANDARD"),
        sa.Column("metadata_", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_customers_external_id", "customers", ["external_id"])
    op.create_index("ix_customers_email", "customers", ["email"])
    op.create_index("ix_customers_tier", "customers", ["customer_tier"])

    # 5. sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_token", sa.String(255), unique=True, nullable=False),
        sa.Column("current_language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("status", sa.String(50), nullable=False, server_default="ACTIVE"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_token", "sessions", ["session_token"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # 6. conversations
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("primary_language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("status", sa.String(50), nullable=False, server_default="OPEN"),
        sa.Column("overall_sentiment_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("is_escalated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_is_escalated", "conversations", ["is_escalated"])
    op.create_index("ix_conversations_last_message", "conversations", ["last_message_at"])

    # 7. messages
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(20), nullable=False),
        sa.Column("sender_id", sa.String(36), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("encrypted_raw_content", sa.LargeBinary(), nullable=True),
        sa.Column("detected_language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("language_confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("idx_messages_conv_created", "messages", ["conversation_id", "created_at"])

    # 8. sentiment_logs
    op.create_table(
        "sentiment_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("sentiment_class", sa.String(30), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("frustration_index", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("urgency_index", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("trajectory_slope", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("requires_escalation", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("escalation_reason", sa.String(255), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sentiment_class", "sentiment_logs", ["sentiment_class"])
    op.create_index("ix_sentiment_escalation", "sentiment_logs", ["requires_escalation"])

    # 9. documents
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("document_code", sa.String(100), unique=True, nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("product_line", sa.String(100), nullable=False, server_default="GLOBAL"),
        sa.Column("region", sa.String(50), nullable=False, server_default="GLOBAL"),
        sa.Column("access_level", sa.String(50), nullable=False, server_default="PUBLIC"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_code", "documents", ["document_code"])
    op.create_index("ix_documents_category", "documents", ["category"])
    op.create_index("ix_documents_region", "documents", ["region"])
    op.create_index("ix_documents_access_level", "documents", ["access_level"])

    # 10. document_versions
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="STAGED"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "version_number", name="uq_doc_version"),
    )
    op.create_index("idx_doc_versions_active", "document_versions", ["document_id", "is_current"])
    op.create_index("idx_doc_versions_temporal", "document_versions", ["effective_from", "effective_until"])

    # 11. document_chunks
    faiss_default = sa.text("nextval('document_chunks_faiss_vector_id_seq')") if bind.dialect.name == "postgresql" else None
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("metadata_", sa.JSON(), nullable=False),
        sa.Column("faiss_vector_id", sa.BigInteger(), unique=True, nullable=False, server_default=faiss_default),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chunks_faiss_id", "document_chunks", ["faiss_vector_id"])
    op.create_index("idx_chunks_version_idx", "document_chunks", ["version_id", "chunk_index"])

    # 12. knowledge_indexes
    op.create_table(
        "knowledge_indexes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("index_version_tag", sa.String(100), unique=True, nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("total_vectors", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="CANDIDATE"),
        sa.Column("benchmark_score", sa.Float(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("watchdog_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canary_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("canary_status", sa.String(50), nullable=False, server_default="IDLE"),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_target_tag", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_knowledge_watchdog", "knowledge_indexes", ["canary_status", "watchdog_expires_at"])

    # 13. benchmark_runs
    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("index_id", sa.String(36), sa.ForeignKey("knowledge_indexes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("benchmark_suite_version", sa.String(50), nullable=False),
        sa.Column("total_queries", sa.Integer(), nullable=False),
        sa.Column("passed_queries", sa.Integer(), nullable=False),
        sa.Column("grounding_score", sa.Float(), nullable=False),
        sa.Column("latency_p95_ms", sa.Float(), nullable=False),
        sa.Column("is_passed", sa.Boolean(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_benchmark_runs_index_id", "benchmark_runs", ["index_id"])

    # 14. sla_calendars
    op.create_table(
        "sla_calendars",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="UTC"),
        sa.Column("work_start_time", sa.Time(), nullable=False),
        sa.Column("work_end_time", sa.Time(), nullable=False),
        sa.Column("work_days", sa.JSON(), nullable=False),
        sa.Column("holidays", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sla_calendars_name", "sla_calendars", ["name"])

    # 15. tickets
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_number", sa.String(32), unique=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("assigned_agent_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("masked_summary", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False, server_default="MEDIUM"),
        sa.Column("priority_score", sa.Float(), nullable=False, server_default="2.5"),
        sa.Column("status", sa.String(50), nullable=False, server_default="OPEN"),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("cluster_group_id", sa.String(36), nullable=True),
        sa.Column("sla_calendar_id", sa.String(36), sa.ForeignKey("sla_calendars.id"), nullable=False),
        sa.Column("sla_target_minutes", sa.Integer(), nullable=False),
        sa.Column("sla_warning_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sla_breach_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sla_warning_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sla_breached", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sla_paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_paused_duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tickets_number", "tickets", ["ticket_number"])
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_customer_id", "tickets", ["customer_id"])
    op.create_index("ix_tickets_assigned_agent", "tickets", ["assigned_agent_id"])
    op.create_index("idx_tickets_assigned_status", "tickets", ["assigned_agent_id", "status"])
    op.create_index("idx_tickets_sla_warning_check", "tickets", ["sla_warning_at", "sla_warning_sent", "status"])
    op.create_index("idx_tickets_sla_breach_check", "tickets", ["sla_breach_at", "sla_breached", "status"])

    # 16. ticket_events
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("event_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])

    # 17. escalation_audits
    op.create_table(
        "escalation_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_condition", sa.String(100), nullable=False),
        sa.Column("previous_state", sa.String(50), nullable=False),
        sa.Column("new_state", sa.String(50), nullable=False),
        sa.Column("escalation_reason", sa.Text(), nullable=False),
        sa.Column("escalation_payload", sa.JSON(), nullable=False),
        sa.Column("escalated_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_escalation_audits_ticket_id", "escalation_audits", ["ticket_id"])
    op.create_index("ix_escalation_audits_trigger", "escalation_audits", ["trigger_condition"])

    # 18. evidence_files
    op.create_table(
        "evidence_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="STAGED"),
        sa.Column("is_malicious", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_files_status", "evidence_files", ["status"])
    op.create_index("ix_evidence_files_sha256", "evidence_files", ["sha256_hash"])
    op.create_index("ix_evidence_files_retention", "evidence_files", ["retention_expires_at"])

    # 19. extracted_evidence_entities
    op.create_table(
        "extracted_evidence_entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evidence_file_id", sa.String(36), sa.ForeignKey("evidence_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("extracted_order_id", sa.String(100), nullable=True),
        sa.Column("extracted_date", sa.Date(), nullable=True),
        sa.Column("extracted_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("extracted_currency", sa.String(10), nullable=True),
        sa.Column("extracted_product_sku", sa.String(100), nullable=True),
        sa.Column("extracted_error_code", sa.String(100), nullable=True),
        sa.Column("raw_ocr_text", sa.Text(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=False),
        sa.Column("pii_redacted_text", sa.Text(), nullable=False),
        sa.Column("extraction_metadata", sa.JSON(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extracted_entities_file_id", "extracted_evidence_entities", ["evidence_file_id"])
    op.create_index("ix_extracted_entities_order_id", "extracted_evidence_entities", ["extracted_order_id"])

    # 20. evidence_verifications
    op.create_table(
        "evidence_verifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evidence_file_id", sa.String(36), sa.ForeignKey("evidence_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("matches_order_id", sa.Boolean(), nullable=True),
        sa.Column("matches_amount", sa.Boolean(), nullable=True),
        sa.Column("matches_product", sa.Boolean(), nullable=True),
        sa.Column("overall_match_status", sa.String(50), nullable=False),
        sa.Column("clarification_prompt", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_verifications_file_id", "evidence_verifications", ["evidence_file_id"])
    op.create_index("ix_evidence_verifications_status", "evidence_verifications", ["overall_match_status"])


def downgrade() -> None:
    # Drop tables in reverse topological order
    op.drop_table("evidence_verifications")
    op.drop_table("extracted_evidence_entities")
    op.drop_table("evidence_files")
    op.drop_table("escalation_audits")
    op.drop_table("ticket_events")
    op.drop_table("tickets")
    op.drop_table("sla_calendars")
    op.drop_table("benchmark_runs")
    op.drop_table("knowledge_indexes")
    op.drop_table("document_chunks")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("sentiment_logs")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("sessions")
    op.drop_table("customers")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("roles")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.schema.DropSequence(sa.Sequence("document_chunks_faiss_vector_id_seq")))

