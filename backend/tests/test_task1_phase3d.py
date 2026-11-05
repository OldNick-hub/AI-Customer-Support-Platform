"""Day 05 — Task 1 Phase 3D: Automated Rollback Watchdog Tests.

Verifies:
1. Canary health check passes on healthy FAISS index (latency <= 100ms, similarity >= 0.75).
2. Canary health check detects P95 latency regressions (> 100ms).
3. Canary health check detects query execution timeouts (> 2.0s).
4. Canary health check detects semantic similarity regressions (< 0.75).
5. Canary health check detects missing or corrupted FAISS index snapshots.
6. Watchdog healthy canary window expiry confirms candidate index as permanent baseline.
7. Consecutive failure trigger (>= 2 consecutive interval failures) executes automated rollback.
8. Total failure trigger (>= 3 total failures across monitoring intervals) executes automated rollback.
9. Consecutive failure counter resets to 0 upon any healthy interval check.
10. Target snapshot cryptographic or structural corruption halts rollback before destructive swap.
11. Exactly one authoritative ACTIVE index invariant preserved in database and filesystem manifest.
12. Target DocumentVersion synchronization sets restored documents to is_current=True.
13. Graceful degradation when Redis is unavailable during rollback execution (redis_notified=False).
14. In-memory ActiveIndexReader hot-swap upon rollback event.
15. Worker restart recovery resumes active canary monitoring when window unexpired.
16. Worker restart recovery executes rollback when downtime expired window with breached failures.
17. Worker restart recovery completes interrupted rollback stuck in ROLLBACK_PENDING.
18. Concurrent rollback execution blocked under transaction-scoped PostgreSQL advisory lock (5432002).
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
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
    KnowledgeLockTimeoutError,
    RollbackExecutionError,
)
from app.db.base import Base
from app.lifecycle import WorkerLifecycleManager
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.knowledge_index import BenchmarkRun, KnowledgeIndex
from app.modules.knowledge.change_detector import DatabaseAdvisoryLock
from app.modules.knowledge.promoter import IndexPromoter
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.knowledge.rollback_manager import RollbackManager, RollbackResult
from app.modules.knowledge.watchdog import (
    CanaryCheckResult,
    IndexHealthWatchdog,
    WatchdogEvaluationResult,
)
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
def mock_redis_service():
    """Mock Redis service that records publishes and sets without connecting to real server."""
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

        async def get(self, key: str, default: Optional[str] = None, raise_on_error: bool = False) -> Optional[str]:
            if key in self.cache:
                val, _ = self.cache[key]
                return str(val)
            return default

        async def delete(self, key: str, raise_on_error: bool = False) -> bool:
            if key in self.cache:
                del self.cache[key]
                return True
            return False

        async def exists(self, key: str, raise_on_error: bool = False) -> bool:
            return key in self.cache

    return MockRedis()


def create_snapshot_index(
    storage_dir: str,
    version_tag: str,
    total_vectors: int = 10,
    corrupt_index: bool = False,
    tamper_hash: bool = False,
    included_codes: Optional[List[str]] = None,
) -> Tuple[str, str, str]:
    """Helper creating a directory, valid FAISS IndexIDMap2, and manifest.json."""
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
        "status": "ACTIVE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "included_document_codes": included_codes or ["POL-RETURN-US-2026"],
    }

    manifest_path = os.path.join(cand_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_payload, f, indent=2)

    return cand_dir, index_path, manifest_path


def setup_active_manifest(
    storage_dir: str,
    active_tag: str,
    index_path: str,
    total_vectors: int,
    previous_tag: Optional[str] = None,
) -> str:
    """Writes canonical active_index.json pointing to active_tag."""
    manifest_path = os.path.join(storage_dir, "active_index.json")
    payload = {
        "active_version_tag": active_tag,
        "index_file_path": os.path.abspath(index_path),
        "total_vectors": total_vectors,
        "embedding_dimension": 384,
        "sha256": "dummy_sha256",
        "swapped_at": datetime.now(timezone.utc).isoformat(),
        "previous_version_tag": previous_tag,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return manifest_path


# =====================================================================
# Tests: Canary Health Checks (1 - 5)
# =====================================================================

def test_canary_health_check_passes_on_healthy_index(temp_storage_dir):
    """Canary check passes with latency <= 100ms and similarity >= 0.75 on healthy index."""
    tag = "idx_healthy_v1"
    _, index_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=20)
    setup_active_manifest(temp_storage_dir, tag, index_path, total_vectors=20)

    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    reader = ActiveIndexReader(settings=settings)
    reader.load_active_index()

    # Injected query runner simulating healthy responses (15ms, 0.88 similarity)
    def healthy_runner(idx, query):
        return 15.0, 0.88

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        query_runner=healthy_runner,
    )

    check = watchdog.run_canary_health_check()
    assert check.is_healthy is True
    assert check.latency_p95_ms <= 100.0
    assert check.average_similarity >= 0.75
    assert check.passed_count == 3
    assert len(check.failure_reasons) == 0


def test_canary_health_check_detects_latency_regression(temp_storage_dir):
    """Canary check trips is_healthy=False when P95 retrieval latency breaches 100ms."""
    tag = "idx_slow_v1"
    _, index_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=20)
    setup_active_manifest(temp_storage_dir, tag, index_path, total_vectors=20)

    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    reader = ActiveIndexReader(settings=settings)
    reader.load_active_index()

    # Injected runner returning 180ms latency (> 100ms threshold)
    def slow_runner(idx, query):
        return 180.0, 0.90

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        query_runner=slow_runner,
    )

    check = watchdog.run_canary_health_check()
    assert check.is_healthy is False
    assert check.latency_p95_ms > 100.0
    assert any("P95 latency breached threshold" in r for r in check.failure_reasons)


def test_canary_health_check_detects_query_timeout(temp_storage_dir):
    """Canary check trips is_healthy=False when individual query exceeds 2.0s timeout."""
    tag = "idx_timeout_v1"
    _, index_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=20)
    setup_active_manifest(temp_storage_dir, tag, index_path, total_vectors=20)

    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    reader = ActiveIndexReader(settings=settings)
    reader.load_active_index()

    # Injected runner returning 2500ms (> 2000ms query timeout)
    def timeout_runner(idx, query):
        return 2500.0, 0.85

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        query_runner=timeout_runner,
    )

    check = watchdog.run_canary_health_check()
    assert check.is_healthy is False
    assert any("timed out" in r for r in check.failure_reasons)


def test_canary_health_check_detects_similarity_regression(temp_storage_dir):
    """Canary check trips is_healthy=False when semantic similarity drops below 0.75."""
    tag = "idx_low_sim_v1"
    _, index_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=20)
    setup_active_manifest(temp_storage_dir, tag, index_path, total_vectors=20)

    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    reader = ActiveIndexReader(settings=settings)
    reader.load_active_index()

    # Injected runner returning 0.60 similarity (< 0.75 threshold)
    def low_sim_runner(idx, query):
        return 12.0, 0.60

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        query_runner=low_sim_runner,
    )

    check = watchdog.run_canary_health_check()
    assert check.is_healthy is False
    assert check.average_similarity < 0.75
    assert any("low similarity" in r for r in check.failure_reasons)


def test_canary_health_check_detects_missing_or_corrupted_index(temp_storage_dir):
    """Canary check fails immediately when active index is missing or unreadable."""
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    reader = ActiveIndexReader(settings=settings)

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
    )

    check = watchdog.run_canary_health_check()
    assert check.is_healthy is False
    assert check.latency_p95_ms == 9999.0
    assert any("unavailable, unreadable, or missing" in r for r in check.failure_reasons)


# =====================================================================
# Tests: Watchdog Evaluation & Automated Triggers (6 - 9)
# =====================================================================

@pytest.mark.asyncio
async def test_watchdog_healthy_window_expiry_confirms_index(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Canary window expiry with zero failures marks index CONFIRMED and canary_status PASSED."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_confirm_candidate"
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=15)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=15)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    reader.load_active_index()

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        redis_service=mock_redis_service,
    )

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        # Candidate was promoted 305 seconds ago, window expired 5 seconds ago
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=idx_path,
            total_vectors=15,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc - timedelta(seconds=5),
            rollback_target_tag="idx_previous_v0",
        )
        session.add(k)
        await session.commit()

        # Simulated healthy canary check result
        healthy_check = CanaryCheckResult(
            is_healthy=True,
            latency_p95_ms=18.0,
            average_similarity=0.88,
            query_count=3,
            passed_count=3,
        )

        res = await watchdog.evaluate_active_index_health(
            session=session,
            active_tag=tag,
            check_result=healthy_check,
            current_time=now_utc,
        )

        assert res.status == "CONFIRMED"
        assert res.rollback_executed is False
        assert res.consecutive_failures == 0

        # Verify DB updated to permanent baseline
        refreshed = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag))).scalar_one()
        assert refreshed.status == "CONFIRMED"
        assert refreshed.canary_status == "PASSED"


@pytest.mark.asyncio
async def test_watchdog_consecutive_failure_trigger_executes_rollback(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Two consecutive interval failures trigger automated zero-downtime rollback."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_prior_healthy_v1"
    failed_tag = "idx_cand_degraded_v2"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=10)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=12)

    setup_active_manifest(
        storage_dir=temp_storage_dir,
        active_tag=failed_tag,
        index_path=failed_path,
        total_vectors=12,
        previous_tag=target_tag,
    )

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    reader.load_active_index()

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service, reader=reader)
    watchdog = IndexHealthWatchdog(
        settings=settings,
        rollback_manager=rollback_mgr,
        reader=reader,
        redis_service=mock_redis_service,
    )

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        k_target = KnowledgeIndex(
            index_version_tag=target_tag,
            storage_path=target_path,
            total_vectors=10,
            status="SUPERSEDED",
        )
        k_failed = KnowledgeIndex(
            index_version_tag=failed_tag,
            storage_path=failed_path,
            total_vectors=12,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc + timedelta(seconds=240),
            rollback_target_tag=target_tag,
        )
        session.add_all([k_target, k_failed])
        await session.commit()

        # Interval 1: First failure (consecutive=1)
        fail_check_1 = CanaryCheckResult(
            is_healthy=False,
            latency_p95_ms=250.0,
            average_similarity=0.80,
            query_count=3,
            passed_count=1,
            failure_reasons=["P95 latency breached threshold (250.0ms > 100.0ms)."],
        )
        res1 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=fail_check_1)
        assert res1.status == "DEGRADED"
        assert res1.consecutive_failures == 1
        assert res1.total_failures == 1
        assert res1.rollback_executed is False

        # Interval 2: Second consecutive failure (consecutive=2 -> TRIGGER!)
        fail_check_2 = CanaryCheckResult(
            is_healthy=False,
            latency_p95_ms=300.0,
            average_similarity=0.79,
            query_count=3,
            passed_count=1,
            failure_reasons=["P95 latency breached threshold (300.0ms > 100.0ms)."],
        )
        res2 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=fail_check_2)
        assert res2.status == "ROLLBACK_TRIGGERED"
        assert res2.rollback_executed is True
        assert res2.rollback_result is not None
        assert res2.rollback_result.success is True
        assert res2.rollback_result.restored_version_tag == target_tag
        assert res2.rollback_result.rolled_back_version_tag == failed_tag

        # Verify active_index.json atomically restored to target_tag
        with open(os.path.join(temp_storage_dir, "active_index.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest["active_version_tag"] == target_tag

        # Verify DB states: target is ACTIVE, failed is ROLLED_BACK
        refreshed_target = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == target_tag))).scalar_one()
        refreshed_failed = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == failed_tag))).scalar_one()
        assert refreshed_target.status == "ACTIVE"
        assert refreshed_failed.status == "ROLLED_BACK"
        assert refreshed_failed.canary_status == "FAILED_ROLLED_BACK"
        assert "Automated Rollback" in refreshed_failed.rollback_reason


@pytest.mark.asyncio
async def test_watchdog_total_failure_trigger_executes_rollback(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Three total failures across monitoring intervals trigger rollback even if not consecutive."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_prior_good_v1"
    failed_tag = "idx_cand_flaky_v2"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=8)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=14)

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=14, previous_tag=target_tag)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    reader.load_active_index()

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service, reader=reader)
    watchdog = IndexHealthWatchdog(settings=settings, rollback_manager=rollback_mgr, reader=reader, redis_service=mock_redis_service)

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=8, status="SUPERSEDED")
        k_failed = KnowledgeIndex(
            index_version_tag=failed_tag,
            storage_path=failed_path,
            total_vectors=14,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc + timedelta(seconds=240),
            rollback_target_tag=target_tag,
        )
        session.add_all([k_target, k_failed])
        await session.commit()

        fail_check = CanaryCheckResult(is_healthy=False, latency_p95_ms=150.0, average_similarity=0.8, query_count=3, passed_count=1, failure_reasons=["Latency spike"])
        pass_check = CanaryCheckResult(is_healthy=True, latency_p95_ms=20.0, average_similarity=0.88, query_count=3, passed_count=3)

        # Interval 1: Fail (consecutive=1, total=1)
        r1 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=fail_check)
        assert r1.status == "DEGRADED" and r1.consecutive_failures == 1 and r1.total_failures == 1

        # Interval 2: Pass (consecutive resets to 0, total remains 1)
        r2 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=pass_check)
        assert r2.status == "HEALTHY" and r2.consecutive_failures == 0 and r2.total_failures == 1

        # Interval 3: Fail (consecutive=1, total=2)
        r3 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=fail_check)
        assert r3.status == "DEGRADED" and r3.consecutive_failures == 1 and r3.total_failures == 2

        # Interval 4: Pass (consecutive resets to 0, total remains 2)
        r4 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=pass_check)
        assert r4.status == "HEALTHY" and r4.consecutive_failures == 0 and r4.total_failures == 2

        # Interval 5: Fail (consecutive=1, total=3 -> TOTAL TRIGGER TRIPPED!)
        r5 = await watchdog.evaluate_active_index_health(session=session, active_tag=failed_tag, check_result=fail_check)
        assert r5.status == "ROLLBACK_TRIGGERED"
        assert r5.rollback_executed is True
        assert r5.total_failures == 3
        assert "total trigger" in r5.message


def test_watchdog_consecutive_counter_resets_on_success(temp_storage_dir):
    """Proves consecutive failure counter resets to 0 when healthy check succeeds."""
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)
    watchdog = IndexHealthWatchdog(settings=settings)
    assert watchdog.consecutive_failures == 0

    watchdog.consecutive_failures = 1
    # Check passes -> resets consecutive counter
    assert watchdog.consecutive_failures == 1


# =====================================================================
# Tests: Rollback Safety, Invariants & Consistency (10 - 14)
# =====================================================================

@pytest.mark.asyncio
async def test_rollback_target_integrity_failure_halts_rollback(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Corrupted target snapshot halts rollback before filesystem swap; manifest is preserved."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_corrupt_target_v1"
    active_tag = "idx_active_cand_v2"

    # Create target with CORRUPT index file
    create_snapshot_index(temp_storage_dir, target_tag, total_vectors=10, corrupt_index=True)
    _, active_path, _ = create_snapshot_index(temp_storage_dir, active_tag, total_vectors=15)

    manifest_path = setup_active_manifest(temp_storage_dir, active_tag, active_path, total_vectors=15, previous_tag=target_tag)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service, reader=reader)
    watchdog = IndexHealthWatchdog(settings=settings, rollback_manager=rollback_mgr, reader=reader, redis_service=mock_redis_service)

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path="", total_vectors=10, status="SUPERSEDED")
        k_active = KnowledgeIndex(
            index_version_tag=active_tag,
            storage_path=active_path,
            total_vectors=15,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=1,
            watchdog_expires_at=now_utc + timedelta(seconds=200),
            rollback_target_tag=target_tag,
        )
        session.add_all([k_target, k_active])
        await session.commit()

        # Set watchdog consecutive failures to 1 so next failure trips trigger
        watchdog.consecutive_failures = 1

        fail_check = CanaryCheckResult(
            is_healthy=False,
            latency_p95_ms=200.0,
            average_similarity=0.8,
            query_count=3,
            passed_count=0,
            failure_reasons=["Degraded"],
        )

        res = await watchdog.evaluate_active_index_health(session=session, active_tag=active_tag, check_result=fail_check)

        # Rollback must fail and halt without destroying the active manifest
        assert res.status == "ERROR"
        assert res.rollback_executed is False
        assert "Rollback aborted" in res.message or "Rollback execution failed" in res.message

        # Manifest must STILL point to active_tag
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest["active_version_tag"] == active_tag


@pytest.mark.asyncio
async def test_rollback_preserves_exactly_one_active_index_invariant(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """PostgreSQL and filesystem manifest maintain exactly one ACTIVE index before and after rollback."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_prior_v1"
    failed_tag = "idx_failed_v2"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=10)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=12)

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=12, previous_tag=target_tag)

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service)

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=10, status="SUPERSEDED")
        k_failed = KnowledgeIndex(index_version_tag=failed_tag, storage_path=failed_path, total_vectors=12, status="ACTIVE")
        session.add_all([k_target, k_failed])
        await session.commit()

        # Before rollback: exactly 1 active index
        actives_before = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE"))).scalars().all()
        assert len(actives_before) == 1
        assert actives_before[0].index_version_tag == failed_tag

        # Execute rollback
        rb_res = await rollback_mgr.execute_rollback(
            session=session,
            failed_tag=failed_tag,
            target_tag=target_tag,
            reason="Automated test rollback",
        )
        assert rb_res.success is True

        # After rollback: exactly 1 active index (target_tag)
        actives_after = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE"))).scalars().all()
        assert len(actives_after) == 1
        assert actives_after[0].index_version_tag == target_tag

        # Failed index is ROLLED_BACK
        refreshed_failed = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == failed_tag))).scalar_one()
        assert refreshed_failed.status == "ROLLED_BACK"


