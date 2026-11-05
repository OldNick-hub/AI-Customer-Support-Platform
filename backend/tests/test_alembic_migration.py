"""Test Alembic Migration Configuration and Offline DDL Generation."""

import contextlib
import io
import os
import pytest
from alembic.config import Config
from alembic import command


def test_alembic_offline_sql_generation():
    """Verifies that Alembic generates complete, valid transactional DDL for all 20 domain tables."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ini_path = os.path.join(backend_dir, "alembic.ini")

    alembic_cfg = Config(ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))

    # Capture standard output from offline migration
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        command.upgrade(alembic_cfg, "head", sql=True)

    generated_sql = buf.getvalue()

    assert len(generated_sql) > 1000
    assert "BEGIN;" in generated_sql
    assert "COMMIT;" in generated_sql
    assert "CREATE TABLE alembic_version" in generated_sql

    # Assert sequence creation
    assert "CREATE SEQUENCE document_chunks_faiss_vector_id_seq" in generated_sql

    # Assert key tables are present
    expected_tables = [
        "roles",
        "users",
        "user_roles",
        "customers",
        "sessions",
        "conversations",
        "messages",
        "sentiment_logs",
        "documents",
        "document_versions",
        "document_chunks",
        "knowledge_indexes",
        "benchmark_runs",
        "sla_calendars",
        "tickets",
        "ticket_events",
        "escalation_audits",
        "evidence_files",
        "extracted_evidence_entities",
        "evidence_verifications",
    ]

    for table in expected_tables:
        assert f"CREATE TABLE {table}" in generated_sql, f"Missing table DDL: {table}"
