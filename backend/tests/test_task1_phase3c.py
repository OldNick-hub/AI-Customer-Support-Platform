"""Day 05 — Task 1 Phase 3C: Atomic Candidate Index Promotion Tests.

Verifies:
1. Valid candidate promotion with atomic manifest swap and DB status transition.
2. Failed benchmark gate rejection.
3. Inconclusive benchmark evaluation rejection.
4. Cryptographic checksum mismatch rejection.
5. Corrupted FAISS vector index rejection.
6. Missing candidate directory rejection.
7. Maintenance window constraint enforcement (blocks outside window).
8. Unauthorized user rejection (RBAC).
9. Authorized emergency administrative override with audit trail.
10. Rejection of emergency override lacking descriptive reason.
11. Preservation of previous index files and manifest pointer history.
12. Exactly one authoritative ACTIVE index invariant in database.
13. Graceful degradation when Redis is unavailable.
14. Crash recovery: advance DB when manifest was swapped before DB commit.
15. Crash recovery: revert DB when crash occurred before manifest swap.
16. Crash recovery: reconstruct missing manifest from authoritative DB row.
"""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

import faiss
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.exceptions import (
    BenchmarkGateFailedError,
    IndexIntegrityError,
    IndexPromotionError,
    KnowledgeLockTimeoutError,
    MaintenanceWindowError,
    UnauthorizedPromotionError,
)
from app.db.base import Base
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import BenchmarkRun, KnowledgeIndex
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock
from app.modules.knowledge.maintenance import (
    MaintenanceSchedule,
    MaintenanceWindowService,
    TimeRange,
)
from app.modules.knowledge.promoter import (
    IndexPromoter,
    PromotionResult,
    ReconciliationResult,
)
from app.modules.knowledge.reader import ActiveIndexReader
from app.services.redis_service import RedisService


# =====================================================================
# Fixtures & Test Helpers
# =====================================================================