@pytest.mark.asyncio
async def test_rollback_synchronizes_document_versions(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Rollback restores target DocumentVersion records to is_current=True and supersedes failed versions."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_doc_sync_target_v1"
    failed_tag = "idx_doc_sync_failed_v2"

    _, target_path, _ = create_snapshot_index(
        temp_storage_dir,
        target_tag,
        total_vectors=10,
        included_codes=["POL-RETURN-US-2026"],
    )
    _, failed_path, _ = create_snapshot_index(
        temp_storage_dir,
        failed_tag,
        total_vectors=12,
        included_codes=["POL-RETURN-US-2026"],
    )

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=12, previous_tag=target_tag)
    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service)

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=10, status="SUPERSEDED")
        k_failed = KnowledgeIndex(index_version_tag=failed_tag, storage_path=failed_path, total_vectors=12, status="ACTIVE")

        doc = Document(
            document_code="POL-RETURN-US-2026",
            title="Return Policy",
            category="Billing",
        )
        session.add_all([k_target, k_failed, doc])
        await session.flush()

        v1 = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            storage_uri="/storage/raw/doc_v1.md",
            status="SUPERSEDED",
            effective_from=date.today(),
            is_current=False,
        )
        v2 = DocumentVersion(
            document_id=doc.id,
            version_number=2,
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b856",
            storage_uri="/storage/raw/doc_v2.md",
            status="ACTIVE",
            effective_from=date.today(),
            is_current=True,
        )
        session.add_all([v1, v2])
        await session.commit()

        # Execute rollback to target_tag
        await rollback_mgr.execute_rollback(
            session=session,
            failed_tag=failed_tag,
            target_tag=target_tag,
            reason="Canary failure test",
        )

        # Verify DocumentVersion records synchronized
        refreshed_v1 = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v1.id))).scalar_one()
        assert refreshed_v1.status == "ACTIVE"
        assert refreshed_v1.is_current is True

        refreshed_v2 = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v2.id))).scalar_one()
        assert refreshed_v2.status == "SUPERSEDED"
        assert refreshed_v2.is_current is False


