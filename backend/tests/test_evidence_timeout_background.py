"""Tests for Automatic Background Evidence Processing Transition after 30s Timeout (Task 2 Criterion 10).

Verifies:
1. Fast synchronous upload completes normally with structured entities.
2. Slow synchronous upload exceeding timeout automatically transitions to STAGED without failing.
3. STAGED evidence is persisted with informative timeout error note.
4. Background processor picks up STAGED evidence and executes extraction to completion.
"""

import asyncio
from datetime import datetime, timezone
import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import ResolvedAuthContext
from app.db.base import Base
from app.models.customer import Conversation, Customer, Session as CustomerSession
from app.models.evidence import EvidenceFile, ExtractedEvidenceEntity
from app.modules.evidence.pipeline import EvidenceProcessingPipeline, PipelineProcessingResult
from app.modules.evidence.schemas import StructuredInvoiceData
from app.modules.evidence.service import EvidenceService
from app.modules.evidence.storage import EvidenceStorageManager
from app.modules.evidence.validator import FileValidator


# Sample minimal valid 1x1 PNG bytes
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def auth_context() -> ResolvedAuthContext:
    cust_id = str(uuid.uuid4())
    return ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        customer_id=cust_id,
        session_token="test-session-token",
        roles=["CUSTOMER"],
    )


@pytest.fixture
def temp_storage_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.mark.asyncio
async def test_evidence_fast_sync_processing(async_db: AsyncSession, auth_context: ResolvedAuthContext, temp_storage_dir: str):
    """Verifies that normal fast uploads complete synchronously with VERIFIED status."""
    cust_uuid = uuid.UUID(auth_context.customer_id)
    cust = Customer(id=cust_uuid, email="test@example.com", full_name="Test Customer")
    conv = Conversation(id=uuid.uuid4(), session_id=uuid.uuid4(), customer_id=cust_uuid)
    async_db.add_all([cust, conv])
    await async_db.commit()

    storage = EvidenceStorageManager(base_dir=temp_storage_dir)
    pipeline = EvidenceProcessingPipeline()

    mock_result = PipelineProcessingResult(
        status="VERIFIED",
        is_malicious=False,
        quarantine_reason=None,
        processing_error=None,
        raw_ocr_text="Invoice #INV-2026-001 Total $120.00",
        ocr_confidence=0.95,
        pii_redacted_text="Invoice #INV-2026-001 Total $120.00",
        structured_entities=StructuredInvoiceData(
            order_id="ORD-1001",
            invoice_date="2026-01-15",
            total_amount=120.0,
            currency="USD",
            product_name="Wireless Keyboard",
        ),
    )
    pipeline.process_document = AsyncMock(return_value=mock_result)

    service = EvidenceService(storage_manager=storage, pipeline=pipeline)

    res = await service.upload_evidence(
        db=async_db,
        auth_context=auth_context,
        file_bytes=VALID_PNG_BYTES,
        client_filename="receipt.png",
        client_content_type="image/png",
        conversation_id=conv.id,
        sync_processing=True,
        sync_timeout_seconds=5.0,
    )

    assert res.status == "VERIFIED"
    assert res.structured_entities is not None
    assert res.structured_entities.order_id == "ORD-1001"
    assert res.structured_entities.total_amount == 120.0


@pytest.mark.asyncio
async def test_evidence_timeout_transitions_to_staged(
    async_db: AsyncSession, auth_context: ResolvedAuthContext, temp_storage_dir: str
):
    """Verifies that an OCR process exceeding timeout transitions cleanly to STAGED."""
    cust_uuid = uuid.UUID(auth_context.customer_id)
    cust = Customer(id=cust_uuid, email="test2@example.com", full_name="Test Customer 2")
    conv = Conversation(id=uuid.uuid4(), session_id=uuid.uuid4(), customer_id=cust_uuid)
    async_db.add_all([cust, conv])
    await async_db.commit()

    storage = EvidenceStorageManager(base_dir=temp_storage_dir)
    pipeline = EvidenceProcessingPipeline()

    # Simulate slow pipeline that takes longer than timeout
    async def slow_process(*args, **kwargs):
        await asyncio.sleep(0.5)
        return PipelineProcessingResult(
            status="VERIFIED",
            is_malicious=False,
            structured_entities=StructuredInvoiceData(),
        )

    pipeline.process_document = slow_process
    service = EvidenceService(storage_manager=storage, pipeline=pipeline)

    # Call with a very tight timeout of 0.05 seconds
    res = await service.upload_evidence(
        db=async_db,
        auth_context=auth_context,
        file_bytes=VALID_PNG_BYTES,
        client_filename="heavy_scan.png",
        client_content_type="image/png",
        conversation_id=conv.id,
        sync_processing=True,
        sync_timeout_seconds=0.05,
    )

    # Must NOT raise exception; must transition to STAGED
    assert res.status == "STAGED"
    assert res.structured_entities is None
    assert "timeout" in (res.processing_error or "").lower()

    # Verify persisted in database as STAGED
    evidence_id = uuid.UUID(res.evidence_id)
    evidence_in_db = await async_db.get(EvidenceFile, evidence_id)
    assert evidence_in_db is not None
    assert evidence_in_db.status == "STAGED"


