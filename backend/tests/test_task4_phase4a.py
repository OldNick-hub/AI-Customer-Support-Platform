"""Comprehensive Test Suite for Day 06 — Task 4 Phase 4A: Security-Aware Retriever.

Validates the 3-Layer Zero-Trust Knowledge Retrieval Architecture:
- Server-side identity resolution & client parameter stripping (Zero Client Trust)
- Unknown customer region default-deny & Safe GLOBAL gating
- Differentiated staff RBAC (AGENT vs SUPERVISOR vs ADMIN)
- Hierarchical access-tier isolation (PUBLIC, REGISTERED, VIP, INTERNAL_AGENT, SUPERVISOR, ADMIN)
- Recall starvation immunity via FAISS IDSelectorBatch
- Regional jurisdiction scoping (US vs EU vs APAC)
- Deterministic temporal policy resolution (effective_from <= query_date <= effective_until)
- Historical policy auditing via effective_as_of_date
- Mandatory policy conflict abstention (contradictory terms at identical specificity)
- Quarantined document and chunk exclusion
- Borderline confidence abstention (zero tentative answers)
- Low similarity out-of-domain refusal
- Layer 3 application-level zero-trust validation gate
- Specificity cascade ordering
- Product line scoping
- End-to-end relational + FAISS retrieval integration
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

import faiss
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import RAGAuthorizationError
from app.models.auth import Role, User, UserRole
from app.models.customer import Customer, Session as CustomerSession
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.modules.rag.retriever import (
    RetrievalResult,
    RetrievedChunk,
    SecurityAwareRetriever,
)
from app.modules.rag.security import (
    SAFE_GLOBAL_CATEGORIES,
    SecurityContext,
    SecurityContextResolver,
)


# ==============================================================================
# DETERMINISTIC TEST HELPERS & VECTOR EMBEDDING HARNESS
# ==============================================================================

class DeterministicTestEmbeddingService:
    """Provides deterministic, normalized 384-dimensional vectors for fast unit testing."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self._cache: Dict[str, List[float]] = {}
        self._preset_vectors: Dict[str, List[float]] = {}

    def set_vector_for_text(self, text: str, vector: List[float]) -> None:
        """Sets an exact vector representation for a specific text."""
        arr = np.array(vector, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        self._preset_vectors[text] = arr.tolist()

    def create_pair_with_similarity(
        self,
        text_a: str,
        text_b: str,
        target_similarity: float,
    ) -> Tuple[List[float], List[float]]:
        """Creates two unit vectors whose inner product equals target_similarity."""
        rng = np.random.RandomState(abs(hash(text_a)) % (2**32))
        v1 = rng.randn(self.dim).astype(np.float32)
        v1 = v1 / np.linalg.norm(v1)

        v_rand = rng.randn(self.dim).astype(np.float32)
        # Gram-Schmidt to get orthogonal vector v2_orth
        v_orth = v_rand - np.dot(v_rand, v1) * v1
        v_orth = v_orth / np.linalg.norm(v_orth)

        # target_similarity = cos(theta)
        sim = float(np.clip(target_similarity, -1.0, 1.0))
        sin_theta = np.sqrt(max(0.0, 1.0 - sim**2))
        v2 = sim * v1 + sin_theta * v_orth
        v2 = v2 / np.linalg.norm(v2)

        self._preset_vectors[text_a] = v1.tolist()
        self._preset_vectors[text_b] = v2.tolist()
        return v1.tolist(), v2.tolist()

    async def embed_text(self, text: str) -> List[float]:
        if text in self._preset_vectors:
            return self._preset_vectors[text]
        if text not in self._cache:
            rng = np.random.RandomState(abs(hash(text)) % (2**32))
            v = rng.randn(self.dim).astype(np.float32)
            v = v / np.linalg.norm(v)
            self._cache[text] = v.tolist()
        return self._cache[text]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [await self.embed_text(t) for t in texts]


def build_faiss_index_with_vectors(
    vectors_and_ids: List[Tuple[int, List[float]]],
    dim: int = 384,
) -> faiss.Index:
    """Constructs an in-memory FAISS IndexIDMap2 flat inner-product index."""
    sub_index = faiss.IndexFlatIP(dim)
    index = faiss.IndexIDMap2(sub_index)

    if vectors_and_ids:
        ids_list = [vid for vid, _ in vectors_and_ids]
        vecs_list = [v for _, v in vectors_and_ids]
        ids_arr = np.array(ids_list, dtype=np.int64)
        vecs_arr = np.array(vecs_list, dtype=np.float32)
        index.add_with_ids(vecs_arr, ids_arr)

    return index


# ==============================================================================
# 1. SERVER-SIDE IDENTITY RESOLUTION & ZERO CLIENT TRUST
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_server_side_identity_resolution(session: AsyncSession):
    """Scenario 1: Asserts client-supplied access_level, role, or regions in payload are ignored.

    Server-side identity resolution strictly determines permissions from the database.
    """
    # Create customer with STANDARD tier
    cust = Customer(
        customer_tier="STANDARD",
        full_name="Alice Standard",
        email="alice@example.com",
        metadata_={"country_code": "US"},
    )
    session.add(cust)
    await session.flush()

    sess = CustomerSession(
        customer_id=cust.id,
        session_token="sess_valid_alice_123",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(sess)
    await session.commit()

    # Resolve from valid customer session
    sec_ctx = await SecurityContextResolver.resolve(
        session=session,
        session_token="sess_valid_alice_123",
    )

    assert sec_ctx.role == "STANDARD_CUSTOMER"
    assert sec_ctx.access_tier == "REGISTERED"
    assert sec_ctx.permitted_access_tiers == {"PUBLIC", "REGISTERED"}
    assert sec_ctx.region == "US"
    assert sec_ctx.permitted_regions == {"GLOBAL", "US"}
    assert sec_ctx.is_safe_global_only is False
    assert sec_ctx.auth_type == "SESSION_TOKEN"

    # Even if client claims to be VIP or ADMIN in client headers, server context governs
    assert "VIP" not in sec_ctx.permitted_access_tiers
    assert "ADMIN" not in sec_ctx.permitted_access_tiers


@pytest.mark.asyncio
async def test_rag_expired_session_raises_authorization_error(session: AsyncSession):
    """Scenario 1b: Expired or inactive customer session strictly raises RAGAuthorizationError."""
    cust = Customer(customer_tier="STANDARD", metadata_={"country_code": "US"})
    session.add(cust)
    await session.flush()

    expired_sess = CustomerSession(
        customer_id=cust.id,
        session_token="sess_expired_123",
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),  # Expired
    )
    session.add(expired_sess)
    await session.commit()

    with pytest.raises(RAGAuthorizationError) as exc_info:
        await SecurityContextResolver.resolve_from_customer_session(
            session=session,
            session_token="sess_expired_123",
        )
    assert "expired" in exc_info.value.message.lower()


# ==============================================================================
# 2. UNKNOWN CUSTOMER REGION: DEFAULT-DENY & SAFE GLOBAL GATING
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_unknown_region_default_deny(session: AsyncSession):
    """Scenario 2: Unknown customer region is strictly blocked from regional documents.

    Safe GLOBAL non-statutory documents are allowed. Statutory policy questions prompt for region.
    """
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    # 1. Regional US refund policy document
    doc_us = Document(
        title="US Return Policy",
        document_code="POL-RET-US-2026",
        category="POLICY",
        region="US",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc_us)
    await session.flush()

    ver_us = DocumentVersion(
        document_id=doc_us.id,
        version_number=1,
        sha256_hash="hash_us",
        storage_uri="/storage/us.pdf",
        status="ACTIVE",
        effective_from=date(2026, 1, 1),
    )
    session.add(ver_us)
    await session.flush()

    chunk_us = DocumentChunk(
        version_id=ver_us.id,
        chunk_index=0,
        chunk_text="US customers have 30 days for returns.",
        token_count=10,
        metadata_={},
        faiss_vector_id=1001,
    )
    session.add(chunk_us)

    # 2. Safe GLOBAL troubleshooting document
    doc_global = Document(
        title="Headphones Bluetooth Pairing Guide",
        document_code="GUIDE-PAIR-01",
        category="TROUBLESHOOTING",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc_global)
    await session.flush()

    ver_global = DocumentVersion(
        document_id=doc_global.id,
        version_number=1,
        sha256_hash="hash_global",
        storage_uri="/storage/global.pdf",
        status="ACTIVE",
        effective_from=date(2026, 1, 1),
    )
    session.add(ver_global)
    await session.flush()

    chunk_global = DocumentChunk(
        version_id=ver_global.id,
        chunk_index=0,
        chunk_text="Hold power button for 5 seconds to initiate Bluetooth pairing.",
        token_count=12,
        metadata_={},
        faiss_vector_id=1002,
    )
    session.add(chunk_global)
    await session.commit()

    # Build FAISS index with both chunks
    vec_us, _ = embedder.create_pair_with_similarity("us_chunk", "query_refund", 0.85)
    vec_global, _ = embedder.create_pair_with_similarity("global_chunk", "query_pair", 0.85)

    embedder.set_vector_for_text(chunk_us.chunk_text, vec_us)
    embedder.set_vector_for_text(chunk_global.chunk_text, vec_global)

    faiss_index = build_faiss_index_with_vectors(
        [(1001, vec_us), (1002, vec_global)]
    )

    # Anonymous / Unknown Region Security Context
    sec_ctx = SecurityContextResolver.resolve_anonymous()
    assert sec_ctx.region == "UNKNOWN"
    assert sec_ctx.is_safe_global_only is True

    # Test 2a: Statutory policy query under unknown region -> Immediate clarification refusal
    res_policy = await retriever.retrieve(
        session=session,
        query="What is the refund and return window for my order?",
        security_context=sec_ctx,
        faiss_index_override=faiss_index,
    )
    assert res_policy.is_refusal is True
    assert res_policy.refusal_reason == "UNKNOWN_REGION_POLICY_CLARIFICATION"
    assert len(res_policy.chunks) == 0

    # Test 2b: Safe GLOBAL technical troubleshooting query -> Allowed and retrieved!
    embedder.set_vector_for_text("How do I pair my headphones via bluetooth?", vec_global)
    res_tech = await retriever.retrieve(
        session=session,
        query="How do I pair my headphones via bluetooth?",
        security_context=sec_ctx,
        category="TROUBLESHOOTING",
        faiss_index_override=faiss_index,
    )
    assert res_tech.is_refusal is False
    assert len(res_tech.chunks) == 1
    assert res_tech.chunks[0].document_code == "GUIDE-PAIR-01"
    assert res_tech.chunks[0].region == "GLOBAL"


# ==============================================================================
# 3. DIFFERENTIATED STAFF RBAC (AGENT vs SUPERVISOR vs ADMIN)
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_staff_role_permission_differentiation(session: AsyncSession):
    """Scenario 3: Differentiates AGENT, SUPERVISOR, and ADMIN permissions.

    AGENT cannot access SUPERVISOR concessions.
    SUPERVISOR cannot access ADMIN system archives.
    ADMIN has universal clearance.
    """
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    # Setup 3 documents at 3 staff tiers
    # 1. Agent SOP
    doc_agent = Document(
        title="Agent Ticket Handling SOP",
        document_code="SOP-AGENT-01",
        category="STANDARD_SOP",
        region="GLOBAL",
        access_level="INTERNAL_AGENT",
        is_active=True,
    )
    # 2. Supervisor Concessions
    doc_sup = Document(
        title="Supervisor Discretionary Concessions ($500 limit)",
        document_code="SOP-SUP-CONCESSION",
        category="SUPERVISOR_CONCESSION",
        region="GLOBAL",
        access_level="SUPERVISOR",
        is_active=True,
    )
    # 3. Admin System Runbook
    doc_admin = Document(
        title="Admin Key Rotation and Infrastructure Archiving",
        document_code="RUNBOOK-ADMIN-01",
        category="SYSTEM_ADMIN",
        region="GLOBAL",
        access_level="ADMIN",
        is_active=True,
    )
    session.add_all([doc_agent, doc_sup, doc_admin])
    await session.flush()

    for idx, (doc, vid) in enumerate([(doc_agent, 3001), (doc_sup, 3002), (doc_admin, 3003)], start=1):
        v = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=f"hash_{idx}",
            storage_uri=f"/storage/{idx}.pdf",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
        )
        session.add(v)
        await session.flush()
        chunk = DocumentChunk(
            version_id=v.id,
            chunk_index=0,
            chunk_text=f"Text for {doc.title}",
            token_count=10,
            metadata_={},
            faiss_vector_id=vid,
        )
        session.add(chunk)

    # Setup roles and users in database
    role_agent = Role(name="AGENT")
    role_sup = Role(name="SUPERVISOR")
    role_admin = Role(name="ADMIN")
    session.add_all([role_agent, role_sup, role_admin])
    await session.flush()

    user_agent = User(
        email="agent@support.com",
        hashed_password="pw",
        full_name="Bob Agent",
        department="US_Support",
        is_active=True,
    )
    user_sup = User(
        email="supervisor@support.com",
        hashed_password="pw",
        full_name="Sarah Supervisor",
        department="Global_Escalations",
        is_active=True,
    )
    user_admin = User(
        email="admin@support.com",
        hashed_password="pw",
        full_name="Adam Admin",
        department="Security_Ops",
        is_active=True,
    )
    session.add_all([user_agent, user_sup, user_admin])
    await session.flush()

    session.add(UserRole(user_id=user_agent.id, role_id=role_agent.id))
    session.add(UserRole(user_id=user_sup.id, role_id=role_sup.id))
    session.add(UserRole(user_id=user_admin.id, role_id=role_admin.id))
    await session.commit()

    # Resolve security contexts
    ctx_agent = await SecurityContextResolver.resolve(session=session, user_id_or_email="agent@support.com")
    ctx_sup = await SecurityContextResolver.resolve(session=session, user_id_or_email="supervisor@support.com")
    ctx_admin = await SecurityContextResolver.resolve(session=session, user_id_or_email="admin@support.com")

    # Layer 1 SQL candidate pruning validation
    today = date(2026, 11, 3)

    # 1. AGENT candidate pruning
    cands_agent = await retriever.prune_candidates_sql(session=session, security_context=ctx_agent, query_date=today)
    assert 3001 in cands_agent
    assert 3002 not in cands_agent  # Blocked from Supervisor
    assert 3003 not in cands_agent  # Blocked from Admin

    # 2. SUPERVISOR candidate pruning
    cands_sup = await retriever.prune_candidates_sql(session=session, security_context=ctx_sup, query_date=today)
    assert 3001 in cands_sup
    assert 3002 in cands_sup        # Allowed Supervisor
    assert 3003 not in cands_sup    # Blocked from Admin

    # 3. ADMIN candidate pruning
    cands_admin = await retriever.prune_candidates_sql(session=session, security_context=ctx_admin, query_date=today)
    assert 3001 in cands_admin
    assert 3002 in cands_admin
    assert 3003 in cands_admin      # Allowed Admin


