"""Tests for Knowledge Base Tenant Isolation & Document Access Control (Task 1 Criterion 14).

Verifies:
1. Document and DocumentChunk schema models enforce tenant_id partition column.
2. Ingestion pipeline extracts tenant_id from frontmatter or defaults safely to 'default'.
3. StructureAwareChunker preserves tenant_id in chunk metadata.
4. SecurityAwareRetriever Layer 1 relational pre-filtering strictly isolates candidate
   vector IDs by tenant_id, preventing any cross-tenant data leakage.
"""

from datetime import date, datetime, timezone
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.modules.knowledge.chunker import StructureAwareChunker
from app.modules.knowledge.sanitizer import IngestionSanitizer
from app.modules.rag.retriever import SecurityAwareRetriever
from app.modules.rag.security import SecurityContext


@pytest.fixture
def chunker() -> StructureAwareChunker:
    return StructureAwareChunker()


@pytest.fixture
def sanitizer() -> IngestionSanitizer:
    return IngestionSanitizer()


def test_document_and_chunk_model_defaults():
    """Verifies that Document and DocumentChunk initialize with default tenant_id."""
    doc = Document(
        title="Test Policy",
        document_code="POL-TEST-001",
        category="POLICY",
        product_line="GLOBAL",
        region="GLOBAL",
        access_level="PUBLIC",
    )
    assert doc.tenant_id == "default"

    chunk = DocumentChunk(
        version_id=uuid.uuid4(),
        chunk_index=0,
        chunk_text="Test chunk text",
        token_count=10,
        metadata_={},
        faiss_vector_id=1,
    )
    assert chunk.tenant_id == "default"


def test_document_and_chunk_explicit_tenant():
    """Verifies that Document and DocumentChunk accept explicit tenant_id."""
    doc = Document(
        tenant_id="tenant_acme",
        title="Acme Policy",
        document_code="POL-ACME-001",
        category="POLICY",
        product_line="GLOBAL",
        region="GLOBAL",
        access_level="PUBLIC",
    )
    assert doc.tenant_id == "tenant_acme"

    chunk = DocumentChunk(
        tenant_id="tenant_acme",
        version_id=uuid.uuid4(),
        chunk_index=0,
        chunk_text="Acme chunk text",
        token_count=10,
        metadata_={"tenant_id": "tenant_acme"},
        faiss_vector_id=2,
    )
    assert chunk.tenant_id == "tenant_acme"


def test_sanitizer_extracts_tenant_from_frontmatter(sanitizer: IngestionSanitizer):
    """Verifies that IngestionSanitizer extracts tenant_id from YAML frontmatter."""
    md_content = """---
tenant_id: tenant_enterprise_99
category: TROUBLESHOOTING
region: US
access_level: INTERNAL_AGENT
---
# POL-ENT-01: Enterprise Troubleshooting Guide
This is an enterprise-specific troubleshooting procedure.
"""
    meta = sanitizer.extract_structural_metadata(md_content, "POL-ENT-01.md")
    assert meta["tenant_id"] == "tenant_enterprise_99"
    assert meta["category"] == "TROUBLESHOOTING"
    assert meta["region"] == "US"


def test_sanitizer_defaults_tenant_when_absent(sanitizer: IngestionSanitizer):
    """Verifies that IngestionSanitizer defaults tenant_id to 'default' when omitted."""
    md_content = """# POL-GEN-01: General Policy
Standard policy without frontmatter.
"""
    meta = sanitizer.extract_structural_metadata(md_content, "POL-GEN-01.md")
    assert meta["tenant_id"] == "default"


def test_chunker_propagates_tenant_id(chunker: StructureAwareChunker):
    """Verifies that StructureAwareChunker propagates tenant_id into chunk metadata."""
    text = "Section 1: Returns. All items can be returned within 30 days."
    chunks = chunker.chunk_document(
        text=text,
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        document_code="POL-RET-01",
        tenant_id="tenant_retail_alpha",
    )
    assert len(chunks) > 0
    assert chunks[0].metadata_["tenant_id"] == "tenant_retail_alpha"