@pytest.fixture
async def sqlite_engine():
    """Provides an isolated in-memory SQLite async engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def temp_storage_dir(tmp_path):
    """Provides an isolated indices directory."""
    indices_dir = tmp_path / "indices"
    indices_dir.mkdir(parents=True, exist_ok=True)
    return str(indices_dir)


@pytest.fixture
def permissive_maintenance_service():
    """Provides a maintenance service configured to permit promotion at any time."""
    schedule = MaintenanceSchedule(
        enabled=False,  # Disabled enforcement for general promotion tests
        timezone="UTC",
    )
    return MaintenanceWindowService(schedule=schedule)


@pytest.fixture
def mock_redis_service():
    """Mock Redis service that records publishes and does not connect to real server."""
    class MockRedis(RedisService):
        def __init__(self):
            super().__init__()
            self.published_events: List[Tuple[str, str]] = []
            self.cache: Dict[str, Any] = {}

        async def publish(self, channel: str, message: str, raise_on_error: bool = False) -> int:
            self.published_events.append((channel, message))
            return 1

        async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None, raise_on_error: bool = False) -> bool:
            self.cache[key] = (value, ttl_seconds)
            return True

    return MockRedis()


def create_mock_candidate(
    storage_dir: str,
    version_tag: str,
    total_vectors: int = 10,
    corrupt_index: bool = False,
    tamper_hash: bool = False,
    included_codes: Optional[List[str]] = None,
) -> Tuple[str, str, str]:
    """Helper creating a candidate directory, valid FAISS index, and manifest.json."""
    cand_dir = os.path.join(storage_dir, version_tag)
    os.makedirs(cand_dir, exist_ok=True)

    index_path = os.path.join(cand_dir, "index.faiss")
    if corrupt_index:
        with open(index_path, "wb") as f:
            f.write(b"NOT_A_VALID_FAISS_INDEX_BINARY_HEADER")
    else:
        flat_index = faiss.IndexFlatIP(384)
        id_map = faiss.IndexIDMap2(flat_index)
        if total_vectors > 0:
            np.random.seed(42)
            vectors = np.random.randn(total_vectors, 384).astype(np.float32)
            faiss.normalize_L2(vectors)
            ids = np.arange(100, 100 + total_vectors, dtype=np.int64)
            id_map.add_with_ids(vectors, ids)
        faiss.write_index(id_map, index_path)

    # Compute hash
    sha256 = hashlib.sha256()
    with open(index_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    actual_hash = sha256.hexdigest()

    manifest_hash = "0000000000000000000000000000000000000000000000000000000000000000" if tamper_hash else actual_hash

    manifest_payload = {
        "index_version_tag": version_tag,
        "storage_path": os.path.abspath(index_path),
        "total_vectors": total_vectors,
        "embedding_dimension": 384,
        "sha256": manifest_hash,
        "status": "STAGED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "included_document_codes": included_codes or ["POL-RETURN-US-2026"],
    }

    manifest_path = os.path.join(cand_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_payload, f, indent=2)

    return cand_dir, index_path, manifest_path


# =====================================================================
# Tests: Valid Candidate Promotion
# =====================================================================

@pytest.mark.asyncio
async def test_valid_candidate_promotion(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Candidate with passing benchmark and valid FAISS snapshot promotes atomically."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_cand_pass_v1"
    cand_dir, index_path, manifest_path = create_mock_candidate(
        storage_dir=temp_storage_dir,
        version_tag=tag,
        total_vectors=15,
        included_codes=["POL-RETURN-US-2026"],
    )

    async with session_factory() as session:
        # Pre-seed candidate KnowledgeIndex and passing BenchmarkRun
        k_idx = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.abspath(index_path),
            total_vectors=15,
            status="CANDIDATE_READY",
            benchmark_score=0.96,
        )
        session.add(k_idx)
        await session.flush()

        bench_run = BenchmarkRun(
            index_id=k_idx.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=25,
            grounding_score=0.96,
            latency_p95_ms=2.5,
            is_passed=True,
            diagnostics={
                "retrieval_hit_rate_at_5": 1.0,
                "is_inconclusive": False,
                "gate_results": {"all_passed": True},
            },
        )
        session.add(bench_run)
        await session.commit()

        # Execute promotion
        res = await promoter.promote_candidate(
            session=session,
            candidate_tag=tag,
            caller_identity="admin_jdoe",
            caller_roles=["ADMIN"],
        )

        assert res.success is True
        assert res.promoted_version_tag == tag
        assert res.total_vectors == 15
        assert res.redis_notified is True

        # Verify active_index.json exists and points to candidate
        active_manifest = promoter.read_active_manifest()
        assert active_manifest is not None
        assert active_manifest["active_version_tag"] == tag
        assert active_manifest["total_vectors"] == 15
        assert active_manifest["promoted_by"] == "admin_jdoe"

        # Verify DB status is ACTIVE
        refreshed = (
            await session.execute(
                select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag)
            )
        ).scalar_one()
        assert refreshed.status == "ACTIVE"
        assert refreshed.activated_at is not None

        # Verify Redis Pub/Sub published reload event
        assert len(mock_redis_service.published_events) == 1
        ch, msg = mock_redis_service.published_events[0]
        assert "INDEX_PROMOTED" in msg
        assert tag in msg


# =====================================================================
# Tests: Benchmark & Integrity Rejections
# =====================================================================

@pytest.mark.asyncio
async def test_failed_benchmark_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Candidate with failed benchmark run is rejected from promotion."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_cand_failed_bench"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5)

    async with session_factory() as session:
        k_idx = KnowledgeIndex(
            index_version_tag=tag,
            storage_path="/path",
            total_vectors=5,
            status="FAILED_GATE",
            benchmark_score=0.75,
        )
        session.add(k_idx)
        await session.flush()

        # Benchmark failed (is_passed=False)
        bench_run = BenchmarkRun(
            index_id=k_idx.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=18,
            grounding_score=0.75,
            latency_p95_ms=3.0,
            is_passed=False,
            diagnostics={"is_inconclusive": False},
        )
        session.add(bench_run)
        await session.commit()

        with pytest.raises(BenchmarkGateFailedError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="admin_test",
                caller_roles=["ADMIN"],
            )
        assert "failed Golden Benchmark gates" in str(exc_info.value)

        # Manifest must not have been created
        assert promoter.read_active_manifest() is None


@pytest.mark.asyncio
async def test_inconclusive_evaluation_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Candidate with inconclusive evaluation is strictly rejected."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_cand_inconclusive"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5)

    async with session_factory() as session:
        k_idx = KnowledgeIndex(
            index_version_tag=tag,
            storage_path="/path",
            total_vectors=5,
            status="EVALUATION_INCONCLUSIVE",
        )
        session.add(k_idx)
        await session.flush()

        bench_run = BenchmarkRun(
            index_id=k_idx.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=20,
            grounding_score=0.91,
            latency_p95_ms=2.0,
            is_passed=False,
            diagnostics={"is_inconclusive": True},
        )
        session.add(bench_run)
        await session.commit()

        with pytest.raises(BenchmarkGateFailedError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="admin_test",
            )
        assert "failed Golden Benchmark gates" in str(exc_info.value) or "inconclusive" in str(exc_info.value)


@pytest.mark.asyncio
async def test_checksum_mismatch_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Candidate with tampered/mismatched SHA-256 is rejected before manifest creation."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_tampered_hash"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5, tamper_hash=True)

    async with session_factory() as session:
        with pytest.raises(IndexIntegrityError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="admin_test",
            )
        assert "SHA-256 mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_corrupted_faiss_index_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Corrupted binary FAISS index file fails read-back validation."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_corrupt_bin"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5, corrupt_index=True)

    async with session_factory() as session:
        with pytest.raises(IndexIntegrityError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="admin_test",
            )
        assert "cannot be loaded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_missing_candidate_directory_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Attempting to promote a non-existent candidate directory raises IndexPromotionError."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    async with session_factory() as session:
        with pytest.raises(IndexPromotionError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag="idx_ghost_nonexistent",
                caller_identity="admin_test",
            )
        assert "Candidate directory does not exist" in str(exc_info.value)


# =====================================================================
# Tests: Maintenance Window & RBAC Constraints
# =====================================================================

@pytest.mark.asyncio
async def test_maintenance_window_rejection(
    temp_storage_dir,
    mock_redis_service,
    sqlite_engine,
):
    """Promotion outside maintenance window is rejected with MaintenanceWindowError."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    # Strictly Sunday 02:00-04:00 UTC
    schedule = MaintenanceSchedule(
        enabled=True,
        timezone="UTC",
        allowed_weekdays=[6],
        time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
    )
    maint_service = MaintenanceWindowService(schedule=schedule)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=maint_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_maint_block"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5)

    async with session_factory() as session:
        # Wednesday Nov 4 at 14:00 UTC (outside window)
        eval_time = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)

        with pytest.raises(MaintenanceWindowError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="admin_test",
                evaluation_time=eval_time,
            )
        assert exc_info.value.status_code == 409
        assert "outside permitted weekdays" in exc_info.value.message


