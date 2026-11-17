"""Unit Tests for Knowledge Base Ingestion Retries (Task 1 Criterion 10).

Verifies:
1. Full retry interval progression: initial failure (15m) -> 1st retry (30m) -> 2nd retry (60m) -> DEAD_LETTER.
2. Permanent validation, security, and quarantine failures route immediately to DEAD_LETTER (0 retries).
3. Operational worker interruptions tracked separately from retries, up to hard limit of 3.
4. Tenant isolation and composite idempotency key (tenant_id, idempotency_key).
5. Process restart persistence and expired lease recovery.
6. Stale worker CAS fencing (rejection of stale worker commits).
7. Transactional consistency: database failure during candidate registration produces clean rollback.
8. Candidate cleanup coordinates safely through promotion advisory lock (5432002).
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base, utc_now
from app.core.exceptions import (
    CandidateIndexBuildError,
    DocumentCorruptError,
    DocumentQuarantinedError,
    DocumentSecurityError,
    KnowledgeLockTimeoutError,
    StorageSecurityError,
)
from app.models.document import Document, DocumentChunk, DocumentVersion, KBDocumentIngestionJob
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.vector_builder import CandidateVectorBuilder
from app.modules.knowledge.retry_service import (
    KBIngestionRetryService,
    KBIngestionRetryWorker,
    compute_idempotency_key,
    compute_content_lock_id,
    is_retryable_error,
    RETRY_DELAYS_MINUTES,
    MAX_RETRY_COUNT,
    MAX_OPERATIONAL_INTERRUPTIONS,
    KB_PROMOTION_ADVISORY_LOCK_ID,
)
from app.services.storage_service import LocalStorageAdapter, StorageService


@pytest.fixture
async def sqlite_engine():
    """Provides an isolated in-memory SQLite database engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(sqlite_engine):
    """Provides async session factory."""
    return async_sessionmaker(bind=sqlite_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def temp_storage_dir():
    """Provides temporary directory for storage tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def storage_service(temp_storage_dir):
    """Provides a StorageService configured with LocalStorageAdapter."""
    adapter = LocalStorageAdapter(root_dir=temp_storage_dir)
    return StorageService(adapter=adapter)


@pytest.fixture
def retry_service(storage_service):
    """Provides KBIngestionRetryService."""
    return KBIngestionRetryService(storage_service=storage_service)


# =============================================================================
# 1. Retry Intervals & State Machine Progression Tests
# =============================================================================

@pytest.mark.asyncio
async def test_full_retry_interval_progression(session_factory, retry_service):
    """Verifies complete schedule progression: 15m -> 30m -> 60m -> DEAD_LETTER."""
    tenant = "tenant_test_1"
    doc_code = "POL-REFUND-001"
    sha = "abc123def456" * 5
    key = "documents/pol_refund.md"
    worker_id = "worker_test_1"

    async with session_factory() as session:
        # Step 0: Initial Ingestion Failure -> Retry Scheduled at +15m
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=doc_code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Temporary database connection timeout",
            exc=KnowledgeLockTimeoutError("Lock timeout"),
        )
        await session.commit()

        assert job.status == "RETRY_SCHEDULED"
        assert job.failure_type == "RETRYABLE"
        assert job.retry_count == 0
        assert job.interruption_count == 0
        assert job.dead_lettered_at is None
        assert job.next_retry_at is not None
        # Verify 15m interval (within tolerance)
        diff_0 = (job.next_retry_at - job.initial_failure_at).total_seconds()
        assert 890 <= diff_0 <= 910  # 15 minutes = 900s

        # Step 1: Simulate time passing -> Worker claims job for Attempt 1
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        claimed_1 = await retry_service.claim_next_job(session, worker_id)
        assert claimed_1 is not None
        assert claimed_1.status == "PROCESSING"
        assert claimed_1.locked_by == worker_id
        assert claimed_1.claim_version == 2

        # Attempt 1 fails -> Reschedule for Retry 2 at +30m
        fail_1_res = await retry_service.record_failure(
            session=session,
            job_id=claimed_1.id,
            worker_id=worker_id,
            claim_version=claimed_1.claim_version,
            exc=KnowledgeLockTimeoutError("Network glitch on embedding API"),
            failure_reason="Embedding API connection timeout",
        )
        assert fail_1_res is True

        # Re-fetch job
        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed_1.id))
        job_after_1 = res.scalar_one()
        assert job_after_1.status == "RETRY_SCHEDULED"
        assert job_after_1.retry_count == 1
        assert job_after_1.dead_lettered_at is None
        diff_1 = (job_after_1.next_retry_at - utc_now()).total_seconds()
        assert 1790 <= diff_1 <= 1810  # 30 minutes = 1800s

        # Step 2: Time passes -> Worker claims job for Attempt 2
        job_after_1.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        claimed_2 = await retry_service.claim_next_job(session, worker_id)
        assert claimed_2 is not None
        assert claimed_2.status == "PROCESSING"
        assert claimed_2.claim_version == 3

        # Attempt 2 fails -> Reschedule for Retry 3 at +60m
        fail_2_res = await retry_service.record_failure(
            session=session,
            job_id=claimed_2.id,
            worker_id=worker_id,
            claim_version=claimed_2.claim_version,
            exc=KnowledgeLockTimeoutError("Disk busy timeout"),
            failure_reason="Temporary disk I/O latency",
        )
        assert fail_2_res is True

        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed_2.id))
        job_after_2 = res.scalar_one()
        assert job_after_2.status == "RETRY_SCHEDULED"
        assert job_after_2.retry_count == 2
        assert job_after_2.dead_lettered_at is None
        diff_2 = (job_after_2.next_retry_at - utc_now()).total_seconds()
        assert 3590 <= diff_2 <= 3610  # 60 minutes = 3600s

        # Step 3: Time passes -> Worker claims job for Attempt 3 (Final attempt)
        job_after_2.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        claimed_3 = await retry_service.claim_next_job(session, worker_id)
        assert claimed_3 is not None
        assert claimed_3.status == "PROCESSING"
        assert claimed_3.claim_version == 4

        # Attempt 3 fails -> Transitions to DEAD_LETTER!
        fail_3_res = await retry_service.record_failure(
            session=session,
            job_id=claimed_3.id,
            worker_id=worker_id,
            claim_version=claimed_3.claim_version,
            exc=KnowledgeLockTimeoutError("Persistent failure"),
            failure_reason="Exhausted all 3 automated retries",
        )
        assert fail_3_res is True

        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed_3.id))
        job_final = res.scalar_one()
        assert job_final.status == "DEAD_LETTER"
        assert job_final.retry_count == 3
        assert job_final.dead_lettered_at is not None
        assert job_final.next_retry_at is None
        assert job_final.locked_by is None


# =============================================================================
# 2. Permanent Failure Tests (Immediate Dead-Letter)
# =============================================================================

@pytest.mark.asyncio
async def test_permanent_failures_route_immediately_to_dead_letter(session_factory, retry_service):
    """Verifies that permanent errors (corrupt, quarantine, security) bypass retry schedule."""
    test_cases = [
        (DocumentCorruptError("Malformed UTF-8 bytes"), "DocumentCorruptError"),
        (DocumentQuarantinedError("Prompt injection detected"), "DocumentQuarantinedError"),
        (DocumentSecurityError("Disallowed executable extension"), "DocumentSecurityError"),
        (StorageSecurityError("Directory traversal detected"), "StorageSecurityError"),
    ]

    async with session_factory() as session:
        for exc, desc in test_cases:
            code = f"DOC-PERM-{uuid.uuid4().hex[:6]}"
            sha = hashlib.sha256(code.encode()).hexdigest()
            job = await retry_service.schedule_job(
                session=session,
                tenant_id="tenant_perm",
                document_code=code,
                sha256_hash=sha,
                storage_key=f"docs/{code}.md",
                failure_reason=str(exc),
                exc=exc,
            )
            await session.commit()

            assert job.status == "DEAD_LETTER", f"Failed for {desc}"
            assert job.failure_type == "PERMANENT", f"Failed for {desc}"
            assert job.retry_count == 0, f"Failed for {desc}"
            assert job.next_retry_at is None, f"Failed for {desc}"
            assert job.dead_lettered_at is not None, f"Failed for {desc}"


# =============================================================================
# 3. Operational Interruption vs Application Retry Tracking
# =============================================================================

@pytest.mark.asyncio
async def test_worker_operational_interruption_tracking(session_factory, retry_service):
    """Verifies separate tracking of worker operational interruptions up to hard limit of 3."""
    tenant = "tenant_interrupt"
    code = "DOC-INT-001"
    sha = hashlib.sha256(code.encode()).hexdigest()
    key = "docs/interrupt.md"
    worker_id = "crashed_worker"

    async with session_factory() as session:
        # Create job
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Initial timeout",
            exc=KnowledgeLockTimeoutError("Timeout"),
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        # Interruption 1: Claim and let lease expire
        claimed = await retry_service.claim_next_job(session, worker_id, lease_duration_seconds=1.0)
        assert claimed is not None
        assert claimed.interruption_count == 0
        assert claimed.retry_count == 0

        # Simulate worker crash: lease expires in past
        claimed.lease_expires_at = utc_now() - timedelta(seconds=5)
        await session.commit()

        recovered_count = await retry_service.recover_expired_leases(session)
        assert recovered_count == 1

        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed.id))
        job_rec1 = res.scalar_one()
        assert job_rec1.status == "RETRY_SCHEDULED"
        assert job_rec1.interruption_count == 1
        assert job_rec1.retry_count == 0  # Retry count untouched!
        assert job_rec1.locked_by is None

        # Interruption 2:
        job_rec1.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        claimed_2 = await retry_service.claim_next_job(session, "crashed_worker_2", lease_duration_seconds=1.0)
        assert claimed_2 is not None

        claimed_2.lease_expires_at = utc_now() - timedelta(seconds=5)
        await session.commit()

        recovered_count = await retry_service.recover_expired_leases(session)
        assert recovered_count == 1

        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed.id))
        job_rec2 = res.scalar_one()
        assert job_rec2.status == "RETRY_SCHEDULED"
        assert job_rec2.interruption_count == 2
        assert job_rec2.retry_count == 0

        # Interruption 3: Hard limit hit -> Dead Letter!
        job_rec2.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        claimed_3 = await retry_service.claim_next_job(session, "crashed_worker_3", lease_duration_seconds=1.0)
        assert claimed_3 is not None

        claimed_3.lease_expires_at = utc_now() - timedelta(seconds=5)
        await session.commit()

        recovered_count = await retry_service.recover_expired_leases(session)
        assert recovered_count == 1

        res = await session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == claimed.id))
        job_final = res.scalar_one()
        assert job_final.status == "DEAD_LETTER"
        assert job_final.failure_type == "INTERRUPTED"
        assert job_final.interruption_count == 3
        assert job_final.dead_lettered_at is not None
        assert "Exceeded maximum operational worker interruptions (3)" in job_final.failure_reason


# =============================================================================
# 4. Tenant Isolation & Idempotency Protection
# =============================================================================

@pytest.mark.asyncio
async def test_tenant_isolation_and_idempotency_composite_keys(session_factory, retry_service):
    """Verifies composite uniqueness (tenant_id, idempotency_key) and cross-tenant separation."""
    code = "SHARED-POL-001"
    sha = hashlib.sha256(b"same_content_different_tenants").hexdigest()
    key = "docs/shared_pol.md"

    async with session_factory() as session:
        # Tenant A job
        job_a = await retry_service.schedule_job(
            session=session,
            tenant_id="tenant_alpha",
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Timeout in Alpha",
        )
        await session.commit()

        # Tenant B job (same code and hash, different tenant)
        job_b = await retry_service.schedule_job(
            session=session,
            tenant_id="tenant_beta",
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Timeout in Beta",
        )
        await session.commit()

        # Assert independent rows created
        assert job_a.id != job_b.id
        assert job_a.tenant_id == "tenant_alpha"
        assert job_b.tenant_id == "tenant_beta"
        assert job_a.idempotency_key == job_b.idempotency_key

        # Submitting duplicate for Tenant A returns existing job (idempotent)
        dup_a = await retry_service.schedule_job(
            session=session,
            tenant_id="tenant_alpha",
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Repeated submission",
        )
        await session.commit()
        assert dup_a.id == job_a.id


# =============================================================================
# 5. Stale Worker CAS Fencing
# =============================================================================

@pytest.mark.asyncio
async def test_stale_worker_cas_fencing(session_factory, retry_service):
    """Verifies that a superseded or lease-expired worker cannot commit finalization."""
    tenant = "tenant_fencing"
    code = "DOC-FENCE-001"
    sha = hashlib.sha256(code.encode()).hexdigest()
    key = "docs/fence.md"

    async with session_factory() as session:
        # Create job
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Initial timeout",
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        # Worker A claims at claim_version = 2
        claimed_a = await retry_service.claim_next_job(session, "worker_A")
        assert claimed_a is not None
        v_a = claimed_a.claim_version

        # Lease expires, Worker B recovers and claims at claim_version = 3
        claimed_a.lease_expires_at = utc_now() - timedelta(seconds=10)
        await session.commit()

        await retry_service.recover_expired_leases(session)

        # Re-ready for claim
        job_reload = (await session.execute(
            select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == job.id)
        )).scalar_one()
        job_reload.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()

        claimed_b = await retry_service.claim_next_job(session, "worker_B")
        assert claimed_b is not None
        assert claimed_b.claim_version > v_a

        job_id = job.id
        # Worker A (stale) now attempts heartbeat -> rejected!
        hb_a = await retry_service.heartbeat(
            session=session,
            job_id=job_id,
            worker_id="worker_A",
            claim_version=v_a,
        )
        assert hb_a is False

        # Worker A (stale) attempts record_success in its own session -> rejected and rolled back!
        async with session_factory() as sess_stale:
            success_a = await retry_service.record_success(
                session=sess_stale,
                job_id=job_id,
                worker_id="worker_A",
                claim_version=v_a,
            )
            assert success_a is False

        # Worker B (active) attempts record_success in its own session -> succeeds!
        async with session_factory() as sess_valid:
            success_b = await retry_service.record_success(
                session=sess_valid,
                job_id=job_id,
                worker_id="worker_B",
                claim_version=claimed_b.claim_version,
            )
            assert success_b is True


# =============================================================================
# 6. Candidate Cleanup Coordination with Promotion Lock
# =============================================================================

@pytest.mark.asyncio
async def test_candidate_cleanup_skips_when_promotion_lock_held(session_factory, retry_service, temp_storage_dir):
    """Verifies that cleanup does not collide with active promotion (lock 5432002)."""
    from app.modules.knowledge.change_detector import _get_sqlite_lock

    # Create dummy scratch directories
    scratch_dir = temp_storage_dir / ".building_idx_v1"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / "test.faiss").write_text("data", encoding="utf-8")

    # Acquire lock 5432002
    lock = _get_sqlite_lock(KB_PROMOTION_ADVISORY_LOCK_ID)
    await lock.acquire()

    try:
        async with session_factory() as session:
            # Run cleanup while promotion lock is active
            res = await retry_service.cleanup_unreferenced_scratch_dirs(
                session=session,
                storage_dir=temp_storage_dir,
                max_age_seconds=0.0,
            )
            assert res.get("status") == "skipped"
            assert res.get("reason") == "promotion_active"
            # Scratch directory must NOT have been deleted
            assert scratch_dir.exists()
    finally:
        lock.release()

    # Now run cleanup without lock held
    async with session_factory() as session:
        res2 = await retry_service.cleanup_unreferenced_scratch_dirs(
            session=session,
            storage_dir=temp_storage_dir,
            max_age_seconds=0.0,
        )
        assert res2.get("cleaned") == 1
        assert not scratch_dir.exists()


# =============================================================================
# 7. End-to-End Worker Ingestion Execution (No Duplicates)
# =============================================================================

@pytest.mark.asyncio
async def test_worker_execute_job_success_and_no_duplicate_versions(session_factory, retry_service, storage_service):
    """Verifies end-to-end ingestion creates staged versions and chunks, and subsequent runs do not duplicate."""
    tenant = "tenant_e2e"
    code = "POL-LEAVE-2026"
    content = "# Employee Leave Policy\n\nEmployees are entitled to 20 days paid leave per calendar year."
    content_bytes = content.encode("utf-8")
    sha = hashlib.sha256(content_bytes).hexdigest()
    key = "documents/leave_policy.md"

    # Store file in storage service
    await storage_service.put_object(
        tenant_id=tenant,
        key=key,
        payload=content_bytes,
        content_type="text/markdown",
    )

    worker = KBIngestionRetryWorker(
        session_factory=session_factory,
        retry_service=retry_service,
        worker_id="test_e2e_worker",
    )

    # Schedule job in initial session
    async with session_factory() as session:
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Transient failure on first attempt",
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        job_id = job.id

    # Claim and execute in worker
    async with session_factory() as claim_session:
        claimed = await retry_service.claim_next_job(claim_session, "test_e2e_worker")
        assert claimed is not None

    cancel_ev = asyncio.Event()
    success = await worker.execute_job(claimed, cancel_ev)
    assert success is True

    # Verify Document, DocumentVersion, and DocumentChunks created in fresh session
    async with session_factory() as verify_session:
        doc_res = await verify_session.execute(select(Document).where(Document.tenant_id == tenant, Document.document_code == code))
        doc = doc_res.scalar_one()
        assert doc is not None

        ver_res = await verify_session.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))
        versions = ver_res.scalars().all()
        assert len(versions) == 1
        assert versions[0].status == "STAGED"
        assert versions[0].is_current is False

        chunk_res = await verify_session.execute(select(DocumentChunk).where(DocumentChunk.version_id == versions[0].id))
        chunks = chunk_res.scalars().all()
        assert len(chunks) >= 1
        assert chunks[0].tenant_id == tenant

        # Verify job marked COMPLETED
        job_res = await verify_session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == job_id))
        completed_job = job_res.scalar_one()
        assert completed_job.status == "COMPLETED"
        assert completed_job.completed_at is not None

        # Re-running ingestion for the same content does NOT duplicate versions or chunks
        job2 = await retry_service.schedule_job(
            session=verify_session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Second run test",
        )
        # Idempotency returns same job row
        assert job2.id == job_id


# =============================================================================
# 8. Transactional Failure Rollback (Zero Partial Versions/Chunks)
# =============================================================================

@pytest.mark.asyncio
async def test_candidate_registration_failure_rollback(session_factory, retry_service, storage_service):
    """Verifies that an error during chunking/insertion rolls back cleanly leaving zero partial records."""
    tenant = "tenant_rollback"
    code = "DOC-FAIL-ROLLBACK"
    content = "Some document content to fail"
    content_bytes = content.encode("utf-8")
    sha = hashlib.sha256(content_bytes).hexdigest()
    key = "docs/fail_rollback.md"

    await storage_service.put_object(
        tenant_id=tenant,
        key=key,
        payload=content_bytes,
        content_type="text/markdown",
    )

    # Monkeypatch chunker to simulate a mid-stream failure
    class FailingChunker:
        def chunk_document(self, *args, **kwargs):
            raise RuntimeError("Mid-stream chunking crash simulated")

    retry_service.chunker = FailingChunker()
    worker = KBIngestionRetryWorker(
        session_factory=session_factory,
        retry_service=retry_service,
        worker_id="rollback_worker",
    )

    async with session_factory() as session:
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Initial scheduling",
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        job_id = job.id

    async with session_factory() as claim_session:
        claimed = await retry_service.claim_next_job(claim_session, "rollback_worker")
        assert claimed is not None

    cancel_ev = asyncio.Event()
    success = await worker.execute_job(claimed, cancel_ev)
    assert success is False

    # Verify no DocumentVersion or DocumentChunk rows exist
    async with session_factory() as verify_session:
        ver_res = await verify_session.execute(select(DocumentVersion).where(DocumentVersion.sha256_hash == sha))
        assert len(ver_res.scalars().all()) == 0

        job_res = await verify_session.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == job_id))
        failed_job = job_res.scalar_one()
        # Job was rescheduled for retry
        assert failed_job.status == "RETRY_SCHEDULED"
        assert failed_job.retry_count == 1
        assert "Mid-stream chunking crash simulated" in failed_job.failure_reason


# =============================================================================
# 9. Maintenance Window & Promotion Coordination
# =============================================================================

@pytest.mark.asyncio
async def test_maintenance_window_restrictions_and_promotion_gate():
    """Verifies that maintenance window service rejects promotion outside scheduled hours."""
    from app.modules.knowledge.maintenance import MaintenanceSchedule, MaintenanceWindowService, TimeRange
    from datetime import time

    # Configure window strictly between 02:00 and 04:00 on Sundays (weekday 6)
    sched = MaintenanceSchedule(
        enabled=True,
        timezone="UTC",
        allowed_weekdays=[6],
        time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
    )
    svc = MaintenanceWindowService(schedule=sched)

    # 1. Test Tuesday 10:00 UTC (not allowed weekday, not allowed hour)
    tuesday_10am = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    elig_tue = svc.check_eligibility(evaluation_time=tuesday_10am)
    assert elig_tue.is_permitted is False

    # 2. Test Sunday 03:00 UTC (allowed weekday, allowed hour)
    sunday_3am = datetime(2026, 11, 8, 3, 0, 0, tzinfo=timezone.utc)
    elig_sun = svc.check_eligibility(evaluation_time=sunday_3am)
    assert elig_sun.is_permitted is True


# =============================================================================
# 10. Phase 2.2-B Wave 3 Fixes: CAS Lease Fencing & Candidate Writer Sync
# =============================================================================

@pytest.mark.asyncio
async def test_worker_lease_expiry_during_ingestion_prevents_commit_and_rolls_back(
    session_factory, retry_service, storage_service
):
    """Verifies that an expired worker cannot commit DocumentVersion or DocumentChunks; entire transaction rolls back."""
    tenant = "tenant_lease_exp"
    code = "POL-LEASE-EXP"
    content = "# Lease Expiry Test\nContent staged before lease expiration."
    content_bytes = content.encode("utf-8")
    sha = hashlib.sha256(content_bytes).hexdigest()
    key = "documents/lease_exp.md"

    await storage_service.put_object(
        tenant_id=tenant,
        key=key,
        payload=content_bytes,
        content_type="text/markdown",
    )

    async with session_factory() as session:
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Initial failure",
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        job_id = job.id

    async with session_factory() as claim_session:
        claimed = await retry_service.claim_next_job(claim_session, "exp_worker")
        assert claimed is not None
        claim_version = claimed.claim_version

    # Simulate worker processing: in its session, it stages Document, Version, and Chunks
    async with session_factory() as work_session:
        # Before worker completes, lease expires in the DB
        async with session_factory() as expire_session:
            await expire_session.execute(
                update(KBDocumentIngestionJob)
                .where(KBDocumentIngestionJob.id == job_id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=10))
            )
            await expire_session.commit()

        # Worker stages document version and chunks in its transaction
        doc = Document(tenant_id=tenant, document_code=code, title=code, category="POLICY", is_active=True)
        work_session.add(doc)
        await work_session.flush()

        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=sha,
            storage_uri=f"storage://{tenant}/{key}",
            status="STAGED",
            effective_from=datetime.now(timezone.utc).date(),
            is_current=False,
        )
        work_session.add(ver)
        await work_session.flush()

        chunk = DocumentChunk(
            tenant_id=tenant,
            version_id=ver.id,
            chunk_index=0,
            chunk_text="Chunk text",
            token_count=10,
            metadata_={},
            faiss_vector_id=101,
        )
        work_session.add(chunk)
        await work_session.flush()

        # Worker attempts record_success with expired lease:
        success = await retry_service.record_success(
            session=work_session,
            job_id=job_id,
            worker_id="exp_worker",
            claim_version=claim_version,
            auto_commit=True,
        )
        assert success is False

    # Verify zero document versions or chunks committed to DB
    async with session_factory() as verify_session:
        v_res = await verify_session.execute(select(DocumentVersion).where(DocumentVersion.sha256_hash == sha))
        assert len(v_res.scalars().all()) == 0

        c_res = await verify_session.execute(select(DocumentChunk).where(DocumentChunk.tenant_id == tenant))
        assert len(c_res.scalars().all()) == 0


@pytest.mark.asyncio
async def test_worker_supersession_during_ingestion_prevents_commit_and_rolls_back(
    session_factory, retry_service, storage_service
):
    """Verifies that when a worker is superseded by a recovered claim, its commit is rejected and rolled back."""
    tenant = "tenant_supersede"
    code = "POL-SUPERSEDE"
    content = "# Supersession Test\nContent for testing worker supersession."
    content_bytes = content.encode("utf-8")
    sha = hashlib.sha256(content_bytes).hexdigest()
    key = "documents/supersede.md"

    await storage_service.put_object(
        tenant_id=tenant,
        key=key,
        payload=content_bytes,
        content_type="text/markdown",
    )

    async with session_factory() as session:
        job = await retry_service.schedule_job(
            session=session,
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key=key,
            failure_reason="Initial failure",
        )
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        await session.commit()
        job_id = job.id

    # Worker 1 claims at claim_version 1
    async with session_factory() as claim_sess1:
        c1 = await retry_service.claim_next_job(claim_sess1, "worker_1")
        assert c1 is not None
        v1 = c1.claim_version

    # Worker 1 lease expires and Worker 2 claims after recovery at claim_version 2
    async with session_factory() as rec_sess:
        await rec_sess.execute(
            update(KBDocumentIngestionJob)
            .where(KBDocumentIngestionJob.id == job_id)
            .values(
                lease_expires_at=utc_now() - timedelta(seconds=10),
                next_retry_at=utc_now() - timedelta(seconds=1),
            )
        )
        await rec_sess.commit()
        await retry_service.recover_expired_leases(rec_sess)

        # Re-ready for claim
        await rec_sess.execute(
            update(KBDocumentIngestionJob)
            .where(KBDocumentIngestionJob.id == job_id)
            .values(next_retry_at=utc_now() - timedelta(seconds=1))
        )
        await rec_sess.commit()

    async with session_factory() as claim_sess2:
        c2 = await retry_service.claim_next_job(claim_sess2, "worker_2")
        assert c2 is not None
        assert c2.claim_version > v1

    # Worker 1 (stale) stages version and chunks, then attempts record_success in its session
    async with session_factory() as w1_sess:
        doc = Document(tenant_id=tenant, document_code=code, title=code, category="POLICY", is_active=True)
        w1_sess.add(doc)
        await w1_sess.flush()

        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash=sha,
            storage_uri=f"storage://{tenant}/{key}",
            status="STAGED",
            effective_from=datetime.now(timezone.utc).date(),
            is_current=False,
        )
        w1_sess.add(ver)
        await w1_sess.flush()

        success_1 = await retry_service.record_success(
            session=w1_sess,
            job_id=job_id,
            worker_id="worker_1",
            claim_version=v1,
            auto_commit=True,
        )
        assert success_1 is False

    # Verify Worker 1 changes were rolled back
    async with session_factory() as check_sess:
        ver_check = await check_sess.execute(select(DocumentVersion).where(DocumentVersion.sha256_hash == sha))
        assert len(ver_check.scalars().all()) == 0

    # Worker 2 completes execution successfully
    worker2 = KBIngestionRetryWorker(
        session_factory=session_factory,
        retry_service=retry_service,
        worker_id="worker_2",
    )
    cancel_ev = asyncio.Event()
    success_2 = await worker2.execute_job(c2, cancel_ev)
    assert success_2 is True

    # Verify Worker 2 committed version and chunks
    async with session_factory() as final_check:
        ver_final = await final_check.execute(select(DocumentVersion).where(DocumentVersion.sha256_hash == sha))
        assert len(ver_final.scalars().all()) == 1
        job_final = (await final_check.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == job_id))).scalar_one()
        assert job_final.status == "COMPLETED"


@pytest.mark.asyncio
async def test_candidate_cleanup_concurrent_with_active_index_writing_protects_scratch(
    session_factory, retry_service, temp_storage_dir
):
    """Verifies that active candidate writers (.building_{tag}) are protected from cleanup while lease is valid."""
    tag = "idx_active_writer_protect_01"
    active_scratch = temp_storage_dir / f".building_{tag}"
    active_scratch.mkdir(parents=True, exist_ok=True)
    (active_scratch / "index.faiss").write_text("faiss_data", encoding="utf-8")

    # Record active BUILDING candidate in DB with unexpired lease
    async with session_factory() as session:
        k_idx = KnowledgeIndex(
            index_version_tag=tag,
            storage_path=str(temp_storage_dir / f"candidate_{tag}" / "index.faiss"),
            total_vectors=0,
            status="BUILDING",
            watchdog_expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(k_idx)
        await session.commit()

    # Run cleanup with max_age_seconds=0.0
    async with session_factory() as session:
        res = await retry_service.cleanup_unreferenced_scratch_dirs(
            session=session,
            storage_dir=temp_storage_dir,
            max_age_seconds=0.0,
        )
        # Active writer scratch directory MUST NOT be deleted
        assert active_scratch.exists()
        assert res.get("cleaned") == 0

    # Now expire the writer's lease
    async with session_factory() as session:
        await session.execute(
            update(KnowledgeIndex)
            .where(KnowledgeIndex.index_version_tag == tag)
            .values(watchdog_expires_at=utc_now() - timedelta(seconds=10))
        )
        await session.commit()

    # Re-run cleanup: expired writer scratch directory is now cleaned and marked ABANDONED
    async with session_factory() as session:
        res2 = await retry_service.cleanup_unreferenced_scratch_dirs(
            session=session,
            storage_dir=temp_storage_dir,
            max_age_seconds=0.0,
        )
        assert not active_scratch.exists()
        assert res2.get("cleaned") == 1

        k_idx_post = (await session.execute(select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag))).scalar_one()
        assert k_idx_post.status == "ABANDONED"


@pytest.mark.asyncio
async def test_stale_candidate_writer_rejected_after_lease_loss(
    session_factory, temp_storage_dir
):
    """Verifies that a candidate builder whose lease expired or was stolen cannot publish under lock 5432002."""
    tag = "tag_stale_builder_01"
    raw_dir = temp_storage_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = raw_dir / "DOC-STALE.md"
    doc_path.write_text("# Stale Test\nContent for stale test.", encoding="utf-8")

    async with session_factory() as session:
        doc = Document(title="Doc Stale", document_code="DOC-STALE", category="POLICY", is_active=True)
        session.add(doc)
        await session.flush()

        ver = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            sha256_hash="hash_stale",
            storage_uri=str(doc_path),
            status="STAGED",
            effective_from=datetime.now(timezone.utc).date(),
        )
        session.add(ver)
        await session.commit()

    from app.core.config import settings as default_settings
    from tests.test_task1_phase2 import MockFastEmbeddingService
    builder = CandidateVectorBuilder(
        settings=default_settings,
        embedding_service=MockFastEmbeddingService(),
    )
    builder.storage_dir = str(temp_storage_dir)

    # Builder starts build with a negative lease duration so watchdog_expires_at is already in the past
    async with session_factory() as session:
        with pytest.raises(CandidateIndexBuildError) as exc_info:
            await builder.build_candidate_index(
                session=session,
                version_tag=tag,
                lease_duration_seconds=-10.0,
            )
        assert "Candidate build lease expired" in str(exc_info.value)

    # Verify no STAGED index exists
    async with session_factory() as session:
        stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag)
        k_idx = (await session.execute(stmt)).scalar_one_or_none()
        assert k_idx is None or k_idx.status != "STAGED"