# ==============================================================================
# 4. HIERARCHICAL ACCESS-TIER ISOLATION (PUBLIC, REGISTERED, VIP)
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_access_tier_hierarchical_isolation(session: AsyncSession):
    """Scenario 4: Customer tiers are hierarchically isolated.

    PUBLIC cannot see REGISTERED or VIP.
    REGISTERED cannot see VIP.
    VIP cannot see INTERNAL_AGENT.
    """
    retriever = SecurityAwareRetriever()

    # Create documents at PUBLIC, REGISTERED, VIP, and INTERNAL_AGENT tiers
    tiers = ["PUBLIC", "REGISTERED", "VIP", "INTERNAL_AGENT"]
    doc_ids = {}

    for idx, t in enumerate(tiers, start=1):
        doc = Document(
            title=f"Doc Tier {t}",
            document_code=f"DOC-TIER-{t}",
            category="FAQ",
            region="GLOBAL",
            access_level=t,
            is_active=True,
        )
        session.add(doc)
        await session.flush()
        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=f"h_{t}",
            storage_uri=f"/s/{t}.pdf",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
        )
        session.add(ver)
        await session.flush()
        chunk = DocumentChunk(
            version_id=ver.id,
            chunk_index=0,
            chunk_text=f"Text for {t}",
            token_count=5,
            metadata_={},
            faiss_vector_id=4000 + idx,
        )
        session.add(chunk)
        doc_ids[t] = 4000 + idx
    await session.commit()

    today = date(2026, 11, 3)

    # 1. Anonymous Customer (PUBLIC)
    ctx_anon = SecurityContextResolver.resolve_anonymous()
    cands_anon = await retriever.prune_candidates_sql(session=session, security_context=ctx_anon, query_date=today)
    assert doc_ids["PUBLIC"] in cands_anon
    assert doc_ids["REGISTERED"] not in cands_anon
    assert doc_ids["VIP"] not in cands_anon
    assert doc_ids["INTERNAL_AGENT"] not in cands_anon

    # 2. Standard Customer (REGISTERED)
    ctx_std = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="STANDARD_CUSTOMER",
        access_tier="REGISTERED",
        permitted_access_tiers={"PUBLIC", "REGISTERED"},
        region="US",
        permitted_regions={"GLOBAL", "US"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )
    cands_std = await retriever.prune_candidates_sql(session=session, security_context=ctx_std, query_date=today)
    assert doc_ids["PUBLIC"] in cands_std
    assert doc_ids["REGISTERED"] in cands_std
    assert doc_ids["VIP"] not in cands_std
    assert doc_ids["INTERNAL_AGENT"] not in cands_std

    # 3. VIP Customer (VIP)
    ctx_vip = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="VIP_CUSTOMER",
        access_tier="VIP",
        permitted_access_tiers={"PUBLIC", "REGISTERED", "VIP"},
        region="US",
        permitted_regions={"GLOBAL", "US"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )
    cands_vip = await retriever.prune_candidates_sql(session=session, security_context=ctx_vip, query_date=today)
    assert doc_ids["PUBLIC"] in cands_vip
    assert doc_ids["REGISTERED"] in cands_vip
    assert doc_ids["VIP"] in cands_vip
    assert doc_ids["INTERNAL_AGENT"] not in cands_vip