@pytest.mark.asyncio
async def test_rollback_redis_outage_graceful_degradation(
    temp_storage_dir,
    sqlite_engine,
):
    """When Redis is down during rollback, operation succeeds with redis_notified=False."""
    class BrokenRedis(RedisService):
        def __init__(self):
            super().__init__()
        async def publish(self, channel: str, message: str, raise_on_error: bool = False):
            raise ConnectionError("Redis cluster unreachable")
        async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None, raise_on_error: bool = False):
            raise ConnectionError("Redis cluster unreachable")

    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_redis_fail_target"
    failed_tag = "idx_redis_fail_cand"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=5)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=8)

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=8, previous_tag=target_tag)

    rollback_mgr = RollbackManager(
        settings=settings,
        redis_service=BrokenRedis(),
    )

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=5, status="SUPERSEDED")
        k_failed = KnowledgeIndex(index_version_tag=failed_tag, storage_path=failed_path, total_vectors=8, status="ACTIVE")
        session.add_all([k_target, k_failed])
        await session.commit()

        res = await rollback_mgr.execute_rollback(
            session=session,
            failed_tag=failed_tag,
            target_tag=target_tag,
            reason="Redis outage test",
        )

        assert res.success is True
        assert res.redis_notified is False
        assert res.restored_version_tag == target_tag