@pytest.mark.asyncio
async def test_unauthorized_user_rejection(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """User without administrative privileges is rejected with UnauthorizedPromotionError."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_cand_unauth"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=5)

    async with session_factory() as session:
        with pytest.raises(UnauthorizedPromotionError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag=tag,
                caller_identity="agent_bob",
                caller_roles=["AGENT"],  # Not an admin
            )
        assert exc_info.value.status_code == 403
        assert "lacks required administrative roles" in str(exc_info.value)


@pytest.mark.asyncio
async def test_emergency_override_with_audit_trail(
    temp_storage_dir,
    mock_redis_service,
    sqlite_engine,
):
    """Admin override permits promotion outside window and records justification in manifest."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    # Window enabled and outside
    schedule = MaintenanceSchedule(enabled=True, timezone="UTC", allowed_weekdays=[6])
    maint_service = MaintenanceWindowService(schedule=schedule)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=maint_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_emergency_promo"
    cand_dir, index_path, _ = create_mock_candidate(temp_storage_dir, tag, total_vectors=10)

    async with session_factory() as session:
        k_idx = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.abspath(index_path),
            total_vectors=10,
            status="CANDIDATE_READY",
        )
        session.add(k_idx)
        await session.flush()

        bench_run = BenchmarkRun(
            index_id=k_idx.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=25,
            grounding_score=0.98,
            latency_p95_ms=2.0,
            is_passed=True,
            diagnostics={"is_inconclusive": False},
        )
        session.add(bench_run)
        await session.commit()

        # Wednesday evaluation (outside window) with admin override
        wednesday_dt = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc)
        override_reason = "URGENT: Remediating critical pricing policy compliance breach."

        res = await promoter.promote_candidate(
            session=session,
            candidate_tag=tag,
            caller_identity="vp_engineering",
            caller_roles=["SYSTEM_ADMIN"],
            admin_override=True,
            override_reason=override_reason,
            evaluation_time=wednesday_dt,
        )

        assert res.success is True
        assert res.admin_override_used is True
        assert res.override_reason == override_reason

        manifest = promoter.read_active_manifest()
        assert manifest["override_reason"] == override_reason
        assert manifest["promoted_by"] == "vp_engineering"


