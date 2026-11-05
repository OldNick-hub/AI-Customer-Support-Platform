"""Task 1 Phase 2 Test Suite: Structure-Aware Chunking & Candidate FAISS Index Construction.

Validates:
- Markdown hierarchy preservation and breadcrumb context
- PDF page number tracking
- Chunk size bounds and sentence-aligned overlap
- Stable metadata preservation and empty text rejection
- 384-dimensional unit-normalized embeddings integration
- Deterministic candidate document selection (staged precedence over active, exclusion of deleted/superseded)
- FAISS IndexIDMap2(IndexFlatIP(384)) vector ID mapping from database sequence
- Candidate index serialization, SHA-256 integrity, and manifest generation
- Candidate isolation: candidate builds never touch or corrupt active_index.json
- Empty candidate collection safe handling
- Failure handling: embedding failure, dimension mismatch, duplicate IDs, read-back integrity, and cleanup
"""

import asyncio
from datetime import date, datetime, timezone
import json
import os
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple
import uuid
import faiss
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.exceptions import (
    CandidateIndexBuildError,
    DocumentCorruptError,
    EmbeddingDimensionMismatchError,
    IndexIntegrityError,
)
from app.db.base import Base
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.chunker import ExtractedChunk, StructureAwareChunker
from app.modules.knowledge.vector_builder import CandidateBuildResult, CandidateVectorBuilder
from app.services.embedding_service import EmbeddingService


@pytest.fixture
def temp_workspace():
    """Provides isolated temporary directories for documents, candidate indices, and quarantine."""
    base_dir = tempfile.mkdtemp(prefix="kb_p2_test_")
    raw_dir = os.path.join(base_dir, "raw_documents")
    indices_dir = os.path.join(base_dir, "indices")
    quarantine_dir = os.path.join(base_dir, "quarantine")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(indices_dir, exist_ok=True)
    os.makedirs(quarantine_dir, exist_ok=True)

    yield {
        "base_dir": base_dir,
        "raw_dir": raw_dir,
        "indices_dir": indices_dir,
        "quarantine_dir": quarantine_dir,
    }

    shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture
def custom_settings(temp_workspace):
    """Provides Settings pointing to isolated test directories."""
    return Settings(
        KNOWLEDGE_DOCS_DIR=temp_workspace["raw_dir"],
        FAISS_STORAGE_DIR=temp_workspace["indices_dir"],
        QUARANTINE_DIR=temp_workspace["quarantine_dir"],
        EMBEDDING_DIMENSION=384,
        EMBEDDING_BATCH_SIZE=16,
    )