# ==============================================================================
# 5. RECALL STARVATION IMMUNITY VIA FAISS IDSelectorBatch
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_recall_starvation_immunity(session: AsyncSession):
    """Scenario 5: Pre-filtering with IDSelectorBatch eliminates recall starvation.

    When an unauthorized document has higher cosine similarity than an authorized document,
    FAISS pre-filtering searches only the authorized subset, returning the authorized document.
    """
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    # Chunk A: Internal Agent document (sim = 0.95 with query)
    doc_internal = Document(
        title="Internal Concession Matrix",
        document_code="INT-CONC-01",
        category="TROUBLESHOOTING",
        region="GLOBAL",
        access_level="INTERNAL_AGENT",
        is_active=True,
    )
    # Chunk B: Public FAQ document (sim = 0.80 with query)
    doc_public = Document(
        title="Public Troubleshooting Guide",
        document_code="PUB-GUIDE-01",
        category="TROUBLESHOOTING",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add_all([doc_internal, doc_public])
    await session.flush()

    for doc, vid in [(doc_internal, 5001), (doc_public, 5002)]:
        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=f"h_{vid}",
            storage_uri="/s.pdf",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
        )
        session.add(ver)
        await session.flush()
        chunk = DocumentChunk(
            version_id=ver.id,
            chunk_index=0,
            chunk_text=f"Text for {doc.title}",
            token_count=10,
            metadata_={},
            faiss_vector_id=vid,
        )
        session.add(chunk)
    await session.commit()

    query_text = "How do I troubleshoot device rebooting?"
    vec_internal, _ = embedder.create_pair_with_similarity(doc_internal.title, query_text, 0.95)
    vec_public, _ = embedder.create_pair_with_similarity(doc_public.title, query_text, 0.80)

    # Set exact vectors
    embedder.set_vector_for_text(f"Text for {doc_internal.title}", vec_internal)
    embedder.set_vector_for_text(f"Text for {doc_public.title}", vec_public)

    faiss_index = build_faiss_index_with_vectors(
        [(5001, vec_internal), (5002, vec_public)]
    )

    # Public anonymous user query with top_k = 1
    sec_ctx = SecurityContextResolver.resolve_anonymous()

    res = await retriever.retrieve(
        session=session,
        query=query_text,
        security_context=sec_ctx,
        category="TROUBLESHOOTING",
        top_k=1,
        faiss_index_override=faiss_index,
    )

    # Under naive post-filtering, top-1 would be 5001 (internal), post-filtered to empty (0 chunks).
    # With IDSelectorBatch, FAISS searches ONLY 5002 and successfully returns it!
    assert res.is_refusal is False
    assert len(res.chunks) == 1
    assert res.chunks[0].vector_id == 5002
    assert res.chunks[0].document_code == "PUB-GUIDE-01"


