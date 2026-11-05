"""PostgreSQL 16 Live Integration & Integrity Verification Tests.

Executes directly against the live PostgreSQL 16 database ('ai_customer_support')
at localhost:5432 to verify native PostgreSQL types, sequences, GIN/JSON queries,
and end-to-end ORM persistence.
"""

from datetime import date, datetime, timezone
import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.encryption import ApplicationLayerEncryption
from app.core.exceptions import KnowledgeLockTimeoutError
from app.core.pii import PIIMaskingEngine
from app.core.security import verify_password
from app.models.auth import Role, User
from app.models.customer import Conversation, Customer, Message, Session
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.ticket import SLACalendar, Ticket
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock


@pytest.fixture
def pg_engine():
    """Creates an async engine connected to the live PostgreSQL 16 database."""
    if not settings.POSTGRES_PASSWORD:
        pytest.skip("POSTGRES_PASSWORD is not set in .env; skipping live PostgreSQL integration tests.")
    
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool, echo=False)
    yield engine
    # Cleanup engine
    # asyncio.run(engine.dispose())


@pytest.fixture
async def pg_session(pg_engine):
    """Yields a transactional session against the live PostgreSQL database."""
    session_factory = async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.mark.asyncio
async def test_pg_live_connection(pg_session: AsyncSession):
    """Verifies async connection to PostgreSQL 16 server."""
    result = await pg_session.execute(text("SELECT version();"))
    version_str = result.scalar()
    assert "PostgreSQL" in version_str
    assert "16." in version_str or "PostgreSQL 16" in version_str


@pytest.mark.asyncio
async def test_pg_all_20_tables_exist(pg_session: AsyncSession):
    """Queries PostgreSQL information_schema to verify all 20 tables exist."""
    query = text("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """)
    result = await pg_session.execute(query)
    tables = [row[0] for row in result.fetchall() if row[0] != "alembic_version"]

    expected_tables = {
        "benchmark_runs", "conversations", "customers", "document_chunks",
        "document_versions", "documents", "escalation_audits", "evidence_files",
        "evidence_verifications", "extracted_evidence_entities", "knowledge_indexes",
        "messages", "roles", "sentiment_logs", "sessions", "sla_calendars",
        "ticket_events", "tickets", "user_roles", "users"
    }

    assert expected_tables.issubset(set(tables)), f"Missing tables in PostgreSQL: {expected_tables - set(tables)}"


@pytest.mark.asyncio
async def test_pg_faiss_vector_id_sequence_auto_increment(pg_session: AsyncSession):
    """Verifies that inserting DocumentChunk automatically pulls nextval from the sequence."""
    doc = Document(
        title="Live PG Sequence Test Doc",
        document_code=f"DOC-SEQ-{datetime.now().timestamp()}",
        category="POLICY",
    )
    pg_session.add(doc)
    await pg_session.flush()

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="/storage/seq_test.pdf",
        effective_from=date(2026, 1, 1),
    )
    pg_session.add(ver)
    await pg_session.flush()

    # Insert chunk without specifying faiss_vector_id
    chunk1 = DocumentChunk(
        version_id=ver.id,
        chunk_index=0,
        chunk_text="First chunk drawn from PostgreSQL sequence.",
        token_count=7,
        metadata_={"access_tier": "PUBLIC"},
    )
    pg_session.add(chunk1)
    await pg_session.flush()
    await pg_session.refresh(chunk1)

    assert chunk1.faiss_vector_id is not None
    assert isinstance(chunk1.faiss_vector_id, int)
    first_id = chunk1.faiss_vector_id

    # Insert second chunk
    chunk2 = DocumentChunk(
        version_id=ver.id,
        chunk_index=1,
        chunk_text="Second chunk drawn from PostgreSQL sequence.",
        token_count=7,
        metadata_={"access_tier": "PUBLIC"},
    )
    pg_session.add(chunk2)
    await pg_session.flush()
    await pg_session.refresh(chunk2)

    assert chunk2.faiss_vector_id == first_id + 1
    await pg_session.rollback()  # Rollback test changes