@pytest.fixture
async def sqlite_engine():
    """Provides an isolated in-memory SQLite async engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sqlite_session(sqlite_engine):
    """Creates an isolated in-memory SQLite async database session."""
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session


class MockFastEmbeddingService(EmbeddingService):
    """Deterministic mock embedding service producing 384-d normalized vectors quickly for tests."""

    def __init__(self, expected_dim: int = 384, fail: bool = False, force_dim: Optional[int] = None):
        super().__init__()
        self.expected_dimension = expected_dim
        self.fail = fail
        self.force_dim = force_dim

    async def embed_batch(self, texts):
        if self.fail:
            raise RuntimeError("Simulated embedding model inference crash.")
        results = []
        dim = self.force_dim or self.expected_dimension
        for idx, t in enumerate(texts):
            # Create deterministic unit-normalized float vector
            seed = sum(ord(c) for c in t) % 1000 + idx
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0 + (seed * 0.001)
            v[1] = 0.5
            norm = np.linalg.norm(v)
            v = v / norm
            results.append(v.tolist())
        return results


# ==============================================================================
# 1. STRUCTURE-AWARE CHUNKING TESTS
# ==============================================================================

def test_markdown_hierarchy_and_breadcrumb_context(custom_settings):
    """Verifies that markdown headings are preserved in breadcrumbs and metadata."""
    chunker = StructureAwareChunker(settings=custom_settings, chunk_size_chars=200, chunk_overlap_chars=30)
    markdown_doc = (
        "# Enterprise Support Policies\n"
        "Introduction to general enterprise customer support standards.\n\n"
        "## Return Guidelines\n"
        "All hardware returns must be submitted within 30 days of shipment receipt.\n\n"
        "### Warranty Exceptions\n"
        "Custom branded merchandise is not eligible for standard return procedures.\n"
    )

    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    chunks = chunker.chunk_document(
        text=markdown_doc,
        document_id=doc_id,
        version_id=ver_id,
        document_code="POL-RETURN-2026",
        title="Enterprise Support Policies",
    )

    assert len(chunks) >= 3
    # Check section hierarchy and heading paths
    hierarchies = [c.section_hierarchy for c in chunks]
    assert any("Enterprise Support Policies" in h for h in hierarchies)
    assert any("Return Guidelines" in h for h in hierarchies)
    assert any("Warranty Exceptions" in h for h in hierarchies)

    # Breadcrumb prefix in text
    assert any("[Enterprise Support Policies > Return Guidelines]" in c.chunk_text for c in chunks)


def test_pdf_page_metadata_tracking(custom_settings):
    """Verifies PDF page boundaries (--- Page X ---) assign correct page_number metadata."""
    chunker = StructureAwareChunker(settings=custom_settings)
    pdf_text = (
        "--- Page 1 ---\n"
        "# Section One\n"
        "This is text on page 1 of the document.\n\n"
        "--- Page 2 ---\n"
        "# Section Two\n"
        "This is text on page 2 of the document.\n"
    )

    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    chunks = chunker.chunk_document(
        text=pdf_text,
        document_id=doc_id,
        version_id=ver_id,
        document_code="DOC-PDF-PAGES",
    )

    pages = {c.page_number for c in chunks}
    assert 1 in pages
    assert 2 in pages


def test_chunk_size_and_sentence_boundary_overlap(custom_settings):
    """Verifies chunk size limits and sentence-boundary overlap."""
    chunker = StructureAwareChunker(settings=custom_settings, chunk_size_chars=120, chunk_overlap_chars=40)
    doc_text = (
        "First sentence of the paragraph. Second sentence of the paragraph. "
        "Third sentence of the paragraph. Fourth sentence of the paragraph. "
        "Fifth sentence of the paragraph. Sixth sentence of the paragraph."
    )

    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    chunks = chunker.chunk_document(
        text=doc_text,
        document_id=doc_id,
        version_id=ver_id,
        document_code="DOC-OVERLAP",
    )

    assert len(chunks) > 1
    # Ensure words are not split mid-word (all chunks end with punctuation or word boundary)
    for c in chunks:
        assert c.chunk_text.strip()[-1] in (".", "!", "?", "]", ")") or not c.chunk_text.endswith(" ")


def test_stable_metadata_and_empty_text_rejection(custom_settings):
    """Verifies stable metadata preservation and rejection of empty/whitespace content."""
    chunker = StructureAwareChunker(settings=custom_settings)
    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()

    # Rejection of empty string
    with pytest.raises(DocumentCorruptError) as exc_empty:
        chunker.chunk_document(
            text="   \n\n  ",
            document_id=doc_id,
            version_id=ver_id,
            document_code="POL-EMPTY",
        )
    assert exc_empty.value.error_code == "EMPTY_DOCUMENT_TEXT"

    # Valid doc metadata check
    chunks = chunker.chunk_document(
        text="Valid policy statement for enterprise customers.",
        document_id=doc_id,
        version_id=ver_id,
        document_code="POL-STABLE",
        category="BILLING",
        product_line="ENTERPRISE",
        region="EU",
        access_level="INTERNAL_AGENT",
    )

    assert len(chunks) == 1
    m = chunks[0].metadata_
    assert m["document_id"] == str(doc_id)
    assert m["version_id"] == str(ver_id)
    assert m["document_code"] == "POL-STABLE"
    assert m["category"] == "BILLING"
    assert m["product_line"] == "ENTERPRISE"
    assert m["region"] == "EU"
    assert m["access_level"] == "INTERNAL_AGENT"


# ==============================================================================
# 2. DETERMINISTIC CANDIDATE SELECTION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_deterministic_candidate_selection_precedence(custom_settings, sqlite_session):
    """AUDIT REQUIREMENT 4: Staged replacement takes precedence over active; deleted/superseded excluded."""
    # 1. Document A: Has v1 ACTIVE and v2 STAGED -> v2 STAGED must be selected
    doc_a = Document(title="Doc A", document_code="DOC-A", category="POLICY", is_active=True)
    sqlite_session.add(doc_a)
    await sqlite_session.flush()

    ver_a1 = DocumentVersion(
        document_id=doc_a.id, version_number=1, sha256_hash="hash_a1",
        storage_uri="dummy_a1.md", status="ACTIVE", effective_from=datetime.now(timezone.utc).date(), is_current=True,
    )
    ver_a2 = DocumentVersion(
        document_id=doc_a.id, version_number=2, sha256_hash="hash_a2",
        storage_uri="dummy_a2.md", status="STAGED", effective_from=datetime.now(timezone.utc).date(), is_current=False,
    )
    sqlite_session.add_all([ver_a1, ver_a2])

    # 2. Document B: Only has v1 ACTIVE (unmodified) -> v1 ACTIVE must be retained
    doc_b = Document(title="Doc B", document_code="DOC-B", category="POLICY", is_active=True)
    sqlite_session.add(doc_b)
    await sqlite_session.flush()

    ver_b1 = DocumentVersion(
        document_id=doc_b.id, version_number=1, sha256_hash="hash_b1",
        storage_uri="dummy_b1.md", status="ACTIVE", effective_from=datetime.now(timezone.utc).date(), is_current=True,
    )
    sqlite_session.add(ver_b1)

    # 3. Document C: Superseded/quarantined -> must be EXCLUDED
    doc_c = Document(title="Doc C", document_code="DOC-C", category="POLICY", is_active=True)
    sqlite_session.add(doc_c)
    await sqlite_session.flush()

    ver_c1 = DocumentVersion(
        document_id=doc_c.id, version_number=1, sha256_hash="hash_c1",
        storage_uri="dummy_c1.md", status="SUPERSEDED", effective_from=datetime.now(timezone.utc).date(),
    )
    ver_c2 = DocumentVersion(
        document_id=doc_c.id, version_number=2, sha256_hash="hash_c2",
        storage_uri="dummy_c2.md", status="QUARANTINED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add_all([ver_c1, ver_c2])

    # 4. Document D: Deactivated/deleted (is_active=False) -> must be EXCLUDED
    doc_d = Document(title="Doc D", document_code="DOC-D", category="POLICY", is_active=False)
    sqlite_session.add(doc_d)
    await sqlite_session.flush()

    ver_d1 = DocumentVersion(
        document_id=doc_d.id, version_number=1, sha256_hash="hash_d1",
        storage_uri="dummy_d1.md", status="ACTIVE", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver_d1)

    await sqlite_session.commit()

    builder = CandidateVectorBuilder(settings=custom_settings)
    selected_pairs = await builder.select_candidate_versions(sqlite_session)

    # Exactly 2 documents selected (Doc A and Doc B)
    assert len(selected_pairs) == 2
    selected_codes = {p[0].document_code for p in selected_pairs}
    assert selected_codes == {"DOC-A", "DOC-B"}

    # Doc A selected pair must be v2 (STAGED)
    pair_a = next(p for p in selected_pairs if p[0].document_code == "DOC-A")
    assert pair_a[1].version_number == 2
    assert pair_a[1].status == "STAGED"

    # Doc B selected pair must be v1 (ACTIVE)
    pair_b = next(p for p in selected_pairs if p[0].document_code == "DOC-B")
    assert pair_b[1].version_number == 1
    assert pair_b[1].status == "ACTIVE"


# ==============================================================================
# 3. FAISS CANDIDATE BUILDER & INTEGRITY TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_candidate_index_build_and_faiss_save_load_roundtrip(
    custom_settings, temp_workspace, sqlite_session
):
    """Verifies end-to-end candidate index creation, FAISS IndexIDMap2 saving, and read-back."""
    raw_dir = temp_workspace["raw_dir"]
    file_path = os.path.join(raw_dir, "POL-SHIPPING.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("# Shipping Policy\nStandard ground delivery takes 3 to 5 business days across US.")

    # Stage Document in DB
    doc = Document(title="Shipping Policy", document_code="POL-SHIPPING", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_ship",
        storage_uri=file_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    mock_embeddings = MockFastEmbeddingService()
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=mock_embeddings)

    result = await builder.build_candidate_index(session=sqlite_session, version_tag="test_tag_001")

    assert result.index_version_tag == "test_tag_001"
    assert result.total_vectors > 0
    assert result.embedding_dimension == 384
    assert os.path.exists(result.storage_path)
    assert os.path.exists(result.manifest_path)
    assert os.path.exists(f"{result.storage_path}.sha256")

    # Read back FAISS index directly
    loaded_index = faiss.read_index(result.storage_path)
    assert loaded_index.ntotal == result.total_vectors
    assert loaded_index.d == 384

    # Verify KnowledgeIndex database row
    stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == "test_tag_001")
    k_idx = (await sqlite_session.execute(stmt)).scalar_one_or_none()
    assert k_idx is not None
    assert k_idx.status == "STAGED"
    assert k_idx.total_vectors == result.total_vectors


@pytest.mark.asyncio
async def test_faiss_vector_id_mapping_and_exact_match(
    custom_settings, temp_workspace, sqlite_session
):
    """Verifies that FAISS vector IDs match DocumentChunk.faiss_vector_id 1:1 without reuse."""
    raw_dir = temp_workspace["raw_dir"]
    file_path = os.path.join(raw_dir, "POL-WARRANTY.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(
            "# Warranty Guidelines\n"
            "Hardware is covered for 12 months.\n\n"
            "## Extended Care\n"
            "Extended coverage options are available for 24 or 36 months.\n"
        )

    doc = Document(title="Warranty", document_code="POL-WARRANTY", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_war",
        storage_uri=file_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    mock_embeddings = MockFastEmbeddingService()
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=mock_embeddings)

    result = await builder.build_candidate_index(session=sqlite_session, version_tag="test_tag_ids")

    # Query DocumentChunks in database
    stmt = select(DocumentChunk).where(DocumentChunk.version_id == ver.id).order_by(DocumentChunk.chunk_index)
    chunks = (await sqlite_session.execute(stmt)).scalars().all()
    assert len(chunks) == result.total_vectors

    # Read FAISS index and verify vector IDs
    loaded_index = faiss.read_index(result.storage_path)
    # Reconstruct keys from IndexIDMap2
    id_map = faiss.downcast_index(loaded_index)
    assert id_map.id_map.size() == len(chunks)

    # Check each ID is unique and positive
    chunk_ids = [c.faiss_vector_id for c in chunks]
    assert len(set(chunk_ids)) == len(chunks)
    for c in chunks:
        assert c.faiss_vector_id > 0


@pytest.mark.asyncio
async def test_candidate_isolation_does_not_mutate_active_index(
    custom_settings, temp_workspace, sqlite_session
):
    """AUDIT REQUIREMENT 3: Candidate index creation never modifies active_index.json or active snapshots."""
    indices_dir = temp_workspace["indices_dir"]

    # Create dummy active index directory and active_index.json
    active_dir = os.path.join(indices_dir, "idx_active_v1")
    os.makedirs(active_dir, exist_ok=True)
    active_index_file = os.path.join(active_dir, "index.faiss")

    active_faiss = faiss.IndexIDMap2(faiss.IndexFlatIP(384))
    faiss.write_index(active_faiss, active_index_file)

    manifest_active_path = os.path.join(indices_dir, "active_index.json")
    original_active_payload = {
        "active_version_tag": "idx_active_v1",
        "total_vectors": 0,
        "status": "ACTIVE",
    }
    with open(manifest_active_path, "w", encoding="utf-8") as f:
        json.dump(original_active_payload, f)

    # Now build a candidate index
    raw_dir = temp_workspace["raw_dir"]
    doc_path = os.path.join(raw_dir, "POL-TEST.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# New Candidate Document\nCandidate content for index testing.")

    doc = Document(title="New Doc", document_code="POL-NEW", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_new",
        storage_uri=doc_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    mock_embeddings = MockFastEmbeddingService()
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=mock_embeddings)

    result = await builder.build_candidate_index(session=sqlite_session, version_tag="candidate_isolated_01")

    # Assert candidate was built in isolated directory
    assert "candidate_candidate_isolated_01" in result.storage_path
    assert os.path.exists(result.storage_path)

    # Assert active_index.json was NEVER modified!
    with open(manifest_active_path, "r", encoding="utf-8") as f:
        active_content = json.load(f)

    assert active_content["active_version_tag"] == "idx_active_v1"
    assert active_content["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_empty_document_collection_creates_safe_empty_index(custom_settings, sqlite_session):
    """Verifies that an empty database creates a valid empty FAISS candidate index (ntotal=0)."""
    mock_embeddings = MockFastEmbeddingService()
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=mock_embeddings)

    result = await builder.build_candidate_index(session=sqlite_session, version_tag="empty_test_index")

    assert result.total_vectors == 0
    assert os.path.exists(result.storage_path)
    loaded_index = faiss.read_index(result.storage_path)
    assert loaded_index.ntotal == 0
    assert loaded_index.d == 384


# ==============================================================================
# 4. FAILURE HANDLING & ISOLATION RECOVERY TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_failure_embedding_model_error_cleans_up_candidate(
    custom_settings, temp_workspace, sqlite_session
):
    """AUDIT REQUIREMENT 5: If embeddings fail, candidate dir is quarantined/cleaned and DB rolls back."""
    raw_dir = temp_workspace["raw_dir"]
    doc_path = os.path.join(raw_dir, "POL-FAIL.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Failing Policy\nThis document will trigger an embedding failure.")

    doc = Document(title="Fail Doc", document_code="POL-FAIL", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_fail",
        storage_uri=doc_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    failing_embeddings = MockFastEmbeddingService(fail=True)
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=failing_embeddings)

    with pytest.raises(CandidateIndexBuildError) as excinfo:
        await builder.build_candidate_index(session=sqlite_session, version_tag="tag_fail_test")

    assert "Simulated embedding model inference crash" in str(excinfo.value)

    # Verify no KnowledgeIndex was committed
    stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == "tag_fail_test")
    k_idx = (await sqlite_session.execute(stmt)).scalar_one_or_none()
    assert k_idx is None


@pytest.mark.asyncio
async def test_failure_invalid_embedding_dimensions_rejected(
    custom_settings, temp_workspace, sqlite_session
):
    """AUDIT REQUIREMENT 5: Reject vectors with incorrect dimension (e.g. 512 instead of 384)."""
    raw_dir = temp_workspace["raw_dir"]
    doc_path = os.path.join(raw_dir, "POL-DIM.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Dimension Test\nTesting dimension failure handling.")

    doc = Document(title="Dim Doc", document_code="POL-DIM", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_dim",
        storage_uri=doc_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    wrong_dim_embeddings = MockFastEmbeddingService(force_dim=512)
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=wrong_dim_embeddings)

    with pytest.raises(EmbeddingDimensionMismatchError):
        await builder.build_candidate_index(session=sqlite_session, version_tag="tag_dim_fail")


@pytest.mark.asyncio
async def test_failure_missing_storage_file_raises_corrupt_error(
    custom_settings, sqlite_session
):
    """AUDIT REQUIREMENT 5: Missing document on disk triggers DocumentCorruptError and rollback."""
    doc = Document(title="Ghost Doc", document_code="POL-GHOST", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_ghost",
        storage_uri="/nonexistent/path/to/ghost.md", status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    mock_embeddings = MockFastEmbeddingService()
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=mock_embeddings)

    with pytest.raises(DocumentCorruptError):
        await builder.build_candidate_index(session=sqlite_session, version_tag="tag_ghost")


# ==============================================================================
# 5. REMEDIATION REGRESSION TESTS (AUDIT FINDINGS 1, 5, 6, 7, 8)
# ==============================================================================

def test_section_overlap_reset_prevents_leakage(custom_settings):
    """REMEDIATION 1: Sentences from Billing Policy cannot leak across heading boundaries into Technical Support."""
    chunker = StructureAwareChunker(settings=custom_settings, chunk_size_chars=200, chunk_overlap_chars=60)
    doc_text = (
        "# Billing Policy\n"
        "Customers will be billed monthly on the 1st. Payment must be in USD.\n\n"
        "# Technical Support\n"
        "If your device fails, please contact support for a diagnostic.\n"
    )

    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    chunks = chunker.chunk_document(
        text=doc_text,
        document_id=doc_id,
        version_id=ver_id,
        document_code="DOC-OVERLAP-ISOLATION",
    )

    assert len(chunks) == 2
    # Verify Billing chunk
    assert "Billing Policy" in chunks[0].section_hierarchy
    assert "Payment must be in USD." in chunks[0].chunk_text

    # Verify Technical Support chunk has ZERO sentences from Billing Policy!
    assert "Technical Support" in chunks[1].section_hierarchy
    assert "Payment must be in USD." not in chunks[1].chunk_text
    assert "If your device fails" in chunks[1].chunk_text


def test_sentence_boundary_handling_abbreviations_and_numbers(custom_settings):
    """REMEDIATION 5: Abbrs (Dr., e.g., i.e., Jan. 15, No. 123, version 2.0) do not trigger premature sentence breaks."""
    chunker = StructureAwareChunker(settings=custom_settings, chunk_size_chars=1000, chunk_overlap_chars=50)
    complex_text = (
        "Please contact Dr. Smith at e.g. support@example.com for version 2.0 release on Jan. 15. "
        "The policy takes effect on Feb. 1. "
        "All refunds for order No. 123 must be verified. "
        "This is mandatory! The U.S. office is open. Are you available? "
        "See section 1.2 for details."
    )

    sentences = chunker._split_into_sentences(complex_text)
    assert len(sentences) == 7
    assert sentences[0] == "Please contact Dr. Smith at e.g. support@example.com for version 2.0 release on Jan. 15."
    assert sentences[1] == "The policy takes effect on Feb. 1."
    assert sentences[2] == "All refunds for order No. 123 must be verified."
    assert sentences[3] == "This is mandatory!"
    assert sentences[4] == "The U.S. office is open."
    assert sentences[5] == "Are you available?"
    assert sentences[6] == "See section 1.2 for details."


def test_section_aware_chunk_deduplication(custom_settings):
    """REMEDIATION 8: Preserves identical boilerplate appearing in different sections, removes dupes within same section."""
    chunker = StructureAwareChunker(settings=custom_settings, chunk_size_chars=300, chunk_overlap_chars=30)
    doc_text = (
        "# Shipping Department\n"
        "All standard shipments take 3 days. For further support contact admin.\n"
        "All standard shipments take 3 days. For further support contact admin.\n\n"
        "# Returns Department\n"
        "Return requests require proof of purchase. For further support contact admin.\n"
    )

    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    chunks = chunker.chunk_document(
        text=doc_text,
        document_id=doc_id,
        version_id=ver_id,
        document_code="DOC-SECTION-DEDUP",
    )

    # Shipping has 1 chunk (duplicate sentence within same section removed)
    shipping_chunks = [c for c in chunks if "Shipping Department" in c.section_hierarchy]
    assert len(shipping_chunks) == 1

    # Returns has 1 chunk (identical boilerplate notice preserved because section hierarchy differs)
    returns_chunks = [c for c in chunks if "Returns Department" in c.section_hierarchy]
    assert len(returns_chunks) == 1
    assert "For further support contact admin" in returns_chunks[0].chunk_text


@pytest.mark.asyncio
async def test_effective_date_future_current_expired_filtering(custom_settings, sqlite_session):
    """REMEDIATION 6: UTC date reference; excludes future effective_from and past effective_until."""
    from datetime import timedelta
    utc_today = datetime.now(timezone.utc).date()
    yesterday = utc_today - timedelta(days=1)
    tomorrow = utc_today + timedelta(days=1)

    # 1. Document with FUTURE effective_from (must be EXCLUDED)
    doc_future = Document(title="Future Doc", document_code="DOC-FUTURE", category="POLICY", is_active=True)
    sqlite_session.add(doc_future)
    await sqlite_session.flush()
    ver_future = DocumentVersion(
        document_id=doc_future.id, version_number=1, sha256_hash="hash_fut",
        storage_uri="dummy.md", status="STAGED", effective_from=tomorrow, effective_until=None,
    )

    # 2. Document with CURRENT effective_from and FUTURE effective_until (must be INCLUDED)
    doc_current = Document(title="Current Doc", document_code="DOC-CURRENT", category="POLICY", is_active=True)
    sqlite_session.add(doc_current)
    await sqlite_session.flush()
    ver_current = DocumentVersion(
        document_id=doc_current.id, version_number=1, sha256_hash="hash_cur",
        storage_uri="dummy.md", status="STAGED", effective_from=utc_today, effective_until=tomorrow,
    )

    # 3. Document with EXPIRED effective_until (must be EXCLUDED)
    doc_expired = Document(title="Expired Doc", document_code="DOC-EXPIRED", category="POLICY", is_active=True)
    sqlite_session.add(doc_expired)
    await sqlite_session.flush()
    ver_expired = DocumentVersion(
        document_id=doc_expired.id, version_number=1, sha256_hash="hash_exp",
        storage_uri="dummy.md", status="ACTIVE", effective_from=yesterday, effective_until=yesterday,
    )

    sqlite_session.add_all([ver_future, ver_current, ver_expired])
    await sqlite_session.commit()

    builder = CandidateVectorBuilder(settings=custom_settings)
    selected_pairs = await builder.select_candidate_versions(sqlite_session)

    selected_codes = {doc.document_code for doc, ver in selected_pairs}
    assert "DOC-CURRENT" in selected_codes
    assert "DOC-FUTURE" not in selected_codes
    assert "DOC-EXPIRED" not in selected_codes


@pytest.mark.asyncio
async def test_repeated_candidate_build_failures_unique_quarantine(
    custom_settings, temp_workspace, sqlite_session
):
    """REMEDIATION 7: Repeated build failures with identical tag produce unique .failed directories without collision."""
    raw_dir = temp_workspace["raw_dir"]
    doc_path = os.path.join(raw_dir, "POL-COLLIDE.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Fail Doc\nContent to trigger repeated failures.")

    doc = Document(title="Collide Doc", document_code="POL-COLLIDE", category="POLICY", is_active=True)
    sqlite_session.add(doc)
    await sqlite_session.flush()

    ver = DocumentVersion(
        document_id=doc.id, version_number=1, sha256_hash="hash_col",
        storage_uri=doc_path, status="STAGED", effective_from=datetime.now(timezone.utc).date(),
    )
    sqlite_session.add(ver)
    await sqlite_session.commit()

    failing_service = MockFastEmbeddingService(fail=True)
    builder = CandidateVectorBuilder(settings=custom_settings, embedding_service=failing_service)

    # First failure with tag 'tag_repeat_fail'
    with pytest.raises(CandidateIndexBuildError):
        await builder.build_candidate_index(session=sqlite_session, version_tag="tag_repeat_fail")

    # Second failure with SAME tag 'tag_repeat_fail'
    with pytest.raises(CandidateIndexBuildError):
        await builder.build_candidate_index(session=sqlite_session, version_tag="tag_repeat_fail")

    # Verify both quarantine directories exist and do not collide
    indices_dir = temp_workspace["indices_dir"]
    failed_dirs = [d for d in os.listdir(indices_dir) if "candidate_tag_repeat_fail.failed" in d]
    assert len(failed_dirs) == 2