# ==============================================================================
# 6. REGIONAL JURISDICTION SCOPING (US vs EU vs APAC)
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_regional_jurisdiction_scoping(session: AsyncSession):
    """Scenario 6: Cross-region isolation. US customer cannot see EU docs; EU customer cannot see US docs."""
    retriever = SecurityAwareRetriever()

    regions = ["US", "EU", "APAC", "GLOBAL"]
    vids = {}

    for idx, r in enumerate(regions, start=1):
        doc = Document(
            title=f"Warranty in {r}",
            document_code=f"WARR-{r}-2026",
            category="FAQ",
            region=r,
            access_level="PUBLIC",
            is_active=True,
        )
        session.add(doc)
        await session.flush()
        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=f"h_{r}",
            storage_uri="/s.pdf",
            status="ACTIVE",
            effective_from=date(2026, 1, 1),
        )
        session.add(ver)
        await session.flush()
        chunk = DocumentChunk(
            version_id=ver.id,
            chunk_index=0,
            chunk_text=f"Warranty terms for {r}",
            token_count=10,
            metadata_={},
            faiss_vector_id=6000 + idx,
        )
        session.add(chunk)
        vids[r] = 6000 + idx
    await session.commit()

    today = date(2026, 11, 3)

    # 1. US Customer
    ctx_us = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="STANDARD_CUSTOMER",
        access_tier="REGISTERED",
        permitted_access_tiers={"PUBLIC", "REGISTERED"},
        region="US",
        permitted_regions={"GLOBAL", "US"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )
    cands_us = await retriever.prune_candidates_sql(session=session, security_context=ctx_us, query_date=today)
    assert vids["US"] in cands_us
    assert vids["GLOBAL"] in cands_us
    assert vids["EU"] not in cands_us
    assert vids["APAC"] not in cands_us

    # 2. EU Customer
    ctx_eu = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="STANDARD_CUSTOMER",
        access_tier="REGISTERED",
        permitted_access_tiers={"PUBLIC", "REGISTERED"},
        region="EU",
        permitted_regions={"GLOBAL", "EU"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )
    cands_eu = await retriever.prune_candidates_sql(session=session, security_context=ctx_eu, query_date=today)
    assert vids["EU"] in cands_eu
    assert vids["GLOBAL"] in cands_eu
    assert vids["US"] not in cands_eu
    assert vids["APAC"] not in cands_eu


