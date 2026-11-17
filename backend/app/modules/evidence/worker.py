"""Background Evidence Reconciliation Worker.

Provides asynchronous, concurrency-safe processing of queued evidence reconciliation
jobs and recurring recovery of stale or interrupted jobs.

Key Guarantees:
1. Concurrency safety: Uses `FOR UPDATE SKIP LOCKED` on PostgreSQL to prevent double-claiming.
2. Safe Lifecycle: Integrated into `WorkerLifecycleManager` and FastAPI `lifespan`.
3. Crash Recovery: Detects stuck `PENDING` or `PROCESSING` jobs (including NULL timestamps)
   exceeding the stale threshold and increments retry counts up to a maximum of 3.
4. Terminal State Invariant: Completed and failed jobs are never reprocessed or overwritten.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Dict, List, Optional
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.db.base import utc_now
from app.models.customer import Conversation
from app.models.evidence import EvidenceFile, EvidenceVerification
from app.modules.evidence.claims import ClaimExtractionEngine
from app.modules.evidence.clarification import ClarificationStatus
from app.modules.evidence.reconciler import (
    EvidenceReconciliationEngine,
    ReconciliationOutcome,
)
from app.modules.evidence.schemas import StructuredInvoiceData

logger = configure_safe_logger(logging.getLogger(__name__))


class ReconciliationWorker:
    """Asynchronous background worker executing and recovering evidence reconciliation jobs."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
        clock: Optional[Clock] = None,
        poll_interval_seconds: float = 10.0,
        stale_timeout_seconds: float = 300.0,
        batch_size: int = 20,
        stale_cutoff_seconds: Optional[float] = None,
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock or system_clock
        self.poll_interval_seconds = poll_interval_seconds
        self.stale_timeout_seconds = stale_cutoff_seconds if stale_cutoff_seconds is not None else stale_timeout_seconds
        self.batch_size = batch_size
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        self.scan_count: int = 0
        self.last_scan_at: Optional[datetime] = None
        self.last_error: Optional[Exception] = None

        self.claims_engine = ClaimExtractionEngine()
        self.reconciliation_engine = EvidenceReconciliationEngine()

    async def scan_and_process_batch(
        self,
        session: AsyncSession,
        batch_size: Optional[int] = None,
    ) -> List[EvidenceVerification]:
        """Claims and processes a batch of PENDING reconciliation jobs safely."""

        limit = batch_size or self.batch_size

        # 1. Concurrency-safe selection of PENDING jobs
        stmt = (
            select(EvidenceVerification)
            .where(EvidenceVerification.job_status == "PENDING")
            .order_by(EvidenceVerification.created_at.asc())
            .limit(limit)
        )

        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        res = await session.execute(stmt)
        pending_jobs = res.scalars().all()

        if not pending_jobs:
            return []

        logger.info("ReconciliationWorker picked up %d pending job(s).", len(pending_jobs))
        processed_records: List[EvidenceVerification] = []

        for job in pending_jobs:
            # Transition to PROCESSING and set timestamp to avoid duplicate pickup
            job.job_status = "PROCESSING"
            job.processing_started_at = utc_now()
            await session.commit()
            await session.refresh(job)

            try:
                # 2. Execute reconciliation
                await self._execute_job_reconciliation(session, job)
                job.job_status = "COMPLETED"
                job.verified_at = utc_now()
                await session.commit()
                await session.refresh(job)
                processed_records.append(job)
                logger.info("ReconciliationWorker completed job: %s", job.id)

            except Exception as exc:
                logger.error("Error executing reconciliation for job %s: %s", job.id, exc, exc_info=True)
                job.retry_count += 1
                if job.retry_count < 3:
                    job.job_status = "PENDING"
                    job.error_details = f"Processing error (retry {job.retry_count}/3): {exc}"
                else:
                    job.job_status = "FAILED"
                    job.error_details = f"Job permanently failed after {job.retry_count} retries: {exc}"
                await session.commit()

        return processed_records

    async def recover_stale_jobs(
        self,
        session: AsyncSession,
        stale_timeout_seconds: Optional[float] = None,
    ) -> int:
        """Recovers jobs stuck in PENDING or PROCESSING past the stale threshold.

        Guarantees:
        - Catches jobs with NULL timestamps or NULL verified_at.
        - Increments retry_count exactly once.
        - Permanently marks jobs FAILED if retry_count >= 3.
        - Leaves COMPLETED and terminal FAILED jobs completely untouched.
        - Concurrency-safe via SKIP LOCKED on PostgreSQL.
        """

        timeout = stale_timeout_seconds or self.stale_timeout_seconds
        cutoff = utc_now() - timedelta(seconds=timeout)

        # Stale criteria:
        # 1. PROCESSING jobs with processing_started_at < cutoff (or NULL processing_started_at + created_at < cutoff)
        # 2. PENDING jobs with created_at < cutoff
        # 3. PENDING/PROCESSING jobs with NULL verified_at + created_at < cutoff
        stale_filter = and_(
            EvidenceVerification.job_status.in_(["PENDING", "PROCESSING"]),
            or_(
                # Processing stuck past threshold
                and_(
                    EvidenceVerification.job_status == "PROCESSING",
                    or_(
                        EvidenceVerification.processing_started_at < cutoff,
                        and_(
                            EvidenceVerification.processing_started_at.is_(None),
                            EvidenceVerification.created_at < cutoff,
                        ),
                    ),
                ),
                # Pending stuck past threshold
                and_(
                    EvidenceVerification.job_status == "PENDING",
                    EvidenceVerification.created_at < cutoff,
                ),
                # Null verified_at stuck past threshold
                and_(
                    EvidenceVerification.verified_at.is_(None),
                    EvidenceVerification.created_at < cutoff,
                ),
            ),
        )

        stmt = select(EvidenceVerification).where(stale_filter).order_by(EvidenceVerification.created_at.asc())

        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        res = await session.execute(stmt)
        stale_jobs = res.scalars().all()

        recovered_count = 0
        for job in stale_jobs:
            if job.retry_count < 3:
                job.retry_count += 1
                job.job_status = "PENDING"
                job.processing_started_at = None
                job.error_details = (
                    f"Recovered from stale state (retry {job.retry_count}/3) at {utc_now().isoformat()}"
                )
                logger.warning(
                    "ReconciliationWorker recovered stale job %s (retries: %d)", job.id, job.retry_count
                )
            else:
                job.job_status = "FAILED"
                job.error_details = f"Job permanently failed after {job.retry_count} retries (stale timeout)."
                logger.error("ReconciliationWorker marked stale job %s permanently FAILED", job.id)

            recovered_count += 1

        if recovered_count > 0:
            await session.commit()

        return recovered_count

    async def _execute_job_reconciliation(
        self,
        session: AsyncSession,
        job: EvidenceVerification,
    ) -> None:
        """Internal helper executing core reconciliation against stored evidence."""

        ev_stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == job.evidence_file_id)
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        ev_res = await session.execute(ev_stmt)
        evidence = ev_res.scalar_one_or_none()
        if not evidence:
            raise RuntimeError(f"Linked evidence file '{job.evidence_file_id}' not found.")

        # Check quarantine
        is_quarantined = evidence.status == "FLAGGED_UNSAFE" or evidence.is_malicious

        # Extract claims
        claims_payload = self.claims_engine.extract_claims(
            text=job.claim_text,
            message_id=str(job.message_id) if job.message_id else None,
        )

        # Structured invoice data
        invoice_data = None
        if evidence.extracted_entities:
            entity = evidence.extracted_entities[0]
            if entity.extraction_metadata:
                try:
                    invoice_data = StructuredInvoiceData(**entity.extraction_metadata)
                except Exception:
                    invoice_data = self._reconstruct_from_entity(entity)
            else:
                invoice_data = self._reconstruct_from_entity(entity)

        # Check secondary evidence if present
        sec_invoice_data = None
        sec_file_hash = None
        if getattr(job, "secondary_evidence_file_id", None):
            sec_stmt = (
                select(EvidenceFile)
                .where(EvidenceFile.id == job.secondary_evidence_file_id)
                .options(selectinload(EvidenceFile.extracted_entities))
            )
            sec_res = await session.execute(sec_stmt)
            sec_evidence = sec_res.scalar_one_or_none()
            if sec_evidence:
                sec_file_hash = sec_evidence.sha256_hash
                if sec_evidence.extracted_entities:
                    sec_ent = sec_evidence.extracted_entities[0]
                    if sec_ent.extraction_metadata:
                        try:
                            sec_invoice_data = StructuredInvoiceData(**sec_ent.extraction_metadata)
                        except Exception:
                            sec_invoice_data = self._reconstruct_from_entity(sec_ent)
                    else:
                        sec_invoice_data = self._reconstruct_from_entity(sec_ent)

        # Run reconciliation engine
        reconcile_result = self.reconciliation_engine.reconcile(
            claims_payload=claims_payload,
            evidence_data=invoice_data,
            evidence_id=str(evidence.id),
            conversation_id=str(job.conversation_id) if job.conversation_id else None,
            is_quarantined=is_quarantined,
            quarantine_reason=evidence.quarantine_reason,
            secondary_evidence_data=sec_invoice_data,
            secondary_evidence_id=str(job.secondary_evidence_file_id) if getattr(job, "secondary_evidence_file_id", None) else None,
            primary_file_hash=evidence.sha256_hash,
            secondary_file_hash=sec_file_hash,
        )

        match_status_map = {
            ReconciliationOutcome.MATCH: "VERIFIED_MATCH",
            ReconciliationOutcome.MISMATCH: "TOTAL_CONFLICT",
            ReconciliationOutcome.UNCERTAIN: "LOW_QUALITY",
            ReconciliationOutcome.INSUFFICIENT_EVIDENCE: "INSUFFICIENT_EVIDENCE",
            ReconciliationOutcome.MISSING_FROM_EVIDENCE: "PARTIAL_MISMATCH",
            ReconciliationOutcome.MISSING_FROM_MESSAGE: "PARTIAL_MISMATCH",
        }

        serialized_claims = [c.model_dump(mode="json") for c in claims_payload.claims]
        serialized_comps = [c.model_dump(mode="json") for c in reconcile_result.field_comparisons]

        job.matches_order_id = any(c.field_name == "order_id" and c.outcome == ReconciliationOutcome.MATCH for c in reconcile_result.field_comparisons)
        job.matches_amount = any(c.field_name == "total_amount" and c.outcome == ReconciliationOutcome.MATCH for c in reconcile_result.field_comparisons)
        job.matches_product = any(c.field_name == "product_name" and c.outcome == ReconciliationOutcome.MATCH for c in reconcile_result.field_comparisons)
        job.overall_match_status = match_status_map.get(reconcile_result.overall_outcome, "PARTIAL_MISMATCH")
        job.reconciliation_status = reconcile_result.overall_outcome.value
        job.clarification_status = (
            ClarificationStatus.PENDING_CUSTOMER.value
            if reconcile_result.requires_clarification
            else ClarificationStatus.NONE_REQUIRED.value
        )
        job.clarification_prompt = reconcile_result.clarification_prompt
        job.extracted_claims = serialized_claims
        job.field_comparisons = {"results": serialized_comps, "summary": reconcile_result.summary}
        job.overall_confidence = reconcile_result.overall_confidence
        job.security_flags = getattr(claims_payload, "security_flags", []) or []

    def _reconstruct_from_entity(self, entity) -> StructuredInvoiceData:
        return StructuredInvoiceData(
            order_id=entity.extracted_order_id,
            total_amount=float(entity.extracted_amount) if entity.extracted_amount is not None else None,
            currency=entity.extracted_currency,
            transaction_date=entity.extracted_date,
            product_name=entity.extracted_product_sku,
            overall_confidence=entity.ocr_confidence,
            field_confidence={
                "order_id": entity.ocr_confidence,
                "total_amount": entity.ocr_confidence,
                "currency": entity.ocr_confidence,
            },
        )

    @property
    def evidence_service(self):
        if not hasattr(self, "_evidence_service") or self._evidence_service is None:
            from app.modules.evidence.service import EvidenceService
            self._evidence_service = EvidenceService()
        return self._evidence_service

    async def scan_and_process_staged_evidence(
        self,
        session: AsyncSession,
        batch_size: Optional[int] = None,
    ) -> List[EvidenceFile]:
        """Claims and processes a batch of STAGED evidence files in the background."""
        limit = batch_size or self.batch_size
        stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.status == "STAGED")
            .order_by(EvidenceFile.created_at.asc())
            .limit(limit)
        )
        bind = session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        res = await session.execute(stmt)
        staged_files = res.scalars().all()
        if not staged_files:
            return []

        processed_files: List[EvidenceFile] = []
        for ef in staged_files:
            processed = await self.evidence_service.process_staged_evidence(
                db=session,
                evidence_id=ef.id,
            )
            if processed:
                processed_files.append(processed)

        return processed_files

    async def run_once(self) -> Dict[str, Any]:
        """Executes a single pass of stale recovery, pending batch processing, and staged evidence."""
        if not self.session_factory:
            raise RuntimeError("Cannot call run_once() without a configured session_factory.")

        async with self.session_factory() as session:
            try:
                recovered = await self.recover_stale_jobs(session)
                processed = await self.scan_and_process_batch(session)
                staged = await self.scan_and_process_staged_evidence(session)
                return {"recovered": recovered, "processed": len(processed), "staged": len(staged)}
            except Exception:
                await session.rollback()
                raise

    def start_periodic_scanner(self, max_iterations: Optional[int] = None) -> asyncio.Task:
        """Starts periodic background scanning as a managed asyncio.Task."""
        if self._task and not self._task.done():
            logger.warning("ReconciliationWorker periodic scanner is already running.")
            return self._task

        self._stop_event.clear()
        self.is_running = True
        self._task = asyncio.create_task(self.start(max_iterations=max_iterations))

        def _on_done(task: asyncio.Task) -> None:
            self.is_running = False
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    self.last_error = exc
                    logger.critical(
                        "ReconciliationWorker background task terminated with exception: %s",
                        exc,
                        exc_info=exc,
                    )

        self._task.add_done_callback(_on_done)
        return self._task

    async def start(self, max_iterations: Optional[int] = None) -> None:
        """Polling loop processing batches and recovering stale jobs."""
        self._stop_event.clear()
        self.is_running = True
        iterations = 0
        logger.info("Starting ReconciliationWorker loop (interval: %.1fs)...", self.poll_interval_seconds)

        try:
            while not self._stop_event.is_set():
                try:
                    self.scan_count += 1
                    self.last_scan_at = self.clock.now()
                    await self.run_once()
                except Exception as exc:
                    self.last_error = exc
                    logger.error("Unexpected error in ReconciliationWorker loop: %s", exc, exc_info=True)

                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("ReconciliationWorker task cancelled. Exiting cleanly.")
            raise
        finally:
            self.is_running = False
            logger.info("ReconciliationWorker loop terminated.")

    def stop(self) -> None:
        """Signals worker to stop."""
        self._stop_event.set()

    async def stop_async(self, timeout_seconds: float = 2.0) -> None:
        """Gracefully cancels and awaits completion of worker background task."""
        self._stop_event.set()
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