@pytest.mark.asyncio
async def test_security_aware_retriever_layer1_tenant_isolation():
    """Verifies that SecurityAwareRetriever Layer 1 relational pre-filtering strictly isolates candidates by tenant."""
    # Create an in-memory SQLite engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        # Tenant 1: Tenant A
        doc_a = Document(
            id=uuid.uuid4(),
            tenant_id="tenant_alpha",
            title="Tenant Alpha Policy",
            document_code="POL-ALPHA-01",
            category="POLICY",
            product_line="GLOBAL",
            region="GLOBAL",
            access_level="PUBLIC",
            is_active=True,
        )
        ver_a = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc_a.id,
            version_number=1,
            sha256_hash="hash_a",
            storage_uri="/storage/doc_a",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
            effective_until=None,
            is_current=True,
        )
        chunk_a = DocumentChunk(
            id=uuid.uuid4(),
            tenant_id="tenant_alpha",
            version_id=ver_a.id,
            chunk_index=0,
            chunk_text="Alpha specific refund instructions.",
            token_count=15,
            metadata_={"tenant_id": "tenant_alpha"},
            faiss_vector_id=101,
        )

        # Tenant 2: Tenant B
        doc_b = Document(
            id=uuid.uuid4(),
            tenant_id="tenant_beta",
            title="Tenant Beta Policy",
            document_code="POL-BETA-01",
            category="POLICY",
            product_line="GLOBAL",
            region="GLOBAL",
            access_level="PUBLIC",
            is_active=True,
        )
        ver_b = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc_b.id,
            version_number=1,
            sha256_hash="hash_b",
            storage_uri="/storage/doc_b",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
            effective_until=None,
            is_current=True,
        )
        chunk_b = DocumentChunk(
            id=uuid.uuid4(),
            tenant_id="tenant_beta",
            version_id=ver_b.id,
            chunk_index=0,
            chunk_text="Beta specific confidential terms.",
            token_count=15,
            metadata_={"tenant_id": "tenant_beta"},
            faiss_vector_id=202,
        )

        session.add_all([doc_a, ver_a, chunk_a, doc_b, ver_b, chunk_b])
        await session.commit()

        retriever = SecurityAwareRetriever()

        # Query as Tenant Alpha
        sec_ctx_alpha = SecurityContext(
            tenant_id="tenant_alpha",
            role="STANDARD_CUSTOMER",
            access_tier="PUBLIC",
            permitted_access_tiers={"PUBLIC"},
            region="GLOBAL",
            permitted_regions={"GLOBAL"},
            permitted_categories={"POLICY"},
            is_safe_global_only=False,
        )

        candidates_alpha = await retriever.prune_candidates_sql(
            session=session,
            security_context=sec_ctx_alpha,
            query_date=date(2026, 6, 1),
        )

        # Assert only Alpha chunks are visible; Beta chunks are strictly excluded
        assert 101 in candidates_alpha
        assert 202 not in candidates_alpha
        assert candidates_alpha[101]["document_code"] == "POL-ALPHA-01"
        assert candidates_alpha[101]["tenant_id"] == "tenant_alpha"

        # Query as Tenant Beta
        sec_ctx_beta = SecurityContext(
            tenant_id="tenant_beta",
            role="STANDARD_CUSTOMER",
            access_tier="PUBLIC",
            permitted_access_tiers={"PUBLIC"},
            region="GLOBAL",
            permitted_regions={"GLOBAL"},
            permitted_categories={"POLICY"},
            is_safe_global_only=False,
        )

        candidates_beta = await retriever.prune_candidates_sql(
            session=session,
            security_context=sec_ctx_beta,
            query_date=date(2026, 6, 1),
        )

        # Assert only Beta chunks are visible; Alpha chunks are strictly excluded
        assert 202 in candidates_beta
        assert 101 not in candidates_beta
        assert candidates_beta[202]["document_code"] == "POL-BETA-01"
        assert candidates_beta[202]["tenant_id"] == "tenant_beta"

    await engine.dispose()