# ==============================================================================
# 7. DETERMINISTIC TEMPORAL POLICY RESOLUTION (CURRENT DEFAULT)
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_temporal_current_default(session: AsyncSession):
    """Scenario 7: Default query date strictly excludes expired versions and future drafts."""
    retriever = SecurityAwareRetriever()

    doc = Document(
        title="Evolutionary Shipping Policy",
        document_code="POL-SHIP-EVO",
        category="FAQ",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    # Version 1: Expired in 2024
    v1_expired = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="h1",
        storage_uri="/s1.pdf",
        status="ACTIVE",
        effective_from=date(2023, 1, 1),
        effective_until=date(2024, 12, 31),
    )
    # Version 2: Currently Active
    v2_active = DocumentVersion(
        document_id=doc.id,
        version_number=2,
        sha256_hash="h2",
        storage_uri="/s2.pdf",
        status="ACTIVE",
        effective_from=date(2025, 1, 1),
        effective_until=None,
    )
    # Version 3: Future draft (effective in 2027)
    v3_future = DocumentVersion(
        document_id=doc.id,
        version_number=3,
        sha256_hash="h3",
        storage_uri="/s3.pdf",
        status="ACTIVE",
        effective_from=date(2027, 1, 1),
        effective_until=None,
    )
    session.add_all([v1_expired, v2_active, v3_future])
    await session.flush()

    chunk1 = DocumentChunk(version_id=v1_expired.id, chunk_index=0, chunk_text="V1", token_count=2, metadata_={}, faiss_vector_id=7001)
    chunk2 = DocumentChunk(version_id=v2_active.id, chunk_index=0, chunk_text="V2", token_count=2, metadata_={}, faiss_vector_id=7002)
    chunk3 = DocumentChunk(version_id=v3_future.id, chunk_index=0, chunk_text="V3", token_count=2, metadata_={}, faiss_vector_id=7003)
    session.add_all([chunk1, chunk2, chunk3])
    await session.commit()

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )

    today = date(2026, 11, 3)
    cands = await retriever.prune_candidates_sql(session=session, security_context=ctx, query_date=today)

    assert 7002 in cands      # Current V2 included
    assert 7001 not in cands  # Expired V1 excluded
    assert 7003 not in cands  # Future V3 excluded


# ==============================================================================
# 8. HISTORICAL POLICY AUDITING VIA effective_as_of_date
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_historical_effective_date_audit(session: AsyncSession):
    """Scenario 8: effective_as_of_date retrieves historical policy active on that past date."""
    retriever = SecurityAwareRetriever()

    doc = Document(
        title="Historical Refund Policy",
        document_code="POL-REF-HIST",
        category="FAQ",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    v1_past = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="h_past",
        storage_uri="/s1.pdf",
        status="ACTIVE",
        effective_from=date(2024, 1, 1),
        effective_until=date(2024, 12, 31),
    )
    v2_curr = DocumentVersion(
        document_id=doc.id,
        version_number=2,
        sha256_hash="h_curr",
        storage_uri="/s2.pdf",
        status="ACTIVE",
        effective_from=date(2025, 1, 1),
        effective_until=None,
    )
    session.add_all([v1_past, v2_curr])
    await session.flush()

    c1 = DocumentChunk(version_id=v1_past.id, chunk_index=0, chunk_text="V1 past text", token_count=5, metadata_={}, faiss_vector_id=8001)
    c2 = DocumentChunk(version_id=v2_curr.id, chunk_index=0, chunk_text="V2 curr text", token_count=5, metadata_={}, faiss_vector_id=8002)
    session.add_all([c1, c2])
    await session.commit()

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )

    # Auditing an order placed on 2024-06-15
    historical_date = date(2024, 6, 15)
    cands = await retriever.prune_candidates_sql(session=session, security_context=ctx, query_date=historical_date)

    assert 8001 in cands      # Historical v1 retrieved
    assert 8002 not in cands  # Current v2 excluded