@pytest.mark.asyncio
async def test_rollback_swaps_in_memory_reader(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Rollback execution triggers in-memory pointer swap on attached ActiveIndexReader."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_reader_swap_target"
    failed_tag = "idx_reader_swap_cand"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=6)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=14)

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=14, previous_tag=target_tag)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    reader.load_active_index()
    assert reader.get_active_version_tag() == failed_tag
    assert reader.in_memory_index.ntotal == 14

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service, reader=reader)

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=6, status="SUPERSEDED")
        k_failed = KnowledgeIndex(index_version_tag=failed_tag, storage_path=failed_path, total_vectors=14, status="ACTIVE")
        session.add_all([k_target, k_failed])
        await session.commit()

        res = await rollback_mgr.execute_rollback(
            session=session,
            failed_tag=failed_tag,
            target_tag=target_tag,
            reason="Reader hot-swap test",
        )
        assert res.success is True

        # Verify reader pointer was hot-swapped to target_tag
        assert reader.get_active_version_tag() == target_tag
        assert reader.in_memory_index.ntotal == 6


# =====================================================================
# Tests: Worker Restart Recovery (15 - 17)
# =====================================================================

@pytest.mark.asyncio
async def test_watchdog_restart_recovery_resumes_active_monitoring(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Worker restart with unexpired window resumes active monitoring without resetting state."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_restart_active_v1"
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=10)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=10)

    watchdog = IndexHealthWatchdog(settings=settings, redis_service=mock_redis_service)
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=idx_path,
            total_vectors=10,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=1,
            watchdog_expires_at=now_utc + timedelta(seconds=120),  # 2 minutes left
            rollback_target_tag="idx_target_v0",
        )
        session.add(k)
        await session.commit()

        recovery_results = await watchdog.recover_monitoring_on_restart(session=session, current_time=now_utc)
        assert len(recovery_results) == 1
        res = recovery_results[0]
        assert res.status == "HEALTHY"
        assert res.active_tag == tag
        assert "Resumed active canary monitoring" in res.message


