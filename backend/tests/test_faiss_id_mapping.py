"""Test BIGINT FAISS Vector ID Mapping and Integrity."""

import uuid
from datetime import date
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentChunk, DocumentVersion


@pytest.mark.asyncio
async def test_bigint_faiss_vector_id_range_and_retrieval(session: AsyncSession):
    """Verifies that DocumentChunk supports 64-bit BIGINT vector IDs mapped directly to FAISS."""
    doc = Document(
        title="Vector ID Mapping Doc",
        document_code="DOC-FAISS-001",
        category="POLICY",
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_faiss",
        storage_uri="/storage/faiss.pdf",
        effective_from=date(2026, 1, 1),
    )
    session.add(ver)
    await session.flush()

    # Large 64-bit integer within signed int64 range (up to 9,223,372,036,854,775,807)
    large_int64_id = 9007199254740991  # Standard JavaScript MAX_SAFE_INTEGER and safe BIGINT

    chunk = DocumentChunk(
        version_id=ver.id,
        chunk_index=0,
        chunk_text="Semantic chunk text for vector testing.",
        token_count=7,
        metadata_={"access_tier": "PUBLIC", "region": "US"},
        faiss_vector_id=large_int64_id,
    )
    session.add(chunk)
    await session.commit()

    # Query chunk back strictly by BIGINT faiss_vector_id
    stmt = select(DocumentChunk).where(DocumentChunk.faiss_vector_id == large_int64_id)
    result = await session.execute(stmt)
    retrieved = result.scalar_one()

    assert retrieved.faiss_vector_id == large_int64_id
    assert retrieved.chunk_text == "Semantic chunk text for vector testing."
    assert retrieved.metadata_["access_tier"] == "PUBLIC"


@pytest.mark.asyncio
async def test_bigint_faiss_vector_id_uniqueness(session: AsyncSession):
    """Verifies that faiss_vector_id enforces strict uniqueness across all chunks."""
    doc = Document(
        title="Vector ID Uniqueness Doc",
        document_code="DOC-FAISS-002",
        category="POLICY",
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_faiss_2",
        storage_uri="/storage/faiss_2.pdf",
        effective_from=date(2026, 1, 1),
    )
    session.add(ver)
    await session.flush()

    chunk1 = DocumentChunk(
        version_id=ver.id,
        chunk_index=0,
        chunk_text="Chunk one",
        token_count=2,
        metadata_={},
        faiss_vector_id=420001,
    )
    chunk2 = DocumentChunk(
        version_id=ver.id,
        chunk_index=1,
        chunk_text="Chunk two with conflicting ID",
        token_count=5,
        metadata_={},
        faiss_vector_id=420001,  # Duplicate faiss_vector_id
    )

    session.add(chunk1)
    await session.commit()

    session.add(chunk2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