@pytest.mark.asyncio
async def test_pg_json_metadata_query(pg_session: AsyncSession):
    """Verifies PostgreSQL native JSON query capabilities on chunk metadata."""
    doc = Document(
        title="Live PG JSON Test",
        document_code=f"DOC-JSON-{datetime.now().timestamp()}",
        category="POLICY",
    )
    pg_session.add(doc)
    await pg_session.flush()

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_json",
        storage_uri="/storage/json.pdf",
        effective_from=date(2026, 1, 1),
    )
    pg_session.add(ver)
    await pg_session.flush()

    chunk = DocumentChunk(
        version_id=ver.id,
        chunk_index=0,
        chunk_text="JSON pre-filtering candidate chunk.",
        token_count=5,
        metadata_={"tier": "REGISTERED", "region": "EU"},
        faiss_vector_id=777888999,
    )
    pg_session.add(chunk)
    await pg_session.flush()

    # Query using PostgreSQL JSON operator
    query = text("""
        SELECT chunk_text 
        FROM document_chunks 
        WHERE metadata_->>'tier' = 'REGISTERED' AND metadata_->>'region' = 'EU';
    """)
    result = await pg_session.execute(query)
    rows = result.fetchall()
    assert len(rows) >= 1
    assert "JSON pre-filtering" in rows[0][0]

    await pg_session.rollback()


@pytest.mark.asyncio
async def test_pg_masked_and_encrypted_message_roundtrip(pg_session: AsyncSession):
    """Verifies end-to-end storage and retrieval of masked + AES-GCM encrypted messages on live PostgreSQL."""
    customer = Customer(
        external_id=f"CUST-LIVE-{datetime.now().timestamp()}",
        email="live_test@example.com",
    )
    pg_session.add(customer)
    await pg_session.flush()

    session = Session(
        customer_id=customer.id,
        session_token=f"sess_live_{datetime.now().timestamp()}",
        expires_at=datetime.now(timezone.utc),
    )
    pg_session.add(session)
    await pg_session.flush()

    conv = Conversation(session_id=session.id, customer_id=customer.id)
    pg_session.add(conv)
    await pg_session.flush()

    raw_message = "My credit card is 4111 1111 1111 1111 and I demand a refund."
    masked_message = PIIMaskingEngine.mask_text(raw_message)

    enc_engine = ApplicationLayerEncryption()
    encrypted_bytes = enc_engine.encrypt(raw_message, associated_data=str(conv.id).encode("utf-8"))

    msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content=masked_message,
        encrypted_raw_content=encrypted_bytes,
    )
    pg_session.add(msg)
    await pg_session.flush()

    # Fetch directly using SQL to inspect column types on PostgreSQL
    query = text("SELECT content, encrypted_raw_content FROM messages WHERE id = :msg_id;")
    result = await pg_session.execute(query, {"msg_id": str(msg.id)})
    row = result.fetchone()

    fetched_content, fetched_encrypted = row
    assert "****-****-****-1111" in fetched_content
    assert "4111 1111 1111" not in fetched_content

    # Decrypt bytea payload
    decrypted_raw = enc_engine.decrypt(bytes(fetched_encrypted), associated_data=str(conv.id).encode("utf-8"))
    assert decrypted_raw == raw_message

    await pg_session.rollback()