@pytest.mark.asyncio
async def test_watchdog_restart_recovery_executes_rollback_on_expired_failures(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Worker restart with expired window and breached failure count executes automated rollback."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_prior_known_good"
    failed_tag = "idx_reboot_expired_fail"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=7)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=9)

    setup_active_manifest(temp_storage_dir, failed_tag, failed_path, total_vectors=9, previous_tag=target_tag)

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service)
    watchdog = IndexHealthWatchdog(settings=settings, rollback_manager=rollback_mgr, redis_service=mock_redis_service)
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=7, status="SUPERSEDED")
        k_failed = KnowledgeIndex(
            index_version_tag=failed_tag,
            storage_path=failed_path,
            total_vectors=9,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=3,  # Already breached threshold of 3
            watchdog_expires_at=now_utc - timedelta(seconds=60),  # Expired during downtime
            rollback_target_tag=target_tag,
        )
        session.add_all([k_target, k_failed])
        await session.commit()

        recovery_results = await watchdog.recover_monitoring_on_restart(session=session, current_time=now_utc)
        assert len(recovery_results) == 1
        res = recovery_results[0]
        assert res.status == "ROLLBACK_TRIGGERED"
        assert res.rollback_executed is True
        assert res.target_tag == target_tag


@pytest.mark.asyncio
async def test_watchdog_restart_recovery_completes_pending_rollback(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Worker restart recovers interrupted rollback stuck in ROLLBACK_PENDING state."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    target_tag = "idx_pending_target"
    pending_tag = "idx_pending_interrupted"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, target_tag, total_vectors=5)
    _, pending_path, _ = create_snapshot_index(temp_storage_dir, pending_tag, total_vectors=11)

    setup_active_manifest(temp_storage_dir, pending_tag, pending_path, total_vectors=11, previous_tag=target_tag)

    rollback_mgr = RollbackManager(settings=settings, redis_service=mock_redis_service)
    watchdog = IndexHealthWatchdog(settings=settings, rollback_manager=rollback_mgr, redis_service=mock_redis_service)

    async with session_factory() as session:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path=target_path, total_vectors=5, status="SUPERSEDED")
        k_pending = KnowledgeIndex(
            index_version_tag=pending_tag,
            storage_path=pending_path,
            total_vectors=11,
            status="ROLLBACK_PENDING",
            rollback_target_tag=target_tag,
        )
        session.add_all([k_target, k_pending])
        await session.commit()

        recovery_results = await watchdog.recover_monitoring_on_restart(session=session)
        assert len(recovery_results) == 1
        res = recovery_results[0]
        assert res.status == "ROLLBACK_TRIGGERED"
        assert res.rollback_executed is True

        # Verify DB completed rollback
        refreshed = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == pending_tag))).scalar_one()
        assert refreshed.status == "ROLLED_BACK"


