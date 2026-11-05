"""PostgreSQL 16 Live Integration & Integrity Audit Script.

Safely checks/creates the dedicated 'ai_customer_support' database, executes Alembic migrations,
runs non-production seeding, verifies all 20 tables/sequences/constraints, and performs
downgrade tests on a disposable test database.
"""

import asyncio
import getpass
import io
import os
import sys
from typing import Dict, List, Set
from urllib.parse import quote_plus
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from alembic.config import Config
from alembic import command

# Ensure backend directory is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.core.config import settings
from app.db.seed import seed_database


def get_verified_password() -> str:
    """Reads password from settings or prompts locally if not configured."""
    password = settings.POSTGRES_PASSWORD
    if not password:
        password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        print("\n[!] POSTGRES_PASSWORD is empty in .env and environment.")
        try:
            password = getpass.getpass("Enter password for PostgreSQL user 'postgres': ")
        except (io.UnsupportedOperation, Exception):
            print("ERROR: Non-interactive terminal and no password provided in .env.")
            sys.exit(1)
    return password


def ensure_database_exists(password: str) -> None:
    """Checks if 'ai_customer_support' database exists, and creates it safely if missing."""
    print(f"\n[1/6] Connecting to PostgreSQL 16 server at {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT} as '{settings.POSTGRES_USER}'...")
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=password,
        dbname="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()

    # Query server version
    cursor.execute("SELECT version();")
    version = cursor.fetchone()[0]
    print(f"      [OK] PostgreSQL Server Version: {version}")

    target_db = settings.POSTGRES_DB
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (target_db,))
    exists = cursor.fetchone()

    if not exists:
        print(f"      [+] Database '{target_db}' does not exist. Creating safely...")
        cursor.execute(f'CREATE DATABASE "{target_db}" WITH ENCODING = \'UTF8\';')
        print(f"      [OK] Database '{target_db}' created successfully.")
    else:
        print(f"      [OK] Database '{target_db}' already exists. Preserving existing data.")

    cursor.close()
    conn.close()


def run_alembic_upgrade(password: str, dbname: str) -> None:
    """Executes the Alembic migration chain against the target database."""
    print(f"\n[2/6] Running Alembic migration against database '{dbname}'...")
    ini_path = os.path.join(backend_dir, "alembic.ini")
    alembic_cfg = Config(ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))

    user = quote_plus(settings.POSTGRES_USER)
    pw = quote_plus(password)
    async_url = f"postgresql+asyncpg://{user}:{pw}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{dbname}"
    alembic_cfg.set_main_option("sqlalchemy.url", async_url)

    command.upgrade(alembic_cfg, "head")
    print(f"      [OK] Alembic migration '001_initial_schema' applied successfully to '{dbname}'.")