@pytest.mark.asyncio
async def test_emergency_override_missing_reason_rejected(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Emergency override without descriptive justification is rejected."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    async with session_factory() as session:
        with pytest.raises(UnauthorizedPromotionError) as exc_info:
            await promoter.promote_candidate(
                session=session,
                candidate_tag="idx_test",
                caller_identity="admin_test",
                caller_roles=["ADMIN"],
                admin_override=True,
                override_reason="short",  # < 10 characters
            )
        assert "requires an explicit, descriptive justification" in str(exc_info.value)


# =====================================================================
# Tests: Previous Index Preservation & Database Invariants
# =====================================================================

@pytest.mark.asyncio
async def test_previous_index_preserved_and_exactly_one_active_invariant(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Sequential promotions preserve previous index directories and maintain exactly one ACTIVE row."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag_v1 = "idx_seq_v1"
    tag_v2 = "idx_seq_v2"

    _, idx_path_1, _ = create_mock_candidate(temp_storage_dir, tag_v1, total_vectors=8)
    _, idx_path_2, _ = create_mock_candidate(temp_storage_dir, tag_v2, total_vectors=12)

    async with session_factory() as session:
        # Seed both candidates with passing benchmarks
        k1 = KnowledgeIndex(index_version_tag=tag_v1, storage_path=os.path.abspath(idx_path_1), total_vectors=8, status="CANDIDATE_READY")
        k2 = KnowledgeIndex(index_version_tag=tag_v2, storage_path=os.path.abspath(idx_path_2), total_vectors=12, status="CANDIDATE_READY")
        session.add_all([k1, k2])
        await session.flush()

        b1 = BenchmarkRun(index_id=k1.id, benchmark_suite_version="v1.2.0", total_queries=25, passed_queries=25, grounding_score=0.95, latency_p95_ms=2.0, is_passed=True, diagnostics={})
        b2 = BenchmarkRun(index_id=k2.id, benchmark_suite_version="v1.2.0", total_queries=25, passed_queries=25, grounding_score=0.97, latency_p95_ms=2.0, is_passed=True, diagnostics={})
        session.add_all([b1, b2])
        await session.commit()

        # 1. Promote V1
        res1 = await promoter.promote_candidate(session=session, candidate_tag=tag_v1, caller_identity="admin_1")
        assert res1.promoted_version_tag == tag_v1
        assert res1.previous_version_tag is None

        # Verify DB: exactly 1 ACTIVE
        active_indices = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE"))).scalars().all()
        assert len(active_indices) == 1
        assert active_indices[0].index_version_tag == tag_v1

        # 2. Promote V2
        res2 = await promoter.promote_candidate(session=session, candidate_tag=tag_v2, caller_identity="admin_2")
        assert res2.promoted_version_tag == tag_v2
        assert res2.previous_version_tag == tag_v1

        # Verify previous V1 directory remains intact on disk
        v1_dir = os.path.join(temp_storage_dir, tag_v1)
        assert os.path.isdir(v1_dir)
        assert os.path.isfile(os.path.join(v1_dir, "index.faiss"))

        # Verify DB: exactly 1 ACTIVE (V2), and V1 is SUPERSEDED
        active_indices_after = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE"))).scalars().all()
        assert len(active_indices_after) == 1
        assert active_indices_after[0].index_version_tag == tag_v2

        superseded_indices = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.status == "SUPERSEDED"))).scalars().all()
        assert any(idx.index_version_tag == tag_v1 for idx in superseded_indices)


# =====================================================================
# Tests: Crash Recovery & Reconciliation
# =====================================================================