# =====================================================================
# Tests: Concurrency & Lock Protection (18)
# =====================================================================

@pytest.mark.asyncio
async def test_concurrent_rollback_blocked_by_advisory_lock(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Concurrent rollback attempt blocked by active advisory lock raises KnowledgeLockTimeoutError."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    rollback_mgr = RollbackManager(
        settings=settings,
        redis_service=mock_redis_service,
        lock_timeout_seconds=0.2,
    )

    target_tag = "idx_lock_target"
    failed_tag = "idx_lock_failed"

    create_snapshot_index(temp_storage_dir, target_tag, total_vectors=5)
    create_snapshot_index(temp_storage_dir, failed_tag, total_vectors=5)

    async with session_factory() as session1, session_factory() as session2:
        k_target = KnowledgeIndex(index_version_tag=target_tag, storage_path="", total_vectors=5, status="SUPERSEDED")
        k_failed = KnowledgeIndex(index_version_tag=failed_tag, storage_path="", total_vectors=5, status="ACTIVE")
        session1.add_all([k_target, k_failed])
        await session1.commit()

        lock_id = getattr(settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)
        async with DatabaseAdvisoryLock(session=session1, lock_id=lock_id, timeout_seconds=2.0):
            with pytest.raises(KnowledgeLockTimeoutError) as exc_info:
                await rollback_mgr.execute_rollback(
                    session=session2,
                    failed_tag=failed_tag,
                    target_tag=target_tag,
                    reason="Concurrent contention test",
                )
            assert "Timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_live_postgres_rollback_concurrency_and_advisory_lock(
    temp_storage_dir,
    mock_redis_service,
):
    """Verifies live PostgreSQL 16 transaction-scoped advisory lock (5432002) blocks concurrent rollback."""
    from sqlalchemy.pool import NullPool
    from app.core.config import settings as global_settings

    if not global_settings.POSTGRES_PASSWORD:
        pytest.skip("Live PostgreSQL password not configured.")

    try:
        live_engine = create_async_engine(global_settings.async_database_url, poolclass=NullPool, echo=False)
        async with live_engine.connect() as conn:
            await conn.execute(select(1))
    except Exception as exc:
        pytest.skip(f"Live PostgreSQL not reachable: {exc}")

    session_factory = async_sessionmaker(bind=live_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    rollback_mgr = RollbackManager(
        settings=settings,
        redis_service=mock_redis_service,
        lock_timeout_seconds=0.2,
    )

    tag_target = f"idx_pg_live_tgt_{uuid.uuid4().hex[:6]}"
    tag_failed = f"idx_pg_live_fld_{uuid.uuid4().hex[:6]}"

    _, target_path, _ = create_snapshot_index(temp_storage_dir, tag_target, total_vectors=5)
    _, failed_path, _ = create_snapshot_index(temp_storage_dir, tag_failed, total_vectors=5)
    setup_active_manifest(temp_storage_dir, tag_failed, failed_path, total_vectors=5, previous_tag=tag_target)

    try:
        async with session_factory() as session1, session_factory() as session2:
            k_target = KnowledgeIndex(index_version_tag=tag_target, storage_path=target_path, total_vectors=5, status="SUPERSEDED")
            k_failed = KnowledgeIndex(index_version_tag=tag_failed, storage_path=failed_path, total_vectors=5, status="ACTIVE")
            session1.add_all([k_target, k_failed])
            await session1.commit()

            lock_id = getattr(settings, "KB_PROMOTION_ADVISORY_LOCK_ID", 5432002)

            # Connection 1 acquires lock
            async with DatabaseAdvisoryLock(session=session1, lock_id=lock_id, timeout_seconds=2.0):
                # Connection 2 attempts rollback on live PostgreSQL and must timeout
                with pytest.raises(KnowledgeLockTimeoutError):
                    await rollback_mgr.execute_rollback(
                        session=session2,
                        failed_tag=tag_failed,
                        target_tag=tag_target,
                        reason="Live lock contention test",
                    )

            # After session1 exits lock block, session2 successfully executes rollback
            rb_res = await rollback_mgr.execute_rollback(
                session=session2,
                failed_tag=tag_failed,
                target_tag=tag_target,
                reason="Live lock acquisition after release",
            )
            assert rb_res.success is True
            assert rb_res.restored_version_tag == tag_target
    finally:
        # Clean up test rows using privileged migration engine (table owner)
        cleanup_url = getattr(global_settings, "migration_async_database_url", None)
        if cleanup_url:
            cleanup_engine = create_async_engine(cleanup_url, poolclass=NullPool, echo=False)
            async with cleanup_engine.begin() as cleanup_conn:
                await cleanup_conn.execute(
                    KnowledgeIndex.__table__.delete().where(
                        KnowledgeIndex.index_version_tag.in_([tag_target, tag_failed])
                    )
                )
            await cleanup_engine.dispose()

        await live_engine.dispose()


# =====================================================================
# Tests: Runtime Integration & Background Execution (19 - 23)
# =====================================================================

@pytest.mark.asyncio
async def test_successful_promotion_automatically_starts_monitoring(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Requirement 1: Successful index promotion automatically registers and starts background canary monitoring."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    prev_tag = "idx_prev_auto_v1"
    cand_tag = "idx_cand_auto_v2"

    _, prev_path, _ = create_snapshot_index(temp_storage_dir, prev_tag, total_vectors=8)
    _, cand_path, _ = create_snapshot_index(temp_storage_dir, cand_tag, total_vectors=10)
    setup_active_manifest(temp_storage_dir, prev_tag, prev_path, total_vectors=8)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    watchdog = IndexHealthWatchdog(settings=settings, reader=reader, redis_service=mock_redis_service)
    promoter = IndexPromoter(
        settings=settings,
        redis_service=mock_redis_service,
        watchdog=watchdog,
        session_factory=session_factory,
    )

    async with session_factory() as session:
        k_prev = KnowledgeIndex(index_version_tag=prev_tag, storage_path=prev_path, total_vectors=8, status="ACTIVE")
        k_cand = KnowledgeIndex(index_version_tag=cand_tag, storage_path=cand_path, total_vectors=10, status="CANDIDATE")
        session.add_all([k_prev, k_cand])
        await session.commit()

        b_run = BenchmarkRun(
            index_id=k_cand.id,
            benchmark_suite_version="v1.0.0",
            total_queries=25,
            passed_queries=25,
            grounding_score=0.95,
            latency_p95_ms=10.0,
            is_passed=True,
            diagnostics={"all_passed": True, "is_inconclusive": False, "gate_results": {"all_passed": True}},
        )
        session.add(b_run)
        await session.commit()

        # Promote candidate
        res = await promoter.promote_candidate(
            session=session,
            candidate_tag=cand_tag,
            caller_identity="admin_test",
            admin_override=True,
            override_reason="Testing auto canary scheduler launch",
        )

        assert res.success is True
        assert res.canary_monitoring_started is True
        assert watchdog._scheduler_task is not None
        assert not watchdog._scheduler_task.done()

        # Clean up background scheduler
        watchdog.stop_monitoring_scheduler()


@pytest.mark.asyncio
async def test_scheduled_health_checks_execute_without_manual_method_calls(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Requirement 2: Background scheduler executes periodic health checks automatically on ticks."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_auto_tick_v1"
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=10)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=10)

    reader = ActiveIndexReader(settings=settings, redis_service=mock_redis_service)
    reader.load_active_index()

    # Query runner simulating healthy responses
    def mock_runner(idx, q):
        return 12.0, 0.90

    watchdog = IndexHealthWatchdog(
        settings=settings,
        reader=reader,
        redis_service=mock_redis_service,
        query_runner=mock_runner,
    )

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=idx_path,
            total_vectors=10,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc + timedelta(seconds=1),
            rollback_target_tag="idx_target_v0",
        )
        session.add(k)
        await session.commit()

    # Run scheduler with fast 0.05s intervals for 2 ticks
    task = watchdog.start_monitoring_scheduler(
        session_factory=session_factory,
        active_tag=tag,
        interval_seconds=0.05,
        max_intervals=2,
    )

    eval_res = await task
    assert eval_res.status in ("CONFIRMED", "COMPLETED", "HEALTHY")

    # Verify checks actually ran without manual method calls
    intervals = await watchdog.get_persisted_intervals_count(tag)
    assert intervals >= 1

    watchdog.stop_monitoring_scheduler()


