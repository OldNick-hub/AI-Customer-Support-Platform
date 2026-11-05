"""Test SQLAlchemy 2.0 ORM Entity Models and Relationships."""

import uuid
from datetime import date, datetime, time, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Role, User, UserRole
from app.models.customer import Conversation, Customer, Message, Session
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.evidence import (
    EvidenceFile,
    EvidenceVerification,
    ExtractedEvidenceEntity,
)
from app.models.knowledge_index import BenchmarkRun, KnowledgeIndex
from app.models.sentiment import SentimentLog
from app.models.ticket import EscalationAudit, SLACalendar, Ticket, TicketEvent


@pytest.mark.asyncio
async def test_user_and_role_relationship(session: AsyncSession):
    """Tests User and Role creation and many-to-many relationship via UserRole."""
    role = Role(name="AGENT", description="Support Agent", permissions=["tickets:read", "tickets:write"])
    session.add(role)
    await session.flush()

    user = User(
        email="agent_test@enterprise.internal",
        hashed_password="hashed_pass_test",
        full_name="Test Agent",
        department="Hardware",
        skills=["hardware", "displays"],
        max_active_tickets=5,
    )
    session.add(user)
    await session.flush()

    user_role = UserRole(user_id=user.id, role_id=role.id)
    session.add(user_role)
    await session.commit()

    # Query back
    result = await session.execute(select(User).where(User.email == "agent_test@enterprise.internal"))
    fetched_user = result.scalar_one()
    assert fetched_user.full_name == "Test Agent"
    assert fetched_user.skills == ["hardware", "displays"]
    assert len(fetched_user.roles) == 1
    assert fetched_user.roles[0].name == "AGENT"


@pytest.mark.asyncio
async def test_conversation_and_message_relationship(session: AsyncSession):
    """Tests Customer -> Session -> Conversation -> Message -> SentimentLog hierarchy."""
    customer = Customer(
        external_id="CUST-1001",
        email="customer1@example.com",
        customer_tier="VIP",
    )
    session.add(customer)
    await session.flush()

    now = datetime.now(timezone.utc)
    cust_session = Session(
        customer_id=customer.id,
        session_token="sess_test_token_12345",
        current_language="es",
        status="ACTIVE",
        expires_at=now,
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(
        session_id=cust_session.id,
        customer_id=customer.id,
        primary_language="es",
        status="OPEN",
    )
    session.add(conv)
    await session.flush()

    msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="Mi pedido no ha llegado",
        detected_language="es",
        language_confidence=0.98,
    )
    session.add(msg)
    await session.flush()

    s_log = SentimentLog(
        message_id=msg.id,
        sentiment_class="frustrated",
        confidence_score=0.94,
        frustration_index=0.85,
        requires_escalation=False,
    )
    session.add(s_log)
    await session.commit()

    # Query conversation with messages and sentiment log
    result = await session.execute(select(Conversation).where(Conversation.id == conv.id))
    fetched_conv = result.scalar_one()
    assert len(fetched_conv.messages) == 1
    assert fetched_conv.messages[0].content == "Mi pedido no ha llegado"
    assert fetched_conv.messages[0].sentiment_log.sentiment_class == "frustrated"
    assert fetched_conv.messages[0].sentiment_log.frustration_index == 0.85


@pytest.mark.asyncio
async def test_document_version_chunk_hierarchy(session: AsyncSession):
    """Tests Document -> DocumentVersion -> DocumentChunk cascading relationships."""
    doc = Document(
        title="Return Policy 2026",
        document_code="POL-RET-2026",
        category="POLICY",
        region="US",
        access_level="PUBLIC",
    )
    session.add(doc)
    await session.flush()

    version = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        storage_uri="/storage/raw_documents/POL-RET-2026.pdf",
        status="ACTIVE",
        effective_from=date(2026, 1, 1),
        is_current=True,
    )
    session.add(version)
    await session.flush()

    chunk1 = DocumentChunk(
        version_id=version.id,
        chunk_index=0,
        chunk_text="Items can be returned within 30 days of purchase.",
        token_count=10,
        metadata_={"product": "GLOBAL", "region": "US", "access_level": "PUBLIC"},
        faiss_vector_id=1001,
    )
    chunk2 = DocumentChunk(
        version_id=version.id,
        chunk_index=1,
        chunk_text="Refunds will be processed back to the original payment method.",
        token_count=11,
        metadata_={"product": "GLOBAL", "region": "US", "access_level": "PUBLIC"},
        faiss_vector_id=1002,
    )
    session.add_all([chunk1, chunk2])
    await session.commit()

    result = await session.execute(select(Document).where(Document.document_code == "POL-RET-2026"))
    fetched_doc = result.scalar_one()
    assert len(fetched_doc.versions) == 1
    assert len(fetched_doc.versions[0].chunks) == 2
    assert fetched_doc.versions[0].chunks[0].faiss_vector_id == 1001
    assert fetched_doc.versions[0].chunks[1].faiss_vector_id == 1002