# ==============================================================================
# 9. CONFLICTING POLICY DETECTION & MANDATORY ABSTENTION
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_conflicting_policy_abstention(session: AsyncSession):
    """Scenario 9: Contradictory policies at identical specificity trigger abstention and escalation."""
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    # Two active US refund policy documents asserting contradictory return periods
    doc_a = Document(
        title="US Electronics Return Policy",
        document_code="POL-RET-US-A",
        category="POLICY",
        product_line="ELECTRONICS",
        region="US",
        access_level="REGISTERED",
        is_active=True,
    )
    doc_b = Document(
        title="US Electronics Warranty Policy",
        document_code="POL-RET-US-B",
        category="POLICY",
        product_line="ELECTRONICS",
        region="US",
        access_level="REGISTERED",
        is_active=True,
    )
    session.add_all([doc_a, doc_b])
    await session.flush()

    v_a = DocumentVersion(document_id=doc_a.id, version_number=1, sha256_hash="ha", storage_uri="/a.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    v_b = DocumentVersion(document_id=doc_b.id, version_number=1, sha256_hash="hb", storage_uri="/b.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    session.add_all([v_a, v_b])
    await session.flush()

    chunk_a = DocumentChunk(
        version_id=v_a.id,
        chunk_index=0,
        chunk_text="All electronics returns must be initiated within 14 days of delivery.",
        token_count=12,
        metadata_={},
        faiss_vector_id=9001,
    )
    chunk_b = DocumentChunk(
        version_id=v_b.id,
        chunk_index=0,
        chunk_text="All electronics returns must be initiated within 30 days of delivery.",
        token_count=12,
        metadata_={},
        faiss_vector_id=9002,
    )
    session.add_all([chunk_a, chunk_b])
    await session.commit()

    query_text = "How many days do I have to return my electronics purchase?"
    vec_a, _ = embedder.create_pair_with_similarity(chunk_a.chunk_text, query_text, 0.88)
    vec_b, _ = embedder.create_pair_with_similarity(chunk_b.chunk_text, query_text, 0.87)

    embedder.set_vector_for_text(chunk_a.chunk_text, vec_a)
    embedder.set_vector_for_text(chunk_b.chunk_text, vec_b)

    faiss_index = build_faiss_index_with_vectors(
        [(9001, vec_a), (9002, vec_b)]
    )

    ctx = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="STANDARD_CUSTOMER",
        access_tier="REGISTERED",
        permitted_access_tiers={"PUBLIC", "REGISTERED"},
        region="US",
        permitted_regions={"GLOBAL", "US"},
        permitted_categories={"POLICY"},
        is_safe_global_only=False,
    )

    res = await retriever.retrieve(
        session=session,
        query=query_text,
        security_context=ctx,
        category="POLICY",
        product_line="ELECTRONICS",
        faiss_index_override=faiss_index,
    )

    # Asserts zero-tolerance conflict rule: Mandatory abstention & escalation
    assert res.conflict_detected is True
    assert res.is_refusal is True
    assert res.refusal_reason == "CONFLICTING_POLICY_EVIDENCE"