@pytest.mark.asyncio
async def test_process_staged_evidence_in_background(
    async_db: AsyncSession, auth_context: ResolvedAuthContext, temp_storage_dir: str
):
    """Verifies that process_staged_evidence successfully processes previously staged evidence."""
    cust_uuid = uuid.UUID(auth_context.customer_id)
    cust = Customer(id=cust_uuid, email="test3@example.com", full_name="Test Customer 3")
    conv = Conversation(id=uuid.uuid4(), session_id=uuid.uuid4(), customer_id=cust_uuid)
    async_db.add_all([cust, conv])
    await async_db.commit()

    storage = EvidenceStorageManager(base_dir=temp_storage_dir)
    pipeline = EvidenceProcessingPipeline()

    # Step 1: Upload with simulated timeout
    async def timeout_process(*args, **kwargs):
        await asyncio.sleep(0.2)

    pipeline.process_document = timeout_process
    service = EvidenceService(storage_manager=storage, pipeline=pipeline)

    upload_res = await service.upload_evidence(
        db=async_db,
        auth_context=auth_context,
        file_bytes=VALID_PNG_BYTES,
        client_filename="staged_receipt.png",
        client_content_type="image/png",
        conversation_id=conv.id,
        sync_processing=True,
        sync_timeout_seconds=0.02,
    )
    assert upload_res.status == "STAGED"
    evidence_id = uuid.UUID(upload_res.evidence_id)

    # Step 2: Configure pipeline with successful background extraction result
    mock_success = PipelineProcessingResult(
        status="VERIFIED",
        is_malicious=False,
        quarantine_reason=None,
        processing_error=None,
        raw_ocr_text="Order #9999 Total $45.00",
        ocr_confidence=0.92,
        pii_redacted_text="Order #9999 Total $45.00",
        structured_entities=StructuredInvoiceData(
            order_id="ORD-9999",
            invoice_date="2026-02-01",
            total_amount=45.0,
            currency="USD",
        ),
    )
    pipeline.process_document = AsyncMock(return_value=mock_success)

    # Step 3: Run background processing
    processed = await service.process_staged_evidence(
        db=async_db,
        evidence_id=evidence_id,
    )

    assert processed is not None
    assert processed.status == "VERIFIED"
    assert processed.processed_at is not None

    # Step 4: Verify ExtractedEvidenceEntity was created
    entities_res = await async_db.get(EvidenceFile, evidence_id)
    await async_db.refresh(entities_res, ["extracted_entities"])
    assert len(entities_res.extracted_entities) == 1
    assert entities_res.extracted_entities[0].extracted_order_id == "ORD-9999"
    assert entities_res.extracted_entities[0].extracted_amount == 45.0


@pytest.mark.asyncio
async def test_evidence_masks_raw_ocr_text_before_db_persistence(
    async_db: AsyncSession, auth_context: ResolvedAuthContext, temp_storage_dir: str
):
    """Verifies raw_ocr_text is scrubbed of PANs/emails/phones before ExtractedEvidenceEntity DB write."""
    cust_uuid = uuid.UUID(auth_context.customer_id)
    cust = Customer(id=cust_uuid, email="ocr_mask@example.com", full_name="Mask Test")
    conv = Conversation(id=uuid.uuid4(), session_id=uuid.uuid4(), customer_id=cust_uuid)
    async_db.add_all([cust, conv])
    await async_db.commit()

    storage = EvidenceStorageManager(base_dir=temp_storage_dir)
    pipeline = EvidenceProcessingPipeline()
    pipeline.process_document = AsyncMock(
        return_value=PipelineProcessingResult(
            status="VERIFIED",
            is_malicious=False,
            quarantine_reason=None,
            processing_error=None,
            raw_ocr_text="Order #101 Card: 4111 1111 1111 1111 Email: alice.smith@example.com",
            ocr_confidence=0.95,
            pii_redacted_text="Order #101 Card: ****-****-****-1111 Email: a***h@example.com",
            structured_entities=StructuredInvoiceData(order_id="ORD-101", total_amount=50.0, currency="USD"),
        )
    )
    service = EvidenceService(storage_manager=storage, pipeline=pipeline)
    res = await service.upload_evidence(
        db=async_db,
        auth_context=auth_context,
        file_bytes=VALID_PNG_BYTES,
        client_filename="card_receipt.png",
        client_content_type="image/png",
        conversation_id=conv.id,
        sync_processing=True,
    )
    ev = await async_db.get(EvidenceFile, uuid.UUID(res.evidence_id))
    await async_db.refresh(ev, ["extracted_entities"])
    persisted_raw = ev.extracted_entities[0].raw_ocr_text
    assert "4111 1111 1111 1111" not in persisted_raw
    assert "alice.smith@example.com" not in persisted_raw
    assert "****-****-****-1111" in persisted_raw