@pytest.mark.asyncio
async def test_crash_recovery_advances_db_when_manifest_was_swapped(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """If process crashed after manifest swap but before DB commit, reconciler advances DB to ACTIVE."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag_v1 = "idx_crash_v1"
    tag_v2 = "idx_crash_v2"

    _, idx_path_1, _ = create_mock_candidate(temp_storage_dir, tag_v1, total_vectors=8)
    _, idx_path_2, _ = create_mock_candidate(temp_storage_dir, tag_v2, total_vectors=14)

    # Write manifest pointing to V2 (simulating successful manifest swap)
    manifest_payload = {
        "active_version_tag": tag_v2,
        "index_file_path": os.path.abspath(idx_path_2),
        "total_vectors": 14,
        "embedding_dimension": 384,
        "sha256": promoter.compute_sha256(idx_path_2),
        "swapped_at": datetime.now(timezone.utc).isoformat(),
        "previous_version_tag": tag_v1,
    }
    promoter.write_atomic_manifest(manifest_payload)

    async with session_factory() as session:
        # In DB: V1 is still ACTIVE, V2 was left in PROMOTING when crash occurred
        k1 = KnowledgeIndex(index_version_tag=tag_v1, storage_path=idx_path_1, total_vectors=8, status="ACTIVE")
        k2 = KnowledgeIndex(index_version_tag=tag_v2, storage_path=idx_path_2, total_vectors=14, status="PROMOTING")
        session.add_all([k1, k2])
        await session.commit()

        # Run startup reconciliation
        recon = await promoter.reconcile_active_index_state(session)

        assert recon.reconciled is True
        assert recon.action_taken == "ADVANCED_DATABASE_TO_MATCH_MANIFEST"
        assert recon.active_version_tag == tag_v2

        # Verify DB updated: V2 is ACTIVE, V1 is SUPERSEDED
        refreshed_v2 = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag_v2))).scalar_one()
        assert refreshed_v2.status == "ACTIVE"

        refreshed_v1 = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag_v1))).scalar_one()
        assert refreshed_v1.status == "SUPERSEDED"


@pytest.mark.asyncio
async def test_crash_recovery_reverts_db_when_manifest_unswapped(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """If process crashed before manifest swap occurred, reconciler resets PROMOTING candidate to FAILED."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag_v1 = "idx_revert_v1"
    tag_v2 = "idx_revert_v2"

    _, idx_path_1, _ = create_mock_candidate(temp_storage_dir, tag_v1, total_vectors=8)
    _, idx_path_2, _ = create_mock_candidate(temp_storage_dir, tag_v2, total_vectors=14)

    # Manifest still points to V1
    manifest_payload = {
        "active_version_tag": tag_v1,
        "index_file_path": os.path.abspath(idx_path_1),
        "total_vectors": 8,
        "embedding_dimension": 384,
        "sha256": promoter.compute_sha256(idx_path_1),
        "swapped_at": datetime.now(timezone.utc).isoformat(),
    }
    promoter.write_atomic_manifest(manifest_payload)

    async with session_factory() as session:
        # In DB: V1 is ACTIVE, V2 was left in PROMOTING when crash occurred before swap
        k1 = KnowledgeIndex(index_version_tag=tag_v1, storage_path=idx_path_1, total_vectors=8, status="ACTIVE")
        k2 = KnowledgeIndex(index_version_tag=tag_v2, storage_path=idx_path_2, total_vectors=14, status="PROMOTING")
        session.add_all([k1, k2])
        await session.commit()

        # Run startup reconciliation
        recon = await promoter.reconcile_active_index_state(session)

        assert recon.reconciled is True
        assert recon.action_taken == "REVERTED_ORPHANED_PROMOTING_INDEX"

        # Verify DB: V2 reset to FAILED, V1 remains ACTIVE
        refreshed_v2 = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag_v2))).scalar_one()
        assert refreshed_v2.status == "FAILED"
        assert "Process crashed before" in refreshed_v2.rollback_reason

        refreshed_v1 = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag_v1))).scalar_one()
        assert refreshed_v1.status == "ACTIVE"