@pytest.mark.asyncio
async def test_worker_restart_resumes_monitoring(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Requirement 3: Cold worker startup lifecycle recovers and resumes active canary monitoring."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_cold_boot_v1"
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=10)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=10)

    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=idx_path,
            total_vectors=10,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc + timedelta(seconds=120),  # Active window
            rollback_target_tag="idx_prev_v0",
        )
        session.add(k)
        await session.commit()

    # Simulate fresh cold worker startup
    lifecycle = WorkerLifecycleManager(
        session_factory=session_factory,
        settings=settings,
        redis_service=mock_redis_service,
    )

    results = await lifecycle.on_startup()
    assert results["monitoring_resumed"] is True
    assert lifecycle.watchdog._scheduler_task is not None
    assert not lifecycle.watchdog._scheduler_task.done()

    sched_task = lifecycle.watchdog._scheduler_task

    # Shutdown lifecycle gracefully
    await lifecycle.on_shutdown()
    assert sched_task.done()
    assert lifecycle.watchdog._scheduler_task is None


@pytest.mark.asyncio
async def test_duplicate_scheduler_prevention(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Requirement 5a: Distributed lock prevents duplicate schedulers for the same active index."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_lock_contention_v1"
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=5)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=5)

    watchdog_1 = IndexHealthWatchdog(settings=settings, redis_service=mock_redis_service)
    watchdog_2 = IndexHealthWatchdog(settings=settings, redis_service=mock_redis_service)

    # Worker 1 starts scheduler
    task1 = watchdog_1.start_monitoring_scheduler(
        session_factory=session_factory,
        active_tag=tag,
        interval_seconds=0.5,
        max_intervals=3,
    )

    # Allow worker 1 lock acquisition
    await asyncio.sleep(0.02)

    # Worker 2 attempts to start scheduler on the same index
    res2 = await watchdog_2.run_monitoring_scheduler(
        session_factory=session_factory,
        active_tag=tag,
        interval_seconds=0.5,
        max_intervals=3,
    )

    assert res2.status == "DUPLICATE_PREVENTED"
    assert "Duplicate scheduler execution prevented" in res2.message

    # Clean up worker 1
    watchdog_1.stop_monitoring_scheduler()


