"""Live PostgreSQL 16 Integration & Concurrency Tests for KB Ingestion Retries (Task 1 Criterion 10).

Executes directly against live PostgreSQL 16 database ('ai_customer_support') at localhost:5432:
1. Multi-connection concurrent claims using SELECT FOR UPDATE SKIP LOCKED.
2. Tenant-content serialization using transaction-scoped PostgreSQL pg_try_advisory_xact_lock.
3. PostgreSQL native check constraints: 0 <= retry_count <= 3, 0 <= interruption_count <= 3.
4. Composite uniqueness: (tenant_id, idempotency_key).
5. CAS fencing against stale worker commits on live PostgreSQL.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import shutil
import tempfile
import uuid
import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import utc_now
from app.models.document import Document, DocumentChunk, DocumentVersion, KBDocumentIngestionJob
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.retry_service import (
    KBIngestionRetryService,
    compute_idempotency_key,
    compute_content_lock_id,
    KB_PROMOTION_ADVISORY_LOCK_ID,
)


@pytest.fixture
def pg_engine():
    """Provides async engine to live PostgreSQL 16."""
    if not settings.POSTGRES_PASSWORD:
        pytest.skip("POSTGRES_PASSWORD not set; skipping live PostgreSQL tests.")
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool, echo=False)
    yield engine


@pytest.fixture
def session_factory(pg_engine):
    """Provides async sessionmaker on live PostgreSQL."""
    return async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def retry_service():
    """Provides retry service instance."""
    return KBIngestionRetryService()


@pytest.fixture(autouse=True)
async def clean_pg_test_jobs(session_factory):
    """Automatically cleans up all tenant_pg_% test jobs before and after each test for test isolation."""
    async with session_factory() as session:
        await session.execute(text("DELETE FROM kb_document_ingestion_jobs WHERE tenant_id LIKE 'tenant_pg_%'"))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(text("DELETE FROM kb_document_ingestion_jobs WHERE tenant_id LIKE 'tenant_pg_%'"))
        await session.commit()


# =============================================================================
# 1. Live PostgreSQL Check & Unique Constraint Verification
# =============================================================================

@pytest.mark.asyncio
async def test_pg_check_constraints_and_uniqueness(session_factory, retry_service):
    """Verifies that live PostgreSQL rejects retry_count > 3, interruption_count > 3, and duplicate idempotency."""
    tenant = f"tenant_pg_chk_{uuid.uuid4().hex[:6]}"
    code = "DOC-CHK-001"
    sha = hashlib.sha256(b"check_constraint_test").hexdigest()
    idemp = compute_idempotency_key(code, sha)

    # 1. Valid insertion succeeds
    async with session_factory() as session:
        job = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key="docs/check.md",
            idempotency_key=idemp,
            status="RETRY_SCHEDULED",
            failure_reason="Test check",
            retry_count=0,
            interruption_count=0,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # 2. Check retry_count > 3 is rejected by PostgreSQL CHECK constraint
    async with session_factory() as session:
        with pytest.raises(sa.exc.IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE kb_document_ingestion_jobs SET retry_count = 4 WHERE id = :id"),
                {"id": str(job_id)},
            )
            await session.commit()
        assert "chk_kb_ingestion_jobs_retry_count" in str(exc_info.value).lower()

    # 3. Check interruption_count > 3 is rejected by PostgreSQL CHECK constraint
    async with session_factory() as session:
        with pytest.raises(sa.exc.IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE kb_document_ingestion_jobs SET interruption_count = 4 WHERE id = :id"),
                {"id": str(job_id)},
            )
            await session.commit()
        assert "chk_kb_ingestion_jobs_interruption_count" in str(exc_info.value).lower()

    # 4. Check duplicate (tenant_id, idempotency_key) rejected by PostgreSQL UNIQUE constraint
    async with session_factory() as session:
        dup_job = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key="docs/check2.md",
            idempotency_key=idemp,
            status="RETRY_SCHEDULED",
            failure_reason="Duplicate test",
        )
        session.add(dup_job)
        with pytest.raises(sa.exc.IntegrityError) as exc_info:
            await session.commit()
        assert "uq_kb_ingestion_jobs_tenant_idempotency" in str(exc_info.value).lower()


# =============================================================================
# 2. Multi-Connection Concurrent Claims (FOR UPDATE SKIP LOCKED)
# =============================================================================

@pytest.mark.asyncio
async def test_pg_multi_connection_concurrent_claim_skip_locked(session_factory, retry_service):
    """Verifies that two independent workers claiming simultaneously never double-claim on PostgreSQL."""
    tenant = f"tenant_pg_conc_{uuid.uuid4().hex[:6]}"
    code = f"DOC-CONC-{uuid.uuid4().hex[:6]}"
    sha = hashlib.sha256(code.encode()).hexdigest()
    idemp = compute_idempotency_key(code, sha)

    async with session_factory() as session:
        job = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key="docs/conc.md",
            idempotency_key=idemp,
            status="RETRY_SCHEDULED",
            failure_reason="Concurrency test job",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # Launch two independent workers concurrently on separate sessions
    async def worker_claim(worker_name: str):
        async with session_factory() as sess:
            return await retry_service.claim_next_job(sess, worker_name)

    res_w1, res_w2 = await asyncio.gather(
        worker_claim("worker_pg_1"),
        worker_claim("worker_pg_2"),
    )

    # Exactly one worker claimed the candidate
    claims = [r for r in (res_w1, res_w2) if r is not None and r.id == job_id]
    assert len(claims) == 1, f"Expected exactly 1 claim, got {len(claims)}"

    winner = claims[0]
    assert winner.status == "PROCESSING"
    assert winner.locked_by in ("worker_pg_1", "worker_pg_2")
    assert winner.claim_version == 2


# =============================================================================
# 3. PostgreSQL Advisory Lock Serialization for Identical Content
# =============================================================================

@pytest.mark.asyncio
async def test_pg_tenant_content_advisory_lock_serialization(session_factory, retry_service):
    """Verifies that PostgreSQL advisory locks prevent concurrent processing of the same content payload."""
    tenant = f"tenant_pg_adv_{uuid.uuid4().hex[:6]}"
    sha_shared = hashlib.sha256(b"shared_payload_across_different_codes").hexdigest()

    # Job 1 and Job 2 have DIFFERENT document codes but IDENTICAL content hash
    async with session_factory() as session:
        job1 = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code="DOC-ADV-001",
            sha256_hash=sha_shared,
            storage_key="docs/adv1.md",
            idempotency_key=compute_idempotency_key("DOC-ADV-001", sha_shared),
            status="RETRY_SCHEDULED",
            failure_reason="Adv lock test 1",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )
        job2 = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code="DOC-ADV-002",
            sha256_hash=sha_shared,
            storage_key="docs/adv2.md",
            idempotency_key=compute_idempotency_key("DOC-ADV-002", sha_shared),
            status="RETRY_SCHEDULED",
            failure_reason="Adv lock test 2",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )
        session.add_all([job1, job2])
        await session.commit()
        j1_id = job1.id
        j2_id = job2.id

    # Worker 1 claims job 1
    async with session_factory() as sess1:
        claimed_1 = await retry_service.claim_next_job(sess1, "adv_worker_1")
        assert claimed_1 is not None

        # While Job 1 is PROCESSING, Worker 2 tries to claim Job 2 (same tenant & content hash)
        async with session_factory() as sess2:
            claimed_2 = await retry_service.claim_next_job(sess2, "adv_worker_2")
            # Must be skipped because job 1 is actively PROCESSING the same content hash!
            assert claimed_2 is None

        # Worker 1 completes Job 1
        await retry_service.record_success(
            sess1,
            claimed_1.id,
            "adv_worker_1",
            claimed_1.claim_version,
        )

    # Now that Job 1 is COMPLETED, Worker 2 can safely claim Job 2
    async with session_factory() as sess3:
        claimed_2_after = await retry_service.claim_next_job(sess3, "adv_worker_2")
        assert claimed_2_after is not None
        assert claimed_2_after.id == j2_id


# =============================================================================
# 4. PostgreSQL Stale Worker CAS Fencing
# =============================================================================

@pytest.mark.asyncio
async def test_pg_stale_worker_cas_fencing(session_factory, retry_service):
    """Verifies that stale workers on live PostgreSQL are rejected by CAS claim_version check."""
    tenant = f"tenant_pg_fence_{uuid.uuid4().hex[:6]}"
    code = f"DOC-PG-FENCE-{uuid.uuid4().hex[:6]}"
    sha = hashlib.sha256(code.encode()).hexdigest()
    idemp = compute_idempotency_key(code, sha)

    async with session_factory() as session:
        job = KBDocumentIngestionJob(
            tenant_id=tenant,
            document_code=code,
            sha256_hash=sha,
            storage_key="docs/pg_fence.md",
            idempotency_key=idemp,
            status="RETRY_SCHEDULED",
            failure_reason="PG CAS fence test",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # Worker A claims at claim_version = 2
    async with session_factory() as sess_a:
        claimed_a = await retry_service.claim_next_job(sess_a, "worker_pg_A")
        assert claimed_a is not None
        v_a = claimed_a.claim_version

    # Worker B recovers lease and claims at claim_version = 3
    async with session_factory() as sess_rec:
        # Force lease expiration
        await sess_rec.execute(
            text("UPDATE kb_document_ingestion_jobs SET lease_expires_at = :exp, next_retry_at = :nxt WHERE id = :id"),
            {"exp": utc_now() - timedelta(seconds=5), "nxt": utc_now() - timedelta(seconds=5), "id": str(job_id)},
        )
        await sess_rec.commit()

        await retry_service.recover_expired_leases(sess_rec)

    async with session_factory() as sess_b:
        # Reset retry schedule for claim
        await sess_b.execute(
            text("UPDATE kb_document_ingestion_jobs SET next_retry_at = :nxt WHERE id = :id"),
            {"nxt": utc_now() - timedelta(seconds=1), "id": str(job_id)},
        )
        await sess_b.commit()

        claimed_b = await retry_service.claim_next_job(sess_b, "worker_pg_B")
        assert claimed_b is not None
        assert claimed_b.claim_version > v_a

    # Worker A (stale) tries to commit success -> rejected!
    async with session_factory() as sess_stale:
        success_a = await retry_service.record_success(
            sess_stale,
            job_id,
            "worker_pg_A",
            v_a,
        )
        assert success_a is False

    # Worker B (valid) commits success -> succeeds!
    async with session_factory() as sess_valid:
        success_b = await retry_service.record_success(
            sess_valid,
            job_id,
            "worker_pg_B",
            claimed_b.claim_version,
        )
        assert success_b is True


# =============================================================================
# 5. PostgreSQL Native CLOCK_TIMESTAMP() Lease Expiry CAS Rejection
# =============================================================================

@pytest.mark.asyncio
async def test_pg_clock_timestamp_lease_expiry_cas_rejection(session_factory, retry_service):
    """Verifies that record_success evaluates PostgreSQL clock_timestamp() and rejects expired workers."""
    tenant = f"tenant_pg_clock_{uuid.uuid4().hex[:6]}"
    code = f"DOC-PG-CLOCK-{uuid.uuid4().hex[:6]}"
    sha = hashlib.sha256(code.encode()).hexdigest()
    idemp = compute_idempotency_key(code, sha)

    try:
        async with session_factory() as session:
            job = KBDocumentIngestionJob(
                tenant_id=tenant,
                document_code=code,
                sha256_hash=sha,
                storage_key="docs/clock.md",
                idempotency_key=idemp,
                status="RETRY_SCHEDULED",
                failure_reason="Clock test",
                next_retry_at=utc_now() - timedelta(seconds=1),
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        # Worker claims job
        async with session_factory() as sess_claim:
            claimed = await retry_service.claim_next_job(sess_claim, "worker_clock_1")
            assert claimed is not None
            v = claimed.claim_version

        # Expire lease directly in PostgreSQL using clock_timestamp() - interval '2 seconds'
        async with session_factory() as sess_exp:
            await sess_exp.execute(
                text("UPDATE kb_document_ingestion_jobs SET lease_expires_at = clock_timestamp() - interval '2 seconds' WHERE id = :id"),
                {"id": str(job_id)},
            )
            await sess_exp.commit()

        # Worker attempts record_success on live PostgreSQL:
        # WHERE clause evaluates lease_expires_at > clock_timestamp() -> matches 0 rows!
        async with session_factory() as sess_work:
            success = await retry_service.record_success(
                sess_work,
                job_id,
                "worker_clock_1",
                v,
            )
            assert success is False

        # Verify job is still PROCESSING (not COMPLETED)
        async with session_factory() as sess_check:
            res = await sess_check.execute(select(KBDocumentIngestionJob).where(KBDocumentIngestionJob.id == job_id))
            j = res.scalar_one()
            assert j.status == "PROCESSING"
    finally:
        async with session_factory() as sess_clean:
            await sess_clean.execute(text("DELETE FROM kb_document_ingestion_jobs WHERE tenant_id = :t"), {"t": tenant})
            await sess_clean.commit()


# =============================================================================
# 6. PostgreSQL Atomic Rollback of Staged Version and Chunks on Fenced Worker
# =============================================================================

@pytest.mark.asyncio
async def test_pg_execute_job_rollback_on_expired_lease(session_factory, retry_service):
    """Verifies that on live PostgreSQL, failure of CAS lease fencing rolls back DocumentVersion and DocumentChunks."""
    tenant = f"tenant_pg_rb_{uuid.uuid4().hex[:6]}"
    code = f"DOC-PG-RB-{uuid.uuid4().hex[:6]}"
    sha = hashlib.sha256(code.encode()).hexdigest()
    idemp = compute_idempotency_key(code, sha)

    try:
        async with session_factory() as session:
            job = KBDocumentIngestionJob(
                tenant_id=tenant,
                document_code=code,
                sha256_hash=sha,
                storage_key="docs/rb.md",
                idempotency_key=idemp,
                status="RETRY_SCHEDULED",
                failure_reason="Rollback test",
                next_retry_at=utc_now() - timedelta(seconds=1),
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        async with session_factory() as sess_claim:
            claimed = await retry_service.claim_next_job(sess_claim, "worker_rb_1")
            assert claimed is not None
            v = claimed.claim_version

        # Simulate Worker staging Document, DocumentVersion, and DocumentChunks in one session
        async with session_factory() as sess_work:
            # Another process or timeout expires the lease in PostgreSQL
            async with session_factory() as sess_expire:
                await sess_expire.execute(
                    text("UPDATE kb_document_ingestion_jobs SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = :id"),
                    {"id": str(job_id)},
                )
                await sess_expire.commit()

            doc = Document(tenant_id=tenant, document_code=code, title=code, category="POLICY", is_active=True)
            sess_work.add(doc)
            await sess_work.flush()

            ver = DocumentVersion(
                document_id=doc.id,
                version_number=1,
                sha256_hash=sha,
                storage_uri=f"storage://{tenant}/docs/rb.md",
                status="STAGED",
                effective_from=datetime.now(timezone.utc).date(),
                is_current=False,
            )
            sess_work.add(ver)
            await sess_work.flush()

            v_id_res = await sess_work.execute(text("SELECT nextval('document_chunks_faiss_vector_id_seq')"))
            v_id = int(v_id_res.scalar_one())

            chunk = DocumentChunk(
                tenant_id=tenant,
                version_id=ver.id,
                chunk_index=0,
                chunk_text="Test chunk text",
                token_count=10,
                metadata_={},
                faiss_vector_id=v_id,
            )
            sess_work.add(chunk)
            await sess_work.flush()

            # Worker attempts record_success:
            success = await retry_service.record_success(
                sess_work,
                job_id,
                "worker_rb_1",
                v,
                auto_commit=True,
            )
            assert success is False

        # In an independent session, verify DocumentVersion and DocumentChunks were ROLLED BACK
        async with session_factory() as sess_verify:
            v_count = (await sess_verify.execute(
                select(sa.func.count(DocumentVersion.id)).where(DocumentVersion.sha256_hash == sha)
            )).scalar()
            assert v_count == 0

            c_count = (await sess_verify.execute(
                select(sa.func.count(DocumentChunk.id)).where(DocumentChunk.tenant_id == tenant)
            )).scalar()
            assert c_count == 0
    finally:
        async with session_factory() as sess_clean:
            await sess_clean.execute(text("DELETE FROM kb_document_ingestion_jobs WHERE tenant_id = :t"), {"t": tenant})
            await sess_clean.commit()


# =============================================================================
# 7. PostgreSQL Candidate Writer vs Cleanup Synchronization (Lock 5432002)
# =============================================================================

@pytest.mark.asyncio
async def test_pg_candidate_writer_vs_cleanup_synchronization_lock_5432002(session_factory, retry_service):
    """Verifies that cleanup on live PostgreSQL respects active BUILDING candidate records and advisory lock 5432002."""
    tag = f"idx_pg_sync_{uuid.uuid4().hex[:6]}"
    temp_dir = tempfile.mkdtemp()
    storage_path = Path(temp_dir)

    try:
        scratch_dir = storage_path / f".building_{tag}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        (scratch_dir / "index.faiss").write_text("pg_faiss_data", encoding="utf-8")

        # 1. Register active BUILDING candidate with valid lease
        async with session_factory() as session:
            k_idx = KnowledgeIndex(
                index_version_tag=tag,
                storage_path=str(storage_path / f"candidate_{tag}" / "index.faiss"),
                total_vectors=0,
                status="BUILDING",
                watchdog_expires_at=utc_now() + timedelta(minutes=5),
            )
            session.add(k_idx)
            await session.commit()

        # 2. Run cleanup with lock 5432002 held by another transaction -> must skip cleanly
        async with session_factory() as lock_sess:
            lock_res = await lock_sess.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": KB_PROMOTION_ADVISORY_LOCK_ID},
            )
            assert bool(lock_res.scalar_one()) is True

            # In another session, run cleanup:
            async with session_factory() as clean_sess:
                res_skip = await retry_service.cleanup_unreferenced_scratch_dirs(
                    clean_sess,
                    storage_path,
                    max_age_seconds=0.0,
                )
                assert res_skip.get("status") == "skipped"
                assert res_skip.get("reason") == "promotion_active"
                assert scratch_dir.exists()

        # 3. Lock released. Run cleanup with unexpired lease -> active writer directory must be protected!
        async with session_factory() as clean_sess2:
            res_prot = await retry_service.cleanup_unreferenced_scratch_dirs(
                clean_sess2,
                storage_path,
                max_age_seconds=0.0,
            )
            assert scratch_dir.exists()
            assert res_prot.get("cleaned") == 0

        # 4. Expire lease in PostgreSQL
        async with session_factory() as exp_sess:
            await exp_sess.execute(
                text("UPDATE knowledge_indexes SET watchdog_expires_at = clock_timestamp() - interval '5 seconds' WHERE index_version_tag = :tag"),
                {"tag": tag},
            )
            await exp_sess.commit()

        # 5. Re-run cleanup: now cleans up expired scratch directory and updates status to ABANDONED
        async with session_factory() as clean_sess3:
            res_clean = await retry_service.cleanup_unreferenced_scratch_dirs(
                clean_sess3,
                storage_path,
                max_age_seconds=0.0,
            )
            assert not scratch_dir.exists()
            assert res_clean.get("cleaned") == 1

            k_post = (await clean_sess3.execute(
                select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == tag)
            )).scalar_one()
            assert k_post.status == "ABANDONED"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        async with session_factory() as clean_db:
            await clean_db.execute(
                text("UPDATE knowledge_indexes SET status = 'ABANDONED', watchdog_expires_at = NULL WHERE index_version_tag = :tag"),
                {"tag": tag},
            )
            await clean_db.commit()