@pytest.mark.asyncio
async def test_recreate_missing_manifest_from_authoritative_db(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """When active_index.json is missing, reconciler recreates it from authoritative ACTIVE DB row."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_restore_manifest"
    _, idx_path, _ = create_mock_candidate(temp_storage_dir, tag, total_vectors=12)

    # Ensure active_index.json does not exist
    manifest_file = promoter.get_active_manifest_path()
    if os.path.exists(manifest_file):
        os.remove(manifest_file)

    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.abspath(idx_path),
            total_vectors=12,
            status="ACTIVE",
        )
        session.add(k)
        await session.commit()

        recon = await promoter.reconcile_active_index_state(session)

        assert recon.reconciled is True
        assert recon.action_taken == "RECREATED_MANIFEST_FROM_DATABASE"
        assert recon.active_version_tag == tag

        # Verify active_index.json was recreated
        manifest = promoter.read_active_manifest()
        assert manifest is not None
        assert manifest["active_version_tag"] == tag
        assert manifest["total_vectors"] == 12


@pytest.mark.asyncio
async def test_redis_outage_graceful_degradation(
    temp_storage_dir,
    permissive_maintenance_service,
    sqlite_engine,
):
    """When Redis is unavailable or raises errors, promotion succeeds with redis_notified=False."""
    class BrokenRedis(RedisService):
        def __init__(self):
            super().__init__()
        async def publish(self, channel: str, message: str, raise_on_error: bool = False):
            raise ConnectionError("Redis cluster unreachable")
        async def set(self, key: str, value: Any, ttl: Optional[int] = None):
            raise ConnectionError("Redis connection refused")

    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=BrokenRedis(),
    )

    tag = "idx_redis_down"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=7)

    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.join(temp_storage_dir, tag, "index.faiss"),
            total_vectors=7,
            status="CANDIDATE",
        )
        session.add(k)
        await session.commit()

        b_run = BenchmarkRun(
            index_id=k.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=25,
            grounding_score=0.92,
            latency_p95_ms=350.0,
            is_passed=True,
            diagnostics={
                "retrieval_hit_rate_at_5": 0.96,
                "is_inconclusive": False,
                "gate_results": {"all_passed": True},
            },
        )
        session.add(b_run)
        await session.commit()

        res = await promoter.promote_candidate(
            session=session,
            candidate_tag=tag,
            caller_identity="admin_test",
        )

        assert res.success is True
        assert res.promoted_version_tag == tag
        assert res.redis_notified is False  # Graceful degradation verified

        # Manifest and DB were still successfully updated
        manifest = promoter.read_active_manifest()
        assert manifest is not None
        assert manifest["active_version_tag"] == tag

        refreshed = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag))).scalar_one()
        assert refreshed.status == "ACTIVE"


@pytest.mark.asyncio
async def test_concurrent_promotions_blocked_by_advisory_lock(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Concurrent promotion attempt blocked by held advisory lock raises KnowledgeLockTimeoutError."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
        lock_timeout_seconds=0.2,
    )

    tag = "idx_lock_contention"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=6)

    async with session_factory() as session1, session_factory() as session2:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.join(temp_storage_dir, tag, "index.faiss"),
            total_vectors=6,
            status="CANDIDATE",
        )
        session1.add(k)
        await session1.commit()

        lock_id = getattr(settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)
        async with DatabaseAdvisoryLock(session=session1, lock_id=lock_id, timeout_seconds=2.0):
            with pytest.raises(KnowledgeLockTimeoutError) as exc_info:
                await promoter.promote_candidate(
                    session=session2,
                    candidate_tag=tag,
                    caller_identity="admin_test",
                )
            assert "Timed out" in str(exc_info.value)


# =====================================================================
# Tests: Redis TTL & Reader Synchronization (Tiers 1, 2, 3)
# =====================================================================

@pytest.mark.asyncio
async def test_redis_cache_receives_300s_ttl(
    temp_storage_dir,
    permissive_maintenance_service,
    mock_redis_service,
    sqlite_engine,
):
    """Successful promotion sets cache:active_index_version with exact 300-second TTL."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    promoter = IndexPromoter(
        settings=settings,
        maintenance_service=permissive_maintenance_service,
        redis_service=mock_redis_service,
    )

    tag = "idx_redis_ttl_verify"
    create_mock_candidate(temp_storage_dir, tag, total_vectors=8)

    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=os.path.join(temp_storage_dir, tag, "index.faiss"),
            total_vectors=8,
            status="CANDIDATE",
        )
        session.add(k)
        await session.commit()

        b_run = BenchmarkRun(
            index_id=k.id,
            benchmark_suite_version="v1.2.0",
            total_queries=25,
            passed_queries=25,
            grounding_score=0.95,
            latency_p95_ms=12.0,
            is_passed=True,
            diagnostics={
                "retrieval_hit_rate_at_5": 1.0,
                "is_inconclusive": False,
                "gate_results": {"all_passed": True},
            },
        )
        session.add(b_run)
        await session.commit()

        res = await promoter.promote_candidate(
            session=session,
            candidate_tag=tag,
            caller_identity="admin_test",
        )

        assert res.success is True
        assert res.redis_notified is True

        # Assert Redis cache received exact key, value, and ttl_seconds=300
        cache_entry = mock_redis_service.cache.get("cache:active_index_version")
        assert cache_entry is not None, "cache:active_index_version was not set in Redis"
        val, ttl_sec = cache_entry
        assert val == tag
        assert ttl_sec == 300, f"Expected 300-second TTL, got {ttl_sec}"