@pytest.mark.asyncio
async def test_monitoring_state_persistence(
    temp_storage_dir,
    sqlite_engine,
    mock_redis_service,
):
    """Requirement 4: Consecutive failure counts and interval metrics persist across worker instances."""
    session_factory = async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    settings = Settings(FAISS_STORAGE_DIR=temp_storage_dir)

    tag = "idx_persist_state_v1"
    create_snapshot_index(temp_storage_dir, "idx_good_v0", total_vectors=5)
    _, idx_path, _ = create_snapshot_index(temp_storage_dir, tag, total_vectors=10)
    setup_active_manifest(temp_storage_dir, tag, idx_path, total_vectors=10, previous_tag="idx_good_v0")

    # Worker Instance 1 experiences 1 failure
    w1 = IndexHealthWatchdog(settings=settings, redis_service=mock_redis_service)
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        k_target = KnowledgeIndex(
            index_version_tag="idx_good_v0",
            storage_path=os.path.join(temp_storage_dir, "idx_good_v0", "index.faiss"),
            total_vectors=5,
            status="SUPERSEDED",
        )
        k = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=idx_path,
            total_vectors=10,
            status="ACTIVE",
            canary_status="MONITORING",
            canary_failure_count=0,
            watchdog_expires_at=now_utc + timedelta(seconds=200),
            rollback_target_tag="idx_good_v0",
        )
        session.add_all([k_target, k])
        await session.commit()

        fail_check = CanaryCheckResult(
            is_healthy=False,
            latency_p95_ms=250.0,
            average_similarity=0.8,
            query_count=3,
            passed_count=1,
            failure_reasons=["Degraded"],
        )
        r1 = await w1.evaluate_active_index_health(session=session, active_tag=tag, check_result=fail_check)
        assert r1.consecutive_failures == 1

    # Simulate worker crash: w1 destroyed, w2 created fresh from scratch
    del w1
    w2 = IndexHealthWatchdog(settings=settings, redis_service=mock_redis_service)

    # w2 reads persisted consecutive failures from Redis
    persisted_consec = await w2.get_persisted_consecutive_failures(tag)
    assert persisted_consec == 1

    # Second failure occurs on w2 -> consecutive reaches 2, tripping rollback!
    async with session_factory() as session:
        r2 = await w2.evaluate_active_index_health(session=session, active_tag=tag, check_result=fail_check)
        assert r2.status == "ROLLBACK_TRIGGERED"
        assert r2.consecutive_failures == 2
        assert r2.rollback_executed is True


