"""Test Unique Constraints, Foreign Key Integrity, and Cascade Deletions."""

import uuid
from datetime import date, datetime, time, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Role, User
from app.models.customer import Conversation, Customer, Message, Session
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import KnowledgeIndex
from app.models.sentiment import SentimentLog
from app.models.ticket import SLACalendar, Ticket


@pytest.mark.asyncio
async def test_user_email_unique_constraint(session: AsyncSession):
    """Asserts that duplicate user emails violate the unique constraint."""
    user1 = User(
        email="duplicate@enterprise.internal",
        hashed_password="hash1",
        full_name="User One",
        department="Ops",
    )
    user2 = User(
        email="duplicate@enterprise.internal",
        hashed_password="hash2",
        full_name="User Two",
        department="Sales",
    )
    session.add(user1)
    await session.commit()

    session.add(user2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_document_code_unique_constraint(session: AsyncSession):
    """Asserts that duplicate document codes violate the unique constraint."""
    doc1 = Document(
        title="Shipping Directives",
        document_code="DOC-SHIP-01",
        category="POLICY",
    )
    doc2 = Document(
        title="Shipping Directives Duplicate",
        document_code="DOC-SHIP-01",
        category="POLICY",
    )
    session.add(doc1)
    await session.commit()

    session.add(doc2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_document_version_composite_unique_constraint(session: AsyncSession):
    """Asserts that duplicate version_number for the same document violates uq_doc_version."""
    doc = Document(
        title="Warranty Guidelines",
        document_code="DOC-WAR-01",
        category="POLICY",
    )
    session.add(doc)
    await session.commit()

    v1_a = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_a",
        storage_uri="/storage/doc_v1.pdf",
        effective_from=date(2026, 1, 1),
    )
    v1_b = DocumentVersion(
        document_id=doc.id,
        version_number=1,  # Same version number for same document
        sha256_hash="hash_b",
        storage_uri="/storage/doc_v1_b.pdf",
        effective_from=date(2026, 1, 1),
    )
    session.add(v1_a)
    await session.commit()

    session.add(v1_b)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_ticket_number_unique_constraint(session: AsyncSession):
    """Asserts that duplicate ticket numbers violate unique constraint."""
    calendar = SLACalendar(
        name="CAL-TEST-01",
        work_start_time=time(9, 0),
        work_end_time=time(17, 0),
        work_days=[1, 2, 3, 4, 5],
    )
    customer = Customer(email="test_cust@example.com")
    session.add_all([calendar, customer])
    await session.flush()

    now = datetime.now(timezone.utc)
    t1 = Ticket(
        ticket_number="TCK-DUP-001",
        customer_id=customer.id,
        title="Ticket One",
        description="Desc 1",
        masked_summary="Summary 1",
        category="SUPPORT",
        sla_calendar_id=calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now,
        sla_breach_at=now,
    )
    t2 = Ticket(
        ticket_number="TCK-DUP-001",
        customer_id=customer.id,
        title="Ticket Two",
        description="Desc 2",
        masked_summary="Summary 2",
        category="SUPPORT",
        sla_calendar_id=calendar.id,
        sla_target_minutes=60,
        sla_warning_at=now,
        sla_breach_at=now,
    )
    session.add(t1)
    await session.commit()

    session.add(t2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_cascade_delete_document_to_chunks(session: AsyncSession):
    """Asserts that deleting a Document cascades and deletes its DocumentVersions and Chunks."""
    doc = Document(
        title="Deletable Policy",
        document_code="DOC-DEL-01",
        category="POLICY",
    )
    session.add(doc)
    await session.flush()

    version = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_del",
        storage_uri="/storage/del.pdf",
        effective_from=date(2026, 1, 1),
    )
    session.add(version)
    await session.flush()

    chunk = DocumentChunk(
        version_id=version.id,
        chunk_index=0,
        chunk_text="Chunk will be cascade deleted.",
        token_count=6,
        metadata_={},
        faiss_vector_id=99999,
    )
    session.add(chunk)
    await session.commit()

    # Delete parent document
    await session.delete(doc)
    await session.commit()

    # Verify children are deleted
    ver_res = await session.execute(select(DocumentVersion).where(DocumentVersion.id == version.id))
    assert ver_res.scalar_one_or_none() is None

    chunk_res = await session.execute(select(DocumentChunk).where(DocumentChunk.id == chunk.id))
    assert chunk_res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_cascade_delete_conversation_to_messages(session: AsyncSession):
    """Asserts that deleting a Conversation cascades to all Messages and SentimentLogs."""
    customer = Customer(email="cust_conv@example.com")
    session.add(customer)
    await session.flush()

    cust_session = Session(
        customer_id=customer.id,
        session_token="sess_token_conv_del",
        expires_at=datetime.now(timezone.utc),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="en",
    )
    session.add(conv)
    await session.flush()

    msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="Hello world",
    )
    session.add(msg)
    await session.flush()

    s_log = SentimentLog(
        message_id=msg.id,
        sentiment_class="neutral",
        confidence_score=0.99,
    )
    session.add(s_log)
    await session.commit()

    # Delete conversation
    await session.delete(conv)
    await session.commit()

    # Assert message and sentiment log were cascade deleted
    msg_res = await session.execute(select(Message).where(Message.id == msg.id))
    assert msg_res.scalar_one_or_none() is None

    s_res = await session.execute(select(SentimentLog).where(SentimentLog.id == s_log.id))
    assert s_res.scalar_one_or_none() is None

