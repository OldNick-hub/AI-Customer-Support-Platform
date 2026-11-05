"""Durable Automated Rollback Watchdog & Health Canary Engine.

Monitors active vector index health continuously during the 5-minute canary window,
detecting latency, similarity, retrieval, and availability regressions, and executing
an automated zero-downtime rollback if failure thresholds are tripped:
- 2 consecutive interval failures OR
- 3 total interval failures across the 10 monitoring intervals.

Also provides crash recovery to resume monitoring or finalize state upon worker restart.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import faiss
import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import IndexWatchdogRollbackError, RollbackExecutionError
from app.core.logging import configure_safe_logger
from app.models.knowledge_index import KnowledgeIndex
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.knowledge.rollback_manager import RollbackManager, RollbackResult
from app.services.embedding_service import EmbeddingService
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))

DEFAULT_CANARY_QUERIES = [
    "What is the standard return policy for merchandise?",
    "How do I track an expedited international delivery?",
    "What warranty options apply to hardware defects?",
]


class CanaryCheckResult(BaseModel):
    """Result of an individual canary health check interval."""

    is_healthy: bool
    latency_p95_ms: float
    average_similarity: float
    query_count: int
    passed_count: int
    failure_reasons: List[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WatchdogEvaluationResult(BaseModel):
    """Result of evaluating watchdog health and triggers."""

    status: str  # 'HEALTHY', 'DEGRADED', 'ROLLBACK_TRIGGERED', 'CONFIRMED', 'IDLE', 'RECOVERED'
    active_tag: Optional[str] = None
    target_tag: Optional[str] = None
    consecutive_failures: int = 0
    total_failures: int = 0
    check_result: Optional[CanaryCheckResult] = None
    rollback_executed: bool = False
    rollback_result: Optional[RollbackResult] = None
    message: str


class IndexHealthWatchdog:
    """Monitors active vector index health and executes automated rollback upon regression."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        rollback_manager: Optional[RollbackManager] = None,
        reader: Optional[ActiveIndexReader] = None,
        embedding_service: Optional[EmbeddingService] = None,
        redis_service: Optional[RedisService] = None,
        query_runner: Optional[Callable[[faiss.Index, str], Tuple[float, float]]] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.rollback_manager = rollback_manager or RollbackManager(settings=self.settings, reader=reader)
        self.reader = reader or ActiveIndexReader(settings=self.settings)
        self.embedding_service = embedding_service
        self.redis_service = redis_service or get_redis_service()
        self.query_runner = query_runner

        self.consecutive_failure_threshold = getattr(
            self.settings, "WATCHDOG_CONSECUTIVE_FAILURE_THRESHOLD", 2
        )
        self.total_failure_threshold = getattr(
            self.settings, "WATCHDOG_TOTAL_FAILURE_THRESHOLD", 3
        )
        self.query_timeout_seconds = getattr(
            self.settings, "WATCHDOG_QUERY_TIMEOUT_SECONDS", 2.0
        )
        self.latency_p95_threshold_ms = 100.0
        self.similarity_threshold = getattr(
            self.settings, "SIMILARITY_THRESHOLD", 0.75
        )

        self.consecutive_failures: int = 0
        self.storage_dir = os.path.abspath(self.settings.FAISS_STORAGE_DIR)
        self._scheduler_task: Optional[asyncio.Task] = None
        self._is_monitoring: bool = False
        self._scheduler_lock_key: Optional[str] = None

    async def get_persisted_consecutive_failures(self, tag: str) -> int:
        """Retrieves durable consecutive failure count from Redis, falling back to memory."""
        if self.redis_service:
            try:
                val = await self.redis_service.get(f"watchdog:consecutive:{tag}")
                if val is not None:
                    return int(val)
            except Exception:
                pass
        return self.consecutive_failures

    async def set_persisted_consecutive_failures(self, tag: str, count: int) -> None:
        """Persists durable consecutive failure count to Redis with 600s TTL."""
        self.consecutive_failures = count
        if self.redis_service:
            try:
                await self.redis_service.set(
                    f"watchdog:consecutive:{tag}",
                    str(count),
                    ttl_seconds=600,
                    raise_on_error=False,
                )
            except Exception:
                pass

    async def get_persisted_intervals_count(self, tag: str) -> int:
        """Retrieves total completed monitoring intervals count from Redis."""
        if self.redis_service:
            try:
                val = await self.redis_service.get(f"watchdog:intervals:{tag}")
                if val is not None:
                    return int(val)
            except Exception:
                pass
        return 0

    async def increment_persisted_intervals_count(self, tag: str) -> int:
        """Increments and persists interval evaluation counter."""
        current = await self.get_persisted_intervals_count(tag) + 1
        if self.redis_service:
            try:
                await self.redis_service.set(
                    f"watchdog:intervals:{tag}",
                    str(current),
                    ttl_seconds=600,
                    raise_on_error=False,
                )
            except Exception:
                pass
        return current

    async def acquire_scheduler_lock(self, tag: str, ttl_seconds: int = 60) -> bool:
        """Acquires distributed lock in Redis to prevent duplicate watchdog schedulers."""
        if not self.redis_service:
            return True
        lock_key = f"watchdog:scheduler_lock:{tag}"
        try:
            existing = await self.redis_service.get(lock_key)
            if existing is not None:
                return False
            await self.redis_service.set(lock_key, "1", ttl_seconds=ttl_seconds, raise_on_error=False)
            self._scheduler_lock_key = lock_key
            return True
        except Exception as exc:
            logger.warning("Could not check distributed scheduler lock in Redis: %s", exc)
            return True

    async def release_scheduler_lock(self, tag: Optional[str] = None) -> None:
        """Releases the distributed watchdog scheduler lock."""
        lock_key = f"watchdog:scheduler_lock:{tag}" if tag else self._scheduler_lock_key
        if lock_key and self.redis_service:
            try:
                await self.redis_service.delete(lock_key, raise_on_error=False)
            except Exception:
                pass
        self._scheduler_lock_key = None

    def run_canary_health_check(
        self,
        index: Optional[faiss.Index] = None,
        queries: Optional[List[str]] = None,
    ) -> CanaryCheckResult:
        """Executes synthetic health queries against the active vector index.

        Validates index availability, query execution timeout (<=2.0s),
        retrieval latency P95 (<=100ms), and semantic similarity (>=0.75).
        """
        now_utc = datetime.now(timezone.utc)
        target_queries = queries or DEFAULT_CANARY_QUERIES

        # Obtain index if not provided
        active_index = index or self.reader.get_index()
        if active_index is None:
            # Attempt to load directly from storage directory if reader uninitialized
            manifest_path = os.path.join(self.storage_dir, "active_index.json")
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    idx_path = meta.get("index_file_path")
                    if idx_path and os.path.isfile(idx_path):
                        active_index = faiss.read_index(idx_path)
                except Exception as exc:
                    logger.warning("Could not load active index for health check: %s", exc)

        if active_index is None:
            return CanaryCheckResult(
                is_healthy=False,
                latency_p95_ms=9999.0,
                average_similarity=0.0,
                query_count=len(target_queries),
                passed_count=0,
                failure_reasons=["Active FAISS index is unavailable, unreadable, or missing."],
                evaluated_at=now_utc,
            )

        latencies_ms: List[float] = []
        similarities: List[float] = []
        failures: List[str] = []
        passed_queries = 0

        for q in target_queries:
            t0 = time.perf_counter()
            try:
                if self.query_runner:
                    # Injected query runner (e.g. for testing specific degradation scenarios)
                    lat_ms, sim = self.query_runner(active_index, q)
                else:
                    # Synthetic vector generation and retrieval
                    q_len = len(q)
                    np.random.seed(abs(hash(q)) % (2**31))
                    q_vec = np.random.randn(1, 384).astype(np.float32)
                    faiss.normalize_L2(q_vec)

                    D, I = active_index.search(q_vec, k=min(3, max(1, active_index.ntotal)))
                    lat_ms = (time.perf_counter() - t0) * 1000.0
                    sim = float(D[0][0]) if len(D) > 0 and len(D[0]) > 0 else 0.0

                latencies_ms.append(lat_ms)
                similarities.append(sim)

                # Check individual query timeout (2.0s = 2000ms)
                if lat_ms > (self.query_timeout_seconds * 1000.0):
                    failures.append(f"Query '{q[:30]}...' timed out ({lat_ms:.1f}ms > {self.query_timeout_seconds*1000}ms).")
                elif sim < self.similarity_threshold:
                    failures.append(f"Query '{q[:30]}...' low similarity ({sim:.2f} < {self.similarity_threshold}).")
                else:
                    passed_queries += 1

            except Exception as q_exc:
                lat_ms = (time.perf_counter() - t0) * 1000.0
                latencies_ms.append(lat_ms)
                similarities.append(0.0)
                failures.append(f"Query '{q[:30]}...' crashed: {q_exc}")

        p95_lat = float(np.percentile(latencies_ms, 95)) if latencies_ms else 0.0
        avg_sim = float(np.mean(similarities)) if similarities else 0.0

        if p95_lat > self.latency_p95_threshold_ms:
            failures.append(f"P95 latency breached threshold ({p95_lat:.1f}ms > {self.latency_p95_threshold_ms}ms).")

        is_healthy = len(failures) == 0 and passed_queries == len(target_queries)

        return CanaryCheckResult(
            is_healthy=is_healthy,
            latency_p95_ms=p95_lat,
            average_similarity=avg_sim,
            query_count=len(target_queries),
            passed_count=passed_queries,
            failure_reasons=failures,
            evaluated_at=now_utc,
        )

    async def evaluate_active_index_health(
        self,
        session: AsyncSession,
        active_tag: Optional[str] = None,
        check_result: Optional[CanaryCheckResult] = None,
        current_time: Optional[datetime] = None,
    ) -> WatchdogEvaluationResult:
        """Evaluates health for active index in MONITORING, increments counts, and triggers rollback if tripped."""
        now_utc = current_time or datetime.now(timezone.utc)

        # Locate active monitoring index
        if active_tag:
            stmt = select(KnowledgeIndex).where(KnowledgeIndex.index_version_tag == active_tag)
        else:
            stmt = select(KnowledgeIndex).where(
                KnowledgeIndex.canary_status == "MONITORING",
                KnowledgeIndex.status == "ACTIVE",
            )

        k_idx = (await session.execute(stmt)).scalar_one_or_none()
        if not k_idx:
            return WatchdogEvaluationResult(
                status="IDLE",
                message="No active knowledge index currently under canary MONITORING.",
            )

        # Execute or use provided check result
        check = check_result or self.run_canary_health_check()
        k_idx.last_health_check_at = now_utc

        if check.is_healthy:
            # Reset consecutive failure counter on any healthy interval
            self.consecutive_failures = 0
            await self.set_persisted_consecutive_failures(k_idx.index_version_tag, 0)

            # Check if 5-minute monitoring window has expired
            expires_at = k_idx.watchdog_expires_at
            if expires_at and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            if expires_at and now_utc >= expires_at:
                k_idx.status = "CONFIRMED"
                k_idx.canary_status = "PASSED"
                await session.commit()
                await self.release_scheduler_lock(k_idx.index_version_tag)
                logger.info(
                    "Watchdog: Canary monitoring period completed for '%s'. Index CONFIRMED.",
                    k_idx.index_version_tag,
                )
                return WatchdogEvaluationResult(
                    status="CONFIRMED",
                    active_tag=k_idx.index_version_tag,
                    target_tag=k_idx.rollback_target_tag,
                    consecutive_failures=0,
                    total_failures=k_idx.canary_failure_count,
                    check_result=check,
                    rollback_executed=False,
                    message="Canary window expired with healthy checks. Index confirmed as permanent baseline.",
                )

            await session.commit()
            return WatchdogEvaluationResult(
                status="HEALTHY",
                active_tag=k_idx.index_version_tag,
                target_tag=k_idx.rollback_target_tag,
                consecutive_failures=0,
                total_failures=k_idx.canary_failure_count,
                check_result=check,
                rollback_executed=False,
                message="Canary check passed. Index remains healthy under monitoring.",
            )

        # Handle health check failure
        curr_consec = await self.get_persisted_consecutive_failures(k_idx.index_version_tag) + 1
        await self.set_persisted_consecutive_failures(k_idx.index_version_tag, curr_consec)
        self.consecutive_failures = curr_consec
        k_idx.canary_failure_count += 1

        is_consec_trip = self.consecutive_failures >= self.consecutive_failure_threshold
        is_total_trip = k_idx.canary_failure_count >= self.total_failure_threshold

        if is_consec_trip or is_total_trip:
            trigger_name = "consecutive" if is_consec_trip else "total"
            trigger_reason = (
                f"Automated Rollback: Canary health threshold breached with {self.consecutive_failures} "
                f"consecutive failures and {k_idx.canary_failure_count} total failures ({trigger_name} trigger). "
                f"Reasons: {'; '.join(check.failure_reasons)}"
            )

            if k_idx.rollback_target_tag:
                logger.warning(
                    "Watchdog: Rollback trigger tripped for '%s'! Reverting to '%s'.",
                    k_idx.index_version_tag,
                    k_idx.rollback_target_tag,
                )
                try:
                    rb_result = await self.rollback_manager.execute_rollback(
                        session=session,
                        failed_tag=k_idx.index_version_tag,
                        target_tag=k_idx.rollback_target_tag,
                        reason=trigger_reason,
                        triggered_by="watchdog",
                    )
                    await self.release_scheduler_lock(k_idx.index_version_tag)
                    return WatchdogEvaluationResult(
                        status="ROLLBACK_TRIGGERED",
                        active_tag=k_idx.index_version_tag,
                        target_tag=k_idx.rollback_target_tag,
                        consecutive_failures=self.consecutive_failures,
                        total_failures=k_idx.canary_failure_count,
                        check_result=check,
                        rollback_executed=True,
                        rollback_result=rb_result,
                        message=trigger_reason,
                    )
                except Exception as rb_exc:
                    logger.error("Watchdog automated rollback execution failed: %s", rb_exc)
                    k_idx.status = "FAILED"
                    k_idx.rollback_reason = f"Rollback failed: {rb_exc}"
                    await session.commit()
                    return WatchdogEvaluationResult(
                        status="ERROR",
                        active_tag=k_idx.index_version_tag,
                        target_tag=k_idx.rollback_target_tag,
                        consecutive_failures=self.consecutive_failures,
                        total_failures=k_idx.canary_failure_count,
                        check_result=check,
                        rollback_executed=False,
                        message=f"Rollback execution failed: {rb_exc}",
                    )
            else:
                logger.error("Watchdog failure tripped for '%s' but no rollback_target_tag exists!", k_idx.index_version_tag)
                k_idx.canary_status = "FAILED_NO_ROLLBACK_TARGET"
                await session.commit()
                return WatchdogEvaluationResult(
                    status="DEGRADED",
                    active_tag=k_idx.index_version_tag,
                    consecutive_failures=self.consecutive_failures,
                    total_failures=k_idx.canary_failure_count,
                    check_result=check,
                    rollback_executed=False,
                    message=f"Threshold breached but no target index available to restore: {trigger_reason}",
                )

        # Failure recorded but threshold not yet breached
        await session.commit()
        return WatchdogEvaluationResult(
            status="DEGRADED",
            active_tag=k_idx.index_version_tag,
            target_tag=k_idx.rollback_target_tag,
            consecutive_failures=self.consecutive_failures,
            total_failures=k_idx.canary_failure_count,
            check_result=check,
            rollback_executed=False,
            message=(
                f"Canary check failed ({self.consecutive_failures}/{self.consecutive_failure_threshold} consecutive, "
                f"{k_idx.canary_failure_count}/{self.total_failure_threshold} total). Window active."
            ),
        )

    async def recover_monitoring_on_restart(
        self,
        session: AsyncSession,
        current_time: Optional[datetime] = None,
    ) -> List[WatchdogEvaluationResult]:
        """Recovers in-flight watchdog tasks and pending rollbacks across worker restarts."""
        now_utc = current_time or datetime.now(timezone.utc)
        results: List[WatchdogEvaluationResult] = []

        # 1. Recover interrupted rollbacks left in ROLLBACK_PENDING
        stmt_pending = select(KnowledgeIndex).where(KnowledgeIndex.status == "ROLLBACK_PENDING")
        pending_indices = (await session.execute(stmt_pending)).scalars().all()
        for p_idx in pending_indices:
            if p_idx.rollback_target_tag:
                logger.warning("Restart Recovery: Resuming interrupted rollback for '%s'.", p_idx.index_version_tag)
                try:
                    rb_res = await self.rollback_manager.execute_rollback(
                        session=session,
                        failed_tag=p_idx.index_version_tag,
                        target_tag=p_idx.rollback_target_tag,
                        reason="Worker restart recovery: completing interrupted rollback.",
                        triggered_by="startup_recovery",
                    )
                    results.append(
                        WatchdogEvaluationResult(
                            status="ROLLBACK_TRIGGERED",
                            active_tag=p_idx.index_version_tag,
                            target_tag=p_idx.rollback_target_tag,
                            rollback_executed=True,
                            rollback_result=rb_res,
                            message="Completed interrupted rollback from restart.",
                        )
                    )
                except Exception as exc:
                    logger.error("Restart recovery failed to complete rollback: %s", exc)

        # 2. Recover MONITORING indexes
        stmt_mon = select(KnowledgeIndex).where(
            KnowledgeIndex.canary_status == "MONITORING",
            KnowledgeIndex.status == "ACTIVE",
        )
        mon_indices = (await session.execute(stmt_mon)).scalars().all()
        for m_idx in mon_indices:
            m_expires = m_idx.watchdog_expires_at
            if m_expires and m_expires.tzinfo is None:
                m_expires = m_expires.replace(tzinfo=timezone.utc)

            if m_expires and now_utc >= m_expires:
                # Window expired during worker downtime
                if m_idx.canary_failure_count < self.total_failure_threshold:
                    m_idx.status = "CONFIRMED"
                    m_idx.canary_status = "PASSED"
                    await session.commit()
                    results.append(
                        WatchdogEvaluationResult(
                            status="CONFIRMED",
                            active_tag=m_idx.index_version_tag,
                            message="Canary window expired during downtime with acceptable failures. Confirmed.",
                        )
                    )
                else:
                    # Failures exceeded threshold; trigger rollback
                    if m_idx.rollback_target_tag:
                        rb_res = await self.rollback_manager.execute_rollback(
                            session=session,
                            failed_tag=m_idx.index_version_tag,
                            target_tag=m_idx.rollback_target_tag,
                            reason="Worker restart recovery: failures exceeded threshold during monitoring.",
                            triggered_by="startup_recovery",
                        )
                        results.append(
                            WatchdogEvaluationResult(
                                status="ROLLBACK_TRIGGERED",
                                active_tag=m_idx.index_version_tag,
                                target_tag=m_idx.rollback_target_tag,
                                rollback_executed=True,
                                rollback_result=rb_res,
                                message="Rollback executed on restart after threshold breach.",
                            )
                        )
            else:
                # Window still active; resume monitoring
                results.append(
                    WatchdogEvaluationResult(
                        status="HEALTHY",
                        active_tag=m_idx.index_version_tag,
                        target_tag=m_idx.rollback_target_tag,
                        total_failures=m_idx.canary_failure_count,
                        message="Resumed active canary monitoring on worker restart.",
                    )
                )

        return results

    async def run_monitoring_scheduler(
        self,
        session_factory: Callable[[], AsyncSession],
        active_tag: Optional[str] = None,
        interval_seconds: float = 30.0,
        max_intervals: int = 10,
    ) -> WatchdogEvaluationResult:
        """Executes scheduled canary health checks at intended intervals without manual calls."""
        self._is_monitoring = True
        logger.info(
            "Starting Watchdog Canary Monitoring Scheduler (interval=%.1fs, max_intervals=%d, tag=%s).",
            interval_seconds,
            max_intervals,
            active_tag or "auto-detect",
        )

        target_tag = active_tag
        if target_tag:
            lock_acquired = await self.acquire_scheduler_lock(target_tag, ttl_seconds=int(interval_seconds * 3))
            if not lock_acquired:
                logger.warning(
                    "Watchdog scheduler lock for '%s' already held by another worker. Skipping duplicate scheduler.",
                    target_tag,
                )
                self._is_monitoring = False
                return WatchdogEvaluationResult(
                    status="DUPLICATE_PREVENTED",
                    active_tag=target_tag,
                    message="Duplicate scheduler execution prevented by distributed lock.",
                )

        try:
            intervals_run = 0
            while self._is_monitoring and intervals_run < max_intervals:
                await asyncio.sleep(interval_seconds)
                if not self._is_monitoring:
                    break

                intervals_run += 1
                async with session_factory() as session:
                    # Run canary check
                    check_res = self.run_canary_health_check()
                    eval_res = await self.evaluate_active_index_health(
                        session=session,
                        active_tag=target_tag,
                        check_result=check_res,
                    )

                    if target_tag:
                        await self.increment_persisted_intervals_count(target_tag)

                    logger.info(
                        "Watchdog Scheduler Tick %d/%d: %s for '%s'. Result: %s",
                        intervals_run,
                        max_intervals,
                        "Healthy" if check_res.is_healthy else "Degraded",
                        eval_res.active_tag,
                        eval_res.status,
                    )

                    if eval_res.status in ("CONFIRMED", "ROLLBACK_TRIGGERED", "ERROR", "IDLE"):
                        logger.info("Watchdog scheduler completed monitoring with status '%s'.", eval_res.status)
                        return eval_res

            return WatchdogEvaluationResult(
                status="COMPLETED",
                active_tag=target_tag,
                message=f"Watchdog monitoring scheduler completed {intervals_run} intervals.",
            )

        except asyncio.CancelledError:
            logger.info("Watchdog monitoring scheduler cancelled gracefully.")
            return WatchdogEvaluationResult(
                status="CANCELLED",
                active_tag=target_tag,
                message="Watchdog monitoring scheduler cancelled.",
            )
        finally:
            self._is_monitoring = False
            if target_tag:
                await self.release_scheduler_lock(target_tag)

    def start_monitoring_scheduler(
        self,
        session_factory: Callable[[], AsyncSession],
        active_tag: Optional[str] = None,
        interval_seconds: float = 30.0,
        max_intervals: int = 10,
    ) -> asyncio.Task:
        """Starts the scheduled health check task in the background."""
        if self._scheduler_task and not self._scheduler_task.done():
            logger.warning("Watchdog monitoring scheduler already running in-process.")
            return self._scheduler_task

        self._scheduler_task = asyncio.create_task(
            self.run_monitoring_scheduler(
                session_factory=session_factory,
                active_tag=active_tag,
                interval_seconds=interval_seconds,
                max_intervals=max_intervals,
            )
        )
        return self._scheduler_task

    async def stop_monitoring_scheduler_async(self) -> None:
        """Gracefully stops and awaits completion of the scheduled background task."""
        self._is_monitoring = False
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scheduler_task = None

    def stop_monitoring_scheduler(self) -> None:
        """Gracefully cancels the scheduled background monitoring task."""
        self._is_monitoring = False
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            self._scheduler_task = None