@pytest.mark.asyncio
async def test_pg_user_skills_crud_and_json_persistence(pg_session: AsyncSession):
    """Regression test: verifies inserting, retrieving, and updating user skills against PostgreSQL."""
    test_email = f"skills_regression_{datetime.now().timestamp()}@enterprise.internal"
    user = User(
        email=test_email,
        hashed_password="test_hashed_password",
        full_name="Skills Regression Agent",
        department="Technical Support",
        skills=["hardware", "networking", "multilingual_es"],
        max_active_tickets=5,
        is_active=True,
        is_on_call=False,
    )
    pg_session.add(user)
    await pg_session.flush()

    # 1. Verify retrieval matches inserted list
    query = select(User).where(User.id == user.id)
    result = await pg_session.execute(query)
    fetched_user = result.scalar_one()
    assert fetched_user.skills == ["hardware", "networking", "multilingual_es"]

    # 2. Verify raw PostgreSQL column data type and JSON content
    raw_query = text("SELECT skills FROM users WHERE id = :user_id;")
    raw_result = await pg_session.execute(raw_query, {"user_id": str(user.id)})
    raw_skills = raw_result.scalar()
    assert isinstance(raw_skills, list)
    assert "hardware" in raw_skills

    # 3. Verify update operation
    fetched_user.skills = ["hardware", "networking", "cloud_infra", "multilingual_de"]
    await pg_session.flush()

    updated_result = await pg_session.execute(query)
    updated_user = updated_result.scalar_one()
    assert updated_user.skills == ["hardware", "networking", "cloud_infra", "multilingual_de"]

    await pg_session.rollback()


@pytest.mark.asyncio
async def test_pg_seed_data_verification(pg_session: AsyncSession):
    """Verifies that seed data is present and valid in live PostgreSQL."""
    # Roles
    roles = (await pg_session.execute(select(Role))).scalars().all()
    if not roles:
        pytest.skip("Seed data has not been committed to live PostgreSQL yet.")
    assert len(roles) >= 4
    role_names = {r.name for r in roles}
    assert {"ADMIN", "SUPERVISOR", "AGENT", "AUDITOR"}.issubset(role_names)

    # Users
    users = (await pg_session.execute(select(User))).scalars().all()
    assert len(users) >= 3
    admin_user = next((u for u in users if u.email == settings.SEED_ADMIN_EMAIL), None)
    assert admin_user is not None
    assert verify_password(settings.SEED_ADMIN_PASSWORD, admin_user.hashed_password) is True

    # SLA Calendar
    calendars = (await pg_session.execute(select(SLACalendar))).scalars().all()
    assert len(calendars) >= 1
    assert any(c.name == "US_COMMERCIAL_HOURS" for c in calendars)


@pytest.mark.asyncio
async def test_pg_advisory_lock_competing_sessions(pg_engine):
    """Verifies PostgreSQL transaction-level advisory locking with genuine competing connections."""
    session_factory = async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)
    lock_id = 987654321

    async with session_factory() as session1, session_factory() as session2:
        lock1 = DatabaseAdvisoryLock(session=session1, lock_id=lock_id, timeout_seconds=2.0)
        lock2 = DatabaseAdvisoryLock(session=session2, lock_id=lock_id, timeout_seconds=0.4)

        # 1. Session 1 acquires lock
        await lock1.acquire()
        assert lock1._acquired is True

        # 2. Session 2 attempts to acquire same lock and MUST time out cleanly
        with pytest.raises(KnowledgeLockTimeoutError) as exc_info:
            await lock2.acquire()
        assert "Timed out after 0.4s" in str(exc_info.value)

        # 3. Session 1 commits/releases, automatically freeing the transaction lock
        await session1.commit()
        await lock1.release()

        # 4. Session 2 can now acquire the released lock without error
        await lock2.acquire()
        assert lock2._acquired is True
        await lock2.release()

        # 5. Cancellation test: A task holding the lock is cancelled
        lock3 = DatabaseAdvisoryLock(session=session1, lock_id=lock_id, timeout_seconds=2.0)
        await lock3.acquire()

        # Rollback session1 simulating task abort/cancellation
        await session1.rollback()
        await lock3.release()

        # Session 2 can acquire immediately following rollback/abort
        lock4 = DatabaseAdvisoryLock(session=session2, lock_id=lock_id, timeout_seconds=1.0)
        await lock4.acquire()
        assert lock4._acquired is True
        await lock4.release()