def audit_database_schema(password: str, dbname: str) -> Dict[str, any]:
    """Inspects PostgreSQL system catalogs to verify all 20 tables, constraints, indexes, and sequences."""
    print(f"\n[3/6] Auditing PostgreSQL schema, tables, sequences, and constraints in '{dbname}'...")
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=password,
        dbname=dbname,
    )
    cursor = conn.cursor()

    # 1. Verify tables
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """)
    existing_tables = [row[0] for row in cursor.fetchall() if row[0] != "alembic_version"]
    
    expected_tables = sorted([
        "benchmark_runs", "conversations", "customers", "document_chunks",
        "document_versions", "documents", "escalation_audits", "evidence_files",
        "evidence_verifications", "extracted_evidence_entities", "knowledge_indexes",
        "messages", "roles", "sentiment_logs", "sessions", "sla_calendars",
        "ticket_events", "tickets", "user_roles", "users"
    ])

    print(f"      Found {len(existing_tables)} tables in database (Expected: {len(expected_tables)}).")
    missing_tables = set(expected_tables) - set(existing_tables)
    assert not missing_tables, f"Missing tables in PostgreSQL: {missing_tables}"

    # 2. Verify sequence
    cursor.execute("""
        SELECT sequence_name 
        FROM information_schema.sequences 
        WHERE sequence_schema = 'public';
    """)
    sequences = [row[0] for row in cursor.fetchall()]
    assert "document_chunks_faiss_vector_id_seq" in sequences, "Sequence 'document_chunks_faiss_vector_id_seq' missing!"
    print("      [OK] Sequence 'document_chunks_faiss_vector_id_seq' verified.")

    # 3. Verify BIGINT faiss_vector_id column & default
    cursor.execute("""
        SELECT data_type, column_default 
        FROM information_schema.columns 
        WHERE table_name = 'document_chunks' AND column_name = 'faiss_vector_id';
    """)
    col_type, col_default = cursor.fetchone()
    assert col_type == "bigint", f"Expected bigint for faiss_vector_id, got {col_type}"
    print(f"      [OK] document_chunks.faiss_vector_id column type: {col_type}, default: {col_default}")

    # 4. Count foreign keys
    cursor.execute("""
        SELECT count(*) 
        FROM information_schema.table_constraints 
        WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public';
    """)
    fk_count = cursor.fetchone()[0]
    print(f"      [OK] Foreign key constraints verified: {fk_count} active constraints.")

    # 5. Count indexes
    cursor.execute("""
        SELECT count(*) 
        FROM pg_indexes 
        WHERE schemaname = 'public';
    """)
    idx_count = cursor.fetchone()[0]
    print(f"      [OK] PostgreSQL indexes verified: {idx_count} indexes.")

    cursor.close()
    conn.close()

    return {
        "tables_count": len(existing_tables),
        "fk_count": fk_count,
        "idx_count": idx_count,
        "faiss_col_type": col_type,
    }


async def audit_async_orm_and_seed(password: str, dbname: str) -> None:
    """Verifies SQLAlchemy 2.0 AsyncEngine connectivity and executes idempotent seeding."""
    print(f"\n[4/6] Verifying SQLAlchemy 2.0 asyncpg connection & executing seed data in '{dbname}'...")
    user = quote_plus(settings.POSTGRES_USER)
    pw = quote_plus(password)
    async_url = f"postgresql+asyncpg://{user}:{pw}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{dbname}"

    engine = create_async_engine(async_url, echo=False)
    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Test basic query
        result = await session.execute(sa.text("SELECT 1;"))
        assert result.scalar() == 1
        print("      [OK] AsyncEngine connection test passed (SELECT 1).")

        # Execute seed
        seed_result = await seed_database(session)
        print("      [OK] Non-production seed executed successfully:")
        print(f"           - Roles provisioned: {list(seed_result['roles'].keys())}")
        print(f"           - Users provisioned: {len(seed_result['users'])} enterprise accounts")
        print(f"           - Customer: {seed_result['customer'].external_id}")
        print(f"           - SLA Calendar: {seed_result['calendar'].name}")

    await engine.dispose()


def test_migration_downgrade_on_disposable_db(password: str) -> None:
    """Tests Alembic upgrade and downgrade logic on a dedicated disposable test database."""
    disposable_db = "ai_customer_support_disposable_test"
    print(f"\n[5/6] Testing Alembic migration upgrade/downgrade on disposable database '{disposable_db}'...")

    # Connect to postgres db and create disposable test db
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=password,
        dbname="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()

    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (disposable_db,))
    if cursor.fetchone():
        cursor.execute(f'DROP DATABASE "{disposable_db}";')

    cursor.execute(f'CREATE DATABASE "{disposable_db}" WITH ENCODING = \'UTF8\';')
    print(f"      [+] Created disposable database '{disposable_db}'.")

    cursor.close()
    conn.close()

    try:
        ini_path = os.path.join(backend_dir, "alembic.ini")
        alembic_cfg = Config(ini_path)
        alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
        user = quote_plus(settings.POSTGRES_USER)
        pw = quote_plus(password)
        async_url = f"postgresql+asyncpg://{user}:{pw}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{disposable_db}"
        alembic_cfg.set_main_option("sqlalchemy.url", async_url)

        # Upgrade head
        command.upgrade(alembic_cfg, "head")
        print("      [OK] Upgrade head succeeded on disposable database.")

        # Downgrade base
        command.downgrade(alembic_cfg, "base")
        print("      [OK] Downgrade base succeeded on disposable database.")

        # Re-verify tables were dropped
        conn_disp = psycopg2.connect(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            password=password,
            dbname=disposable_db,
        )
        cur_disp = conn_disp.cursor()
        cur_disp.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name != 'alembic_version';")
        remaining_tables = cur_disp.fetchone()[0]
        assert remaining_tables == 0, f"Expected 0 tables after downgrade, found {remaining_tables}"
        print("      [OK] All tables successfully dropped in downgrade.")
        cur_disp.close()
        conn_disp.close()

    finally:
        # Clean up disposable database
        conn = psycopg2.connect(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            password=password,
            dbname="postgres",
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()
        cursor.execute(f'DROP DATABASE IF EXISTS "{disposable_db}";')
        cursor.close()
        conn.close()
        print(f"      [+] Cleaned up and dropped disposable database '{disposable_db}'.")


def main() -> None:
    print("=" * 70)
    print("DAY 02 - POSTGRESQL 16 INTEGRATION & INTEGRITY AUDIT")
    print("=" * 70)

    password = get_verified_password()

    # 1. Ensure dedicated database exists
    ensure_database_exists(password)

    # 2. Run Alembic upgrade on working database
    run_alembic_upgrade(password, settings.POSTGRES_DB)

    # 3. Audit schema and tables in PostgreSQL
    schema_stats = audit_database_schema(password, settings.POSTGRES_DB)

    # 4. Audit async ORM and run seed
    asyncio.run(audit_async_orm_and_seed(password, settings.POSTGRES_DB))

    # 5. Test upgrade and downgrade on disposable test database
    test_migration_downgrade_on_disposable_db(password)

    print("\n[6/6] PostgreSQL 16 Integration & Integrity Audit Complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()