@pytest.mark.asyncio
async def test_ticket_and_sla_calendar_relationship(session: AsyncSession):
    """Tests Ticket creation linked to SLACalendar and EscalationAudit."""
    calendar = SLACalendar(
        name="DEFAULT_BUSINESS_HOURS",
        timezone="UTC",
        work_start_time=time(9, 0, 0),
        work_end_time=time(17, 0, 0),
        work_days=[1, 2, 3, 4, 5],
        holidays=["2026-12-25"],
    )
    customer = Customer(external_id="CUST-2002", email="customer2@example.com")
    session.add_all([calendar, customer])
    await session.flush()

    now = datetime.now(timezone.utc)
    ticket = Ticket(
        ticket_number="TCK-202611-001",
        customer_id=customer.id,
        title="Defective Display Port",
        description="Monitor flickers constantly.",
        masked_summary="Monitor display port flickering defect.",
        priority="HIGH",
        priority_score=3.8,
        status="OPEN",
        category="HARDWARE_DEFECT",
        sla_calendar_id=calendar.id,
        sla_target_minutes=120,
        sla_warning_at=now,
        sla_breach_at=now,
    )
    session.add(ticket)
    await session.flush()

    audit = EscalationAudit(
        ticket_id=ticket.id,
        trigger_condition="SLA_BREACH",
        previous_state="OPEN",
        new_state="ESCALATED",
        escalation_reason="SLA exceeded 120 minutes during peak hours.",
    )
    session.add(audit)
    await session.commit()

    result = await session.execute(select(Ticket).where(Ticket.ticket_number == "TCK-202611-001"))
    fetched_ticket = result.scalar_one()
    assert fetched_ticket.sla_calendar.name == "DEFAULT_BUSINESS_HOURS"
    assert len(fetched_ticket.escalation_audits) == 1
    assert fetched_ticket.escalation_audits[0].trigger_condition == "SLA_BREACH"


@pytest.mark.asyncio
async def test_evidence_file_and_verifications(session: AsyncSession):
    """Tests EvidenceFile -> ExtractedEvidenceEntity and EvidenceVerification relationships."""
    now = datetime.now(timezone.utc)
    evidence = EvidenceFile(
        file_name="receipt_nov2026.png",
        mime_type="image/png",
        file_size_bytes=245100,
        storage_uri="/storage/evidence_uploads/staged/receipt_nov2026.png",
        sha256_hash="aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
        status="VERIFIED",
        retention_expires_at=now,
    )
    session.add(evidence)
    await session.flush()

    entity = ExtractedEvidenceEntity(
        evidence_file_id=evidence.id,
        extracted_order_id="ORD-99124",
        extracted_amount=149.99,
        extracted_currency="USD",
        raw_ocr_text="Order ORD-99124 Total: $149.99",
        ocr_confidence=0.98,
        pii_redacted_text="Order ORD-99124 Total: $149.99",
    )
    verification = EvidenceVerification(
        evidence_file_id=evidence.id,
        claim_text="Customer claimed refund for $149.99 on ORD-99124",
        matches_order_id=True,
        matches_amount=True,
        overall_match_status="VERIFIED_MATCH",
    )
    session.add_all([entity, verification])
    await session.commit()

    result = await session.execute(select(EvidenceFile).where(EvidenceFile.id == evidence.id))
    fetched_ev = result.scalar_one()
    assert len(fetched_ev.extracted_entities) == 1
    assert fetched_ev.extracted_entities[0].extracted_order_id == "ORD-99124"
    assert len(fetched_ev.verifications) == 1
    assert fetched_ev.verifications[0].overall_match_status == "VERIFIED_MATCH"