@pytest.mark.asyncio
async def test_reader_startup_synchronization(
    temp_storage_dir,
    mock_redis_service,
    sqlite_engine,
):
    """Tier 3: Worker startup synchronization loads active index into memory."""
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    tag = "idx_reader_boot"
    cand_dir, idx_path, _ = create_mock_candidate(temp_storage_dir, tag, total_vectors=10)

    # Write manifest pointing to tag
    manifest_path = os.path.join(temp_storage_dir, "active_index.json")
    manifest_payload = {
        "active_version_tag": tag,
        "index_file_path": os.path.abspath(idx_path),
        "total_vectors": 10,
        "embedding_dimension": 384,
        "sha256": "dummy",
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_payload, f)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    assert reader.in_memory_index is None
    assert reader.get_active_version_tag() is None

    # Boot sync
    synced = await reader.sync_on_startup()
    assert synced is True
    assert reader.get_active_version_tag() == tag
    assert reader.get_index() is not None
    assert reader.in_memory_index.ntotal == 10


@pytest.mark.asyncio
async def test_reader_pubsub_reload_hot_swap(
    temp_storage_dir,
    mock_redis_service,
):
    """Tier 1: Pub/Sub event arrival immediately hot-swaps in-memory FAISS index."""
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    tag_v1 = "idx_reader_v1"
    tag_v2 = "idx_reader_v2"

    _, idx_path_1, _ = create_mock_candidate(temp_storage_dir, tag_v1, total_vectors=5)
    _, idx_path_2, _ = create_mock_candidate(temp_storage_dir, tag_v2, total_vectors=12)

    manifest_path = os.path.join(temp_storage_dir, "active_index.json")

    # Initial state: manifest points to V1
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"active_version_tag": tag_v1, "index_file_path": os.path.abspath(idx_path_1)}, f)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    await reader.sync_on_startup()
    assert reader.get_active_version_tag() == tag_v1
    assert reader.in_memory_index.ntotal == 5

    # Manifest updated to V2 and Pub/Sub event broadcast
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"active_version_tag": tag_v2, "index_file_path": os.path.abspath(idx_path_2)}, f)

    reload_event = {
        "event": "INDEX_PROMOTED",
        "active_version_tag": tag_v2,
        "previous_version_tag": tag_v1,
    }
    swapped = await reader.handle_reload_event(reload_event)

    assert swapped is True
    assert reader.get_active_version_tag() == tag_v2
    assert reader.in_memory_index.ntotal == 12


@pytest.mark.asyncio
async def test_reader_reconciliation_heals_missed_pubsub_and_stale_reader(
    temp_storage_dir,
    mock_redis_service,
):
    """Tier 2: Periodic reconciliation detects tag divergence and replaces stale reader."""
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    tag_v1 = "idx_stale_v1"
    tag_v2 = "idx_fresh_v2"

    _, idx_path_1, _ = create_mock_candidate(temp_storage_dir, tag_v1, total_vectors=6)
    _, idx_path_2, _ = create_mock_candidate(temp_storage_dir, tag_v2, total_vectors=18)

    manifest_path = os.path.join(temp_storage_dir, "active_index.json")

    # Reader starts up on V1
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"active_version_tag": tag_v1, "index_file_path": os.path.abspath(idx_path_1)}, f)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    await reader.sync_on_startup()
    assert reader.get_active_version_tag() == tag_v1
    assert reader.in_memory_index.ntotal == 6

    # Manifest swapped to V2 behind the scenes (simulating missed Pub/Sub event)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"active_version_tag": tag_v2, "index_file_path": os.path.abspath(idx_path_2)}, f)

    # Reader still holds stale V1 in memory
    assert reader.get_active_version_tag() == tag_v1

    # Tier 2 reconciliation cycle runs
    healed = await reader.reconcile_active_version()

    assert healed is True
    assert reader.get_active_version_tag() == tag_v2
    assert reader.in_memory_index.ntotal == 18

    # Second reconciliation cycle without changes is a no-op
    no_op = await reader.reconcile_active_version()
    assert no_op is False