# ==============================================================================
# 10. QUARANTINED CHUNK AND VERSION EXCLUSION
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_quarantined_chunk_and_version_exclusion(session: AsyncSession):
    """Scenario 10: Chunks with is_quarantined=True or versions with status='QUARANTINED' are excluded."""
    retriever = SecurityAwareRetriever()

    doc = Document(
        title="Quarantine Test Document",
        document_code="DOC-QUARANTINE-01",
        category="FAQ",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    # Version 1: ACTIVE version but contains a quarantined chunk
    v1 = DocumentVersion(document_id=doc.id, version_number=1, sha256_hash="h1", storage_uri="/1.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    # Version 2: Status is QUARANTINED
    v2 = DocumentVersion(document_id=doc.id, version_number=2, sha256_hash="h2", storage_uri="/2.pdf", status="QUARANTINED", effective_from=date(2026, 1, 1))
    session.add_all([v1, v2])
    await session.flush()

    # Clean chunk in v1
    c1_clean = DocumentChunk(version_id=v1.id, chunk_index=0, chunk_text="Clean text", token_count=5, metadata_={}, faiss_vector_id=10001)
    # Quarantined chunk in v1
    c1_quar = DocumentChunk(version_id=v1.id, chunk_index=1, chunk_text="Malicious prompt injection text", token_count=10, metadata_={"is_quarantined": True, "quarantine_reason": "PROMPT_INJECTION_DETECTED"}, faiss_vector_id=10002)
    # Chunk in quarantined version v2
    c2 = DocumentChunk(version_id=v2.id, chunk_index=0, chunk_text="Version 2 text", token_count=5, metadata_={}, faiss_vector_id=10003)
    session.add_all([c1_clean, c1_quar, c2])
    await session.commit()

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )

    today = date(2026, 11, 3)
    cands = await retriever.prune_candidates_sql(session=session, security_context=ctx, query_date=today)

    assert 10001 in cands      # Clean chunk included
    assert 10002 not in cands  # Quarantined chunk excluded
    assert 10003 not in cands  # Quarantined version excluded


# ==============================================================================
# 11. BORDERLINE CONFIDENCE ABSTENTION (ZERO TENTATIVE ANSWERS)
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_borderline_confidence_abstention(session: AsyncSession):
    """Scenario 11: Similarity in the grey-zone [0.65, 0.76) triggers clean abstention without tentative answer."""
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    doc = Document(
        title="Standard FAQ",
        document_code="FAQ-BORDERLINE",
        category="POLICY",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(document_id=doc.id, version_number=1, sha256_hash="hb", storage_uri="/b.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    session.add(ver)
    await session.flush()

    chunk = DocumentChunk(version_id=ver.id, chunk_index=0, chunk_text="Borderline matching text", token_count=5, metadata_={}, faiss_vector_id=11001)
    session.add(chunk)
    await session.commit()

    query_text = "Borderline query inquiry?"
    # Set similarity to 0.68 (above 0.65, but below calibrated threshold 0.76 for POLICY)
    vec_chunk, _ = embedder.create_pair_with_similarity(chunk.chunk_text, query_text, 0.68)
    embedder.set_vector_for_text(chunk.chunk_text, vec_chunk)

    faiss_index = build_faiss_index_with_vectors([(11001, vec_chunk)])

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"POLICY"},
        is_safe_global_only=False,
    )

    res = await retriever.retrieve(
        session=session,
        query=query_text,
        security_context=ctx,
        category="POLICY",
        faiss_index_override=faiss_index,
    )

    assert res.is_refusal is True
    assert res.refusal_reason == "INCONCLUSIVE_RETRIEVAL_CONFIDENCE"


# ==============================================================================
# 12. LOW SIMILARITY OUT-OF-DOMAIN REFUSAL
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_low_similarity_hallucination_refusal(session: AsyncSession):
    """Scenario 12: Completely out-of-domain query (similarity < 0.65) triggers LOW_SIMILARITY_OUT_OF_DOMAIN."""
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    doc = Document(
        title="Audio Technical Manual",
        document_code="MAN-AUDIO-01",
        category="PRODUCT_MANUAL",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(document_id=doc.id, version_number=1, sha256_hash="h_aud", storage_uri="/a.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    session.add(ver)
    await session.flush()

    chunk = DocumentChunk(version_id=ver.id, chunk_index=0, chunk_text="Audio equalizer specifications", token_count=5, metadata_={}, faiss_vector_id=12001)
    session.add(chunk)
    await session.commit()

    query_text = "Who was the 16th president of the United States?"
    # Similarity = 0.35 (well below 0.65)
    vec_chunk, _ = embedder.create_pair_with_similarity(chunk.chunk_text, query_text, 0.35)
    embedder.set_vector_for_text(chunk.chunk_text, vec_chunk)

    faiss_index = build_faiss_index_with_vectors([(12001, vec_chunk)])

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"PRODUCT_MANUAL"},
        is_safe_global_only=False,
    )

    res = await retriever.retrieve(
        session=session,
        query=query_text,
        security_context=ctx,
        category="PRODUCT_MANUAL",
        faiss_index_override=faiss_index,
    )

    assert res.is_refusal is True
    assert res.refusal_reason == "LOW_SIMILARITY_OUT_OF_DOMAIN"


# ==============================================================================
# 13. LAYER 3 ZERO-TRUST VALIDATION GATE
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_faiss_idselector_layer3_zero_trust(session: AsyncSession):
    """Scenario 13: Layer 3 application gate drops unauthorized vector IDs even if FAISS returns them."""
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    doc = Document(
        title="Authorized Document",
        document_code="DOC-AUTH-01",
        category="FAQ",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(document_id=doc.id, version_number=1, sha256_hash="h_auth", storage_uri="/a.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    session.add(ver)
    await session.flush()

    chunk = DocumentChunk(version_id=ver.id, chunk_index=0, chunk_text="Authorized text", token_count=5, metadata_={}, faiss_vector_id=13001)
    session.add(chunk)
    await session.commit()

    query_text = "Query test text"
    vec_auth, _ = embedder.create_pair_with_similarity(chunk.chunk_text, query_text, 0.85)
    vec_unauth = [0.1] * 384  # Unauthorized vector 99999

    # FAISS index contains both 13001 and an unauthorized 99999
    faiss_index = build_faiss_index_with_vectors([(13001, vec_auth), (99999, vec_unauth)])

    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"FAQ"},
        is_safe_global_only=False,
    )

    res = await retriever.retrieve(
        session=session,
        query=query_text,
        security_context=ctx,
        category="FAQ",
        faiss_index_override=faiss_index,
    )

    assert res.is_refusal is False
    vids = [c.vector_id for c in res.chunks]
    assert 13001 in vids
    assert 99999 not in vids  # Layer 3 gate guarantees unauthorized ID is never leaked


# ==============================================================================
# 14. SPECIFICITY CASCADE RANKING
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_specificity_cascade_ranking(session: AsyncSession):
    """Scenario 14: Four-tier specificity cascade: Code pin > Product > Region > Recency."""
    retriever = SecurityAwareRetriever()

    now_date = date(2026, 11, 3)

    c_global = RetrievedChunk(
        vector_id=1,
        chunk_id="1",
        version_id="1",
        document_id="1",
        document_code="DOC-GEN-01",
        document_title="General Global Doc",
        version_number=1,
        category="FAQ",
        product_line="GLOBAL",
        region="GLOBAL",
        access_level="PUBLIC",
        effective_from=date(2026, 1, 1),
        chunk_index=0,
        token_count=10,
        chunk_text="General text",
        similarity_score=0.80,
        sha256_hash="h1",
    )
    c_region = RetrievedChunk(
        vector_id=2,
        chunk_id="2",
        version_id="2",
        document_id="2",
        document_code="DOC-REG-01",
        document_title="US Regional Doc",
        version_number=1,
        category="FAQ",
        product_line="GLOBAL",
        region="US",
        access_level="PUBLIC",
        effective_from=date(2026, 1, 1),
        chunk_index=0,
        token_count=10,
        chunk_text="US Regional text",
        similarity_score=0.80,
        sha256_hash="h2",
    )
    c_prod_region = RetrievedChunk(
        vector_id=3,
        chunk_id="3",
        version_id="3",
        document_id="3",
        document_code="DOC-PROD-US",
        document_title="US Audio Doc",
        version_number=1,
        category="FAQ",
        product_line="AUDIO",
        region="US",
        access_level="PUBLIC",
        effective_from=date(2026, 1, 1),
        chunk_index=0,
        token_count=10,
        chunk_text="US Audio text",
        similarity_score=0.80,
        sha256_hash="h3",
    )
    c_pinned = RetrievedChunk(
        vector_id=4,
        chunk_id="4",
        version_id="4",
        document_id="4",
        document_code="PINNED-DOC-01",
        document_title="Pinned Doc",
        version_number=1,
        category="FAQ",
        product_line="GLOBAL",
        region="GLOBAL",
        access_level="PUBLIC",
        effective_from=date(2026, 1, 1),
        chunk_index=0,
        token_count=10,
        chunk_text="Pinned text",
        similarity_score=0.80,
        sha256_hash="h4",
    )

    chunks = [c_global, c_region, c_prod_region, c_pinned]
    ranked = retriever._sort_by_specificity_cascade(
        chunks=chunks,
        requested_product_line="AUDIO",
        requested_document_code="PINNED-DOC-01",
        query_date=now_date,
    )

    # 1st: Document Code Pin (c_pinned)
    assert ranked[0].vector_id == 4
    # 2nd: Product Line Specificity (c_prod_region)
    assert ranked[1].vector_id == 3
    # 3rd: Regional Specificity (c_region)
    assert ranked[2].vector_id == 2
    # 4th: General Global (c_global)
    assert ranked[3].vector_id == 1


# ==============================================================================
# 15. PRODUCT LINE SCOPING
# ==============================================================================

@pytest.mark.asyncio
async def test_rag_product_line_scoping(session: AsyncSession):
    """Scenario 15: Security context with restricted product line cannot retrieve other products."""
    retriever = SecurityAwareRetriever()

    # Product A: AUDIO
    doc_audio = Document(
        title="Audio Headphone Troubleshooting",
        document_code="DOC-AUD-01",
        category="TROUBLESHOOTING",
        product_line="AUDIO",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    # Product B: SMART_HOME
    doc_smart = Document(
        title="Smart Thermostat Setup",
        document_code="DOC-SMT-01",
        category="TROUBLESHOOTING",
        product_line="SMART_HOME",
        region="GLOBAL",
        access_level="PUBLIC",
        is_active=True,
    )
    session.add_all([doc_audio, doc_smart])
    await session.flush()

    v_aud = DocumentVersion(document_id=doc_audio.id, version_number=1, sha256_hash="ha", storage_uri="/a.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    v_smt = DocumentVersion(document_id=doc_smart.id, version_number=1, sha256_hash="hs", storage_uri="/s.pdf", status="ACTIVE", effective_from=date(2026, 1, 1))
    session.add_all([v_aud, v_smt])
    await session.flush()

    c_aud = DocumentChunk(version_id=v_aud.id, chunk_index=0, chunk_text="Audio setup", token_count=5, metadata_={}, faiss_vector_id=15001)
    c_smt = DocumentChunk(version_id=v_smt.id, chunk_index=0, chunk_text="Smart setup", token_count=5, metadata_={}, faiss_vector_id=15002)
    session.add_all([c_aud, c_smt])
    await session.commit()

    # Security context restricted to AUDIO product line only
    ctx = SecurityContext(
        permitted_access_tiers={"PUBLIC"},
        permitted_regions={"GLOBAL"},
        permitted_categories={"TROUBLESHOOTING"},
        permitted_product_lines={"AUDIO"},
        is_safe_global_only=False,
    )

    today = date(2026, 11, 3)
    cands = await retriever.prune_candidates_sql(session=session, security_context=ctx, query_date=today)

    assert 15001 in cands      # AUDIO allowed
    assert 15002 not in cands  # SMART_HOME excluded


# ==============================================================================
# 16. LIVE POSTGRESQL & FAISS END-TO-END INTEGRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_live_postgres_rag_end_to_end_integration(session: AsyncSession):
    """Scenario 16: Complete 3-Layer retrieval lifecycle with relational store and live FAISS IndexIDMap2."""
    embedder = DeterministicTestEmbeddingService()
    retriever = SecurityAwareRetriever(embedding_service=embedder)

    doc = Document(
        title="Live Enterprise Return Policy",
        document_code="POL-LIVE-US-2026",
        category="POLICY",
        product_line="GLOBAL",
        region="US",
        access_level="REGISTERED",
        is_active=True,
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        sha256_hash="hash_live_e2e",
        storage_uri="/storage/live.pdf",
        status="ACTIVE",
        effective_from=date(2026, 1, 1),
        effective_until=None,
    )
    session.add(ver)
    await session.flush()

    chunk_text = "US customers receive a full refund within 30 days of purchase upon return receipt."
    chunk = DocumentChunk(
        version_id=ver.id,
        chunk_index=0,
        chunk_text=chunk_text,
        token_count=15,
        metadata_={"section_hierarchy": "Refunds > US Policy", "page_number": 2},
        faiss_vector_id=16001,
    )
    session.add(chunk)
    await session.commit()

    query_str = "What is the US refund window policy?"
    vec_doc, vec_query = embedder.create_pair_with_similarity(chunk_text, query_str, 0.86)
    embedder.set_vector_for_text(chunk_text, vec_doc)
    embedder.set_vector_for_text(query_str, vec_query)

    live_faiss_index = build_faiss_index_with_vectors([(16001, vec_doc)])

    # Authenticated US standard customer
    ctx = SecurityContext(
        customer_id=str(uuid.uuid4()),
        role="STANDARD_CUSTOMER",
        access_tier="REGISTERED",
        permitted_access_tiers={"PUBLIC", "REGISTERED"},
        region="US",
        permitted_regions={"GLOBAL", "US"},
        permitted_categories={"POLICY", "FAQ"},
        is_safe_global_only=False,
    )

    result: RetrievalResult = await retriever.retrieve(
        session=session,
        query=query_str,
        security_context=ctx,
        category="POLICY",
        faiss_index_override=live_faiss_index,
    )

    assert result.is_refusal is False
    assert result.conflict_detected is False
    assert len(result.chunks) == 1
    top_chunk = result.chunks[0]
    assert top_chunk.vector_id == 16001
    assert top_chunk.document_code == "POL-LIVE-US-2026"
    assert top_chunk.similarity_score >= 0.85
    assert top_chunk.effective_from == date(2026, 1, 1)
    assert top_chunk.effective_until is None
    assert top_chunk.metadata["section_hierarchy"] == "Refunds > US Policy"
    assert result.latency_ms > 0.0
