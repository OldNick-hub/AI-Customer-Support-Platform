"""Test Durable Watchdog State Persistence and Worker Restart Recovery."""

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_index import KnowledgeIndex


@pytest.mark.asyncio
async def test_watchdog_state_persistence_and_transitions(session: AsyncSession):
    """Verifies that watchdog timer, canary counts, and status persist across transactions."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=300)  # 5-minute watchdog window

    idx = KnowledgeIndex(
        index_version_tag="idx_test_canary_v1",
        storage_path="/storage/indices/idx_test_canary_v1.index",
        total_vectors=5000,
        status="ACTIVE",
        watchdog_expires_at=expires,
        canary_failure_count=0,
        canary_status="MONITORING",
        rollback_target_tag="idx_previous_stable_v0",
    )
    session.add(idx)
    await session.commit()

    # Query back and increment failures
    result = await session.execute(
        select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == "idx_test_canary_v1")
    )
    loaded_idx = result.scalar_one()
    assert loaded_idx.canary_status == "MONITORING"
    assert loaded_idx.canary_failure_count == 0
    assert loaded_idx.rollback_target_tag == "idx_previous_stable_v0"

    # Simulate canary health check failure
    loaded_idx.canary_failure_count += 1
    loaded_idx.last_health_check_at = datetime.now(timezone.utc)
    await session.commit()

    # Re-fetch and assert updated state
    stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == "idx_test_canary_v1")
    updated = (await session.execute(stmt)).scalar_one()
    assert updated.canary_failure_count == 1

    # Simulate automated rollback trigger upon reaching threshold
    updated.canary_status = "FAILED_ROLLED_BACK"
    updated.status = "ROLLED_BACK"
    updated.rolled_back_at = datetime.now(timezone.utc)
    updated.rollback_reason = "Canary query failure rate exceeded threshold during 5-minute watchdog."
    await session.commit()

    final = (await session.execute(stmt)).scalar_one()
    assert final.canary_status == "FAILED_ROLLED_BACK"
    assert final.status == "ROLLED_BACK"
    assert "Canary query failure" in final.rollback_reason


@pytest.mark.asyncio
async def test_watchdog_restart_recovery_query(session: AsyncSession):
    """Simulates Celery worker restart scanning for uncompleted watchdog monitors."""
    now = datetime.now(timezone.utc)

    # 1. Past watchdog expired index
    expired_idx = KnowledgeIndex(
        index_version_tag="idx_restart_expired",
        storage_path="/storage/indices/exp.index",
        total_vectors=1000,
        status="ACTIVE",
        watchdog_expires_at=now - timedelta(minutes=2),
        canary_status="MONITORING",
        rollback_target_tag="idx_stable",
    )
    # 2. Currently active monitoring index
    active_idx = KnowledgeIndex(
        index_version_tag="idx_restart_active",
        storage_path="/storage/indices/act.index",
        total_vectors=2000,
        status="ACTIVE",
        watchdog_expires_at=now + timedelta(minutes=3),
        canary_status="MONITORING",
        rollback_target_tag="idx_stable",
    )
    # 3. Passed index
    passed_idx = KnowledgeIndex(
        index_version_tag="idx_restart_passed",
        storage_path="/storage/indices/pass.index",
        total_vectors=3000,
        status="ACTIVE",
        canary_status="PASSED",
    )
    session.add_all([expired_idx, active_idx, passed_idx])
    await session.commit()

    # Query for all indexes needing worker attention upon worker startup
    stmt = select(KnowledgeIndex).where(KnowledgeIndex.canary_status == "MONITORING")
    monitoring_indices = (await session.execute(stmt)).scalars().all()

    assert len(monitoring_indices) == 2
    tags = {idx.index_version_tag for idx in monitoring_indices}
    assert "idx_restart_expired" in tags
    assert "idx_restart_active" in tags
    assert "idx_restart_passed" not in tags

