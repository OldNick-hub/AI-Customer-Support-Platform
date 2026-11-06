"""Evidence Reconciliation & Mismatch Detection Service.

Coordinates:
- Extraction of customer claims from natural language utterances.
- Retrieval & inspection of uploaded evidence files and OCR entities.
- Field-by-field verification with strict financial, currency, date, and security rules.
- Clarification lifecycle (PENDING_CUSTOMER -> RESOLVED / SUPERSEDED).
- Multi-tenant tenant boundary checks and PII-masked audit logging.
- Background asynchronous job orchestration with idempotency deduplication and recovery.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.db.base import utc_now
from app.models.customer import Conversation, Customer, Message
from app.models.evidence import EvidenceFile, EvidenceVerification, ExtractedEvidenceEntity
from app.models.auth import User
from app.modules.evidence.claims import ClaimExtractionEngine, ExtractedClaimsPayload
from app.modules.evidence.clarification import (
    ClarificationEngine,
    ClarificationRequest,
    ClarificationStatus,
)
from app.modules.evidence.reconciler import (
    EvidenceReconciliationEngine,
    FieldComparisonResult,
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.modules.evidence.schemas import (
    ClarificationItemResponse,
    ClarificationResponseRequest,
    ReconciliationJobStatusResponse,
    ReconciliationRequest,
    ReconciliationResponse,
    StructuredInvoiceData,
)

logger = configure_safe_logger(logging.getLogger(__name__))


class ReconciliationNotFoundException(PlatformException):
    """Raised when an evidence verification or job record cannot be located."""
    status_code: int = 404
    error_code: str = "NOT_FOUND"
    default_message: str = "Evidence reconciliation record not found."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message=message or self.default_message, status_code=404, error_code="NOT_FOUND")


class ReconciliationAuthorizationError(PlatformException):
    """Raised when a user attempts to access reconciliation data outside their tenant boundary."""
    status_code: int = 403
    error_code: str = "FORBIDDEN"
    default_message: str = "Access to requested reconciliation data is forbidden."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message=message or self.default_message, status_code=403, error_code="FORBIDDEN")


class ReconciliationService:
    """Production service for evidence claim verification and mismatch resolution."""

    def __init__(
        self,
        claims_engine: Optional[ClaimExtractionEngine] = None,
        reconciliation_engine: Optional[EvidenceReconciliationEngine] = None,
        clarification_engine: Optional[ClarificationEngine] = None,
    ):
        self.claims_engine = claims_engine or ClaimExtractionEngine()
        self.reconciliation_engine = reconciliation_engine or EvidenceReconciliationEngine()
        self.clarification_engine = clarification_engine or ClarificationEngine(
            reconciliation_engine=self.reconciliation_engine,
            claims_engine=self.claims_engine,
        )

    async def evaluate_reconciliation(
        self,
        db: AsyncSession,
        request: ReconciliationRequest,
        customer_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        current_user: Optional[User] = None,
    ) -> ReconciliationResponse:
        """Run or schedule reconciliation between customer claim text and stored evidence."""

        try:
            conv_uuid = uuid.UUID(request.conversation_id)
        except (ValueError, TypeError):
            raise PlatformException("Invalid conversation_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        try:
            evidence_uuid = uuid.UUID(request.evidence_id)
        except (ValueError, TypeError):
            raise PlatformException("Invalid evidence_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        message_uuid = None
        if request.message_id:
            try:
                message_uuid = uuid.UUID(request.message_id)
            except (ValueError, TypeError):
                raise PlatformException("Invalid message_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        # 1. Authorize Conversation Ownership
        conv_stmt = select(Conversation).where(Conversation.id == conv_uuid)
        conv_res = await db.execute(conv_stmt)
        conversation = conv_res.scalar_one_or_none()
        if not conversation:
            raise ReconciliationNotFoundException(f"Conversation '{request.conversation_id}' not found.")

        # Customer isolation check (staff bypass)
        is_staff = bool(roles and any(r in ("ADMIN", "AGENT", "SUPERVISOR") for r in roles))
        resolved_customer_id = customer_id
        if current_user and getattr(current_user, "role", None) == "CUSTOMER":
            resolved_customer_id = str(current_user.id)

        if not is_staff and resolved_customer_id and conversation.customer_id:
            if str(conversation.customer_id) != str(resolved_customer_id):
                logger.warning(
                    "Reconciliation multi-tenant violation: customer %s tried accessing conversation %s owned by %s",
                    resolved_customer_id,
                    conv_uuid,
                    conversation.customer_id,
                )
                raise ReconciliationAuthorizationError("Access denied: Conversation belongs to another customer.")

        # 2. Idempotency Check
        if request.idempotency_key:
            existing_stmt = select(EvidenceVerification).where(
                EvidenceVerification.conversation_id == conv_uuid,
                EvidenceVerification.idempotency_key == request.idempotency_key,
            )
            existing_res = await db.execute(existing_stmt)
            existing_rec = existing_res.scalar_one_or_none()
            if existing_rec:
                logger.info(
                    "Idempotency hit: returning existing verification %s for key %s",
                    existing_rec.id,
                    request.idempotency_key,
                )
                return self._to_response(existing_rec)

        # 3. Retrieve Evidence Record & Extracted Entities
        ev_stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == evidence_uuid)
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        ev_res = await db.execute(ev_stmt)
        evidence = ev_res.scalar_one_or_none()
        if not evidence:
            raise ReconciliationNotFoundException(f"Evidence '{request.evidence_id}' not found.")

        # Evidence must belong to conversation
        if evidence.conversation_id != conv_uuid:
            raise ReconciliationAuthorizationError("Evidence file is not linked to the requested conversation.")

        # 4. Check Quarantined / Malicious Evidence
        is_quarantined = evidence.status == "FLAGGED_UNSAFE" or evidence.is_malicious
        quarantine_reason = evidence.quarantine_reason

        # 5. Extract Customer Claims from Text
        claims_payload = self.claims_engine.extract_claims(
            text=request.claim_text,
            message_id=str(message_uuid) if message_uuid else None,
        )

        # 6. Asynchronous Background Queuing Mode
        if request.async_job:
            job_id = str(uuid.uuid4())
            serialized_claims = [c.model_dump(mode="json") for c in claims_payload.claims]
            verification = EvidenceVerification(
                evidence_file_id=evidence_uuid,
                conversation_id=conv_uuid,
                message_id=message_uuid,
                claim_text=claims_payload.pii_masked_text,
                matches_order_id=None,
                matches_amount=None,
                matches_product=None,
                overall_match_status="PARTIAL_MISMATCH",
                reconciliation_status=ReconciliationOutcome.UNCERTAIN.value,
                clarification_status=ClarificationStatus.NONE_REQUIRED.value,
                clarification_prompt=None,
                extracted_claims=serialized_claims,
                field_comparisons={"results": [], "summary": "Job queued for asynchronous background processing."},
                overall_confidence=0.0,
                job_id=job_id,
                job_status="PENDING",
                retry_count=0,
                idempotency_key=request.idempotency_key,
                created_at=utc_now(),
                processing_started_at=None,
                verified_at=None,
            )
            db.add(verification)
            await db.commit()
            await db.refresh(verification)
            logger.info("Queued async reconciliation job %s for conversation %s", job_id, conv_uuid)
            return self._to_response(verification)

        # 7. Synchronous Fast-Path: Reconstruct StructuredInvoiceData
        invoice_data: Optional[StructuredInvoiceData] = None
        if evidence.extracted_entities:
            entity = evidence.extracted_entities[0]
            if entity.extraction_metadata:
                try:
                    invoice_data = StructuredInvoiceData(**entity.extraction_metadata)
                except Exception:
                    invoice_data = self._reconstruct_from_entity(entity)
            else:
                invoice_data = self._reconstruct_from_entity(entity)

        # 8. Run Reconciliation
        reconcile_result = self.reconciliation_engine.reconcile(
            claims_payload=claims_payload,
            evidence_data=invoice_data,
            evidence_id=str(evidence_uuid),
            conversation_id=str(conv_uuid),
            customer_id=str(conversation.customer_id) if conversation.customer_id else None,
            is_quarantined=is_quarantined,
            quarantine_reason=quarantine_reason,
        )

        # 9. Determine Clarification Need
        clarification_status = (
            ClarificationStatus.PENDING_CUSTOMER.value
            if reconcile_result.requires_clarification
            else ClarificationStatus.NONE_REQUIRED.value
        )

        # 10. Map Match Status
        match_status_map = {
            ReconciliationOutcome.MATCH: "VERIFIED_MATCH",
            ReconciliationOutcome.MISMATCH: "TOTAL_CONFLICT",
            ReconciliationOutcome.UNCERTAIN: "LOW_QUALITY",
            ReconciliationOutcome.INSUFFICIENT_EVIDENCE: "INSUFFICIENT_EVIDENCE",
            ReconciliationOutcome.MISSING_FROM_EVIDENCE: "PARTIAL_MISMATCH",
            ReconciliationOutcome.MISSING_FROM_MESSAGE: "PARTIAL_MISMATCH",
        }
        overall_match_status = match_status_map.get(
            reconcile_result.overall_outcome, "PARTIAL_MISMATCH"
        )

        job_id = str(uuid.uuid4())
        job_status = "COMPLETED"

        # 11. Persist EvidenceVerification Record
        serialized_claims = [c.model_dump(mode="json") for c in claims_payload.claims]
        serialized_comps = [c.model_dump(mode="json") for c in reconcile_result.field_comparisons]

        now = utc_now()
        verification = EvidenceVerification(
            evidence_file_id=evidence_uuid,
            conversation_id=conv_uuid,
            message_id=message_uuid,
            claim_text=claims_payload.pii_masked_text,
            matches_order_id=self._extract_match_flag(reconcile_result, "order_id"),
            matches_amount=self._extract_match_flag(reconcile_result, "total_amount"),
            matches_product=self._extract_match_flag(reconcile_result, "product_name"),
            overall_match_status=overall_match_status,
            reconciliation_status=reconcile_result.overall_outcome.value,
            clarification_status=clarification_status,
            clarification_prompt=reconcile_result.clarification_prompt,
            extracted_claims=serialized_claims,
            field_comparisons={"results": serialized_comps, "summary": reconcile_result.summary},
            overall_confidence=reconcile_result.overall_confidence,
            job_id=job_id,
            job_status=job_status,
            retry_count=0,
            idempotency_key=request.idempotency_key,
            created_at=now,
            processing_started_at=now,
            verified_at=now,
        )

        db.add(verification)
        await db.commit()
        await db.refresh(verification)

        logger.info(
            "Reconciliation evaluated successfully: id=%s outcome=%s conf=%.2f",
            verification.id,
            verification.reconciliation_status,
            verification.overall_confidence,
        )

        return self._to_response(verification)

    async def get_verification(
        self,
        db: AsyncSession,
        verification_id: str,
        customer_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        current_user: Optional[User] = None,
    ) -> ReconciliationResponse:
        """Fetch a verification record by ID with multi-tenant protection."""

        try:
            verif_uuid = uuid.UUID(verification_id)
        except (ValueError, TypeError):
            raise PlatformException("Invalid verification_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        stmt = select(EvidenceVerification).where(EvidenceVerification.id == verif_uuid)
        res = await db.execute(stmt)
        verification = res.scalar_one_or_none()
        if not verification:
            raise ReconciliationNotFoundException(f"Verification '{verification_id}' not found.")

        # Tenant isolation
        is_staff = bool(roles and any(r in ("ADMIN", "AGENT", "SUPERVISOR") for r in roles))
        if not is_staff and verification.conversation_id:
            conv_stmt = select(Conversation).where(Conversation.id == verification.conversation_id)
            conv_res = await db.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if conv and conv.customer_id:
                resolved_customer_id = customer_id
                if current_user and getattr(current_user, "role", None) == "CUSTOMER":
                    resolved_customer_id = str(current_user.id)
                if resolved_customer_id and str(conv.customer_id) != str(resolved_customer_id):
                    raise ReconciliationAuthorizationError("Access denied: Verification belongs to another customer.")

        return self._to_response(verification)

    async def get_job_status(
        self,
        db: AsyncSession,
        job_id: str,
        customer_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        current_user: Optional[User] = None,
    ) -> ReconciliationJobStatusResponse:
        """Poll background reconciliation job status with strict multi-tenant authorization."""

        stmt = select(EvidenceVerification).where(EvidenceVerification.job_id == job_id)
        res = await db.execute(stmt)
        verification = res.scalar_one_or_none()
        if not verification:
            raise ReconciliationNotFoundException(f"Job '{job_id}' not found.")

        # Multi-tenant isolation: enforce conversation customer ownership for non-staff callers
        is_staff = bool(roles and any(r in ("ADMIN", "AGENT", "SUPERVISOR") for r in roles))
        if not is_staff and (customer_id is not None or roles is not None or current_user is not None):
            if not verification.conversation_id:
                raise ReconciliationAuthorizationError("Access denied: Job is not linked to a conversation.")

            conv_stmt = select(Conversation).where(Conversation.id == verification.conversation_id)
            conv_res = await db.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if not conv:
                raise ReconciliationAuthorizationError("Access denied: Associated conversation not found.")

            resolved_customer_id = customer_id
            if current_user and getattr(current_user, "role", None) == "CUSTOMER":
                resolved_customer_id = str(current_user.id)

            if not resolved_customer_id or str(conv.customer_id) != str(resolved_customer_id):
                logger.warning(
                    "Reconciliation job status multi-tenant violation: customer %s tried accessing job %s owned by %s",
                    resolved_customer_id,
                    job_id,
                    conv.customer_id,
                )
                raise ReconciliationAuthorizationError("Access denied: Job belongs to another customer.")

        updated_at = verification.verified_at or verification.processing_started_at or verification.created_at or utc_now()

        return ReconciliationJobStatusResponse(
            job_id=job_id,
            job_status=verification.job_status,
            verification_id=str(verification.id),
            reconciliation_status=verification.reconciliation_status,
            error_details=verification.error_details,
            retry_count=verification.retry_count,
            updated_at=updated_at,
        )

    async def get_conversation_clarifications(
        self,
        db: AsyncSession,
        conversation_id: str,
        customer_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        current_user: Optional[User] = None,
    ) -> List[ClarificationItemResponse]:
        """List active or outstanding clarifications for a conversation."""

        try:
            conv_uuid = uuid.UUID(conversation_id)
        except (ValueError, TypeError):
            raise PlatformException("Invalid conversation_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        # Verify conversation ownership
        conv_stmt = select(Conversation).where(Conversation.id == conv_uuid)
        conv_res = await db.execute(conv_stmt)
        conv = conv_res.scalar_one_or_none()
        if not conv:
            raise ReconciliationNotFoundException(f"Conversation '{conversation_id}' not found.")

        is_staff = bool(roles and any(r in ("ADMIN", "AGENT", "SUPERVISOR") for r in roles))
        resolved_customer_id = customer_id
        if current_user and getattr(current_user, "role", None) == "CUSTOMER":
            resolved_customer_id = str(current_user.id)
        if not is_staff and resolved_customer_id and conv.customer_id and str(conv.customer_id) != str(resolved_customer_id):
            raise ReconciliationAuthorizationError("Access denied: Conversation belongs to another customer.")

        stmt = (
            select(EvidenceVerification)
            .where(
                EvidenceVerification.conversation_id == conv_uuid,
                EvidenceVerification.clarification_status == ClarificationStatus.PENDING_CUSTOMER.value,
            )
            .order_by(EvidenceVerification.verified_at.desc())
        )
        res = await db.execute(stmt)
        records = res.scalars().all()

        items: List[ClarificationItemResponse] = []
        for r in records:
            comps = r.field_comparisons.get("results", []) if isinstance(r.field_comparisons, dict) else []
            mismatched = [c["field_name"] for c in comps if c.get("outcome") == "MISMATCH"]
            missing = [c["field_name"] for c in comps if c.get("outcome") == "MISSING_FROM_EVIDENCE"]
            uncertain = [c["field_name"] for c in comps if c.get("outcome") == "UNCERTAIN"]

            items.append(
                ClarificationItemResponse(
                    verification_id=str(r.id),
                    conversation_id=str(r.conversation_id),
                    status=r.clarification_status,
                    prompt_message=r.clarification_prompt or "Clarification required for evidence discrepancies.",
                    mismatched_fields=mismatched,
                    missing_fields=missing,
                    uncertain_fields=uncertain,
                    created_at=r.verified_at,
                    resolved_at=r.resolved_at,
                )
            )

        return items

    async def respond_to_clarification(
        self,
        db: AsyncSession,
        verification_id: str,
        payload: ClarificationResponseRequest,
        customer_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        current_user: Optional[User] = None,
    ) -> ReconciliationResponse:
        """Process customer clarification response and re-evaluate claims against evidence."""

        try:
            verif_uuid = uuid.UUID(verification_id)
        except (ValueError, TypeError):
            raise PlatformException("Invalid verification_id UUID format.", status_code=400, error_code="BAD_REQUEST")

        stmt = (
            select(EvidenceVerification)
            .where(EvidenceVerification.id == verif_uuid)
            .options(selectinload(EvidenceVerification.evidence_file))
        )
        res = await db.execute(stmt)
        verification = res.scalar_one_or_none()
        if not verification:
            raise ReconciliationNotFoundException(f"Verification '{verification_id}' not found.")

        # Multi-tenant check
        is_staff = bool(roles and any(r in ("ADMIN", "AGENT", "SUPERVISOR") for r in roles))
        if not is_staff and verification.conversation_id:
            conv_stmt = select(Conversation).where(Conversation.id == verification.conversation_id)
            conv_res = await db.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if conv and conv.customer_id:
                resolved_customer_id = customer_id
                if current_user and getattr(current_user, "role", None) == "CUSTOMER":
                    resolved_customer_id = str(current_user.id)
                if resolved_customer_id and str(conv.customer_id) != str(resolved_customer_id):
                    raise ReconciliationAuthorizationError("Access denied: Verification belongs to another customer.")

        # Re-fetch evidence with entities
        ev_id = payload.attachment_evidence_id or str(verification.evidence_file_id)
        ev_stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == uuid.UUID(ev_id))
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        ev_res = await db.execute(ev_stmt)
        evidence = ev_res.scalar_one_or_none()
        if not evidence:
            raise ReconciliationNotFoundException(f"Evidence '{ev_id}' not found.")

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

        # Build original claims payload
        base_claims_payload = self.claims_engine.extract_claims(
            text=verification.claim_text,
            message_id=str(verification.message_id) if verification.message_id else None,
        )

        # Run re-evaluation
        re_result, new_status = self.clarification_engine.re_evaluate(
            original_claims=base_claims_payload,
            evidence_data=invoice_data,
            customer_response_text=payload.response_text,
            verification_id=verification_id,
            evidence_id=str(evidence.id),
            conversation_id=str(verification.conversation_id) if verification.conversation_id else None,
            customer_id=customer_id,
        )

        match_status_map = {
            ReconciliationOutcome.MATCH: "VERIFIED_MATCH",
            ReconciliationOutcome.MISMATCH: "TOTAL_CONFLICT",
            ReconciliationOutcome.UNCERTAIN: "LOW_QUALITY",
            ReconciliationOutcome.INSUFFICIENT_EVIDENCE: "INSUFFICIENT_EVIDENCE",
            ReconciliationOutcome.MISSING_FROM_EVIDENCE: "PARTIAL_MISMATCH",
            ReconciliationOutcome.MISSING_FROM_MESSAGE: "PARTIAL_MISMATCH",
        }

        # Update record
        serialized_comps = [c.model_dump(mode="json") for c in re_result.field_comparisons]
        verification.reconciliation_status = re_result.overall_outcome.value
        verification.overall_match_status = match_status_map.get(
            re_result.overall_outcome, "PARTIAL_MISMATCH"
        )
        verification.clarification_status = new_status.value
        verification.clarification_prompt = re_result.clarification_prompt
        verification.overall_confidence = re_result.overall_confidence
        verification.field_comparisons = {"results": serialized_comps, "summary": re_result.summary}
        if new_status == ClarificationStatus.RESOLVED:
            verification.resolved_at = utc_now()

        await db.commit()
        await db.refresh(verification)

        logger.info(
            "Clarification response evaluated: verification=%s status=%s outcome=%s",
            verification.id,
            verification.clarification_status,
            verification.reconciliation_status,
        )

        return self._to_response(verification)

    async def recover_stale_jobs(
        self,
        db: AsyncSession,
        max_age_seconds: int = 300,
    ) -> int:
        """Crash-recovery scanner: marks stuck jobs as FAILED or increments retry count."""

        cutoff = utc_now() - timedelta(seconds=max_age_seconds)
        stale_condition = and_(
            EvidenceVerification.job_status.in_(["PENDING", "PROCESSING"]),
            or_(
                # Processing jobs stuck past threshold
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
                # Pending jobs stuck past threshold
                and_(
                    EvidenceVerification.job_status == "PENDING",
                    EvidenceVerification.created_at < cutoff,
                ),
                # Null verified_at stuck past threshold
                and_(
                    EvidenceVerification.verified_at.is_(None),
                    EvidenceVerification.created_at < cutoff,
                ),
                # Verified_at past cutoff (for backward compatibility)
                and_(
                    EvidenceVerification.verified_at.is_not(None),
                    EvidenceVerification.verified_at < cutoff,
                ),
            ),
        )

        stmt = select(EvidenceVerification).where(stale_condition)
        res = await db.execute(stmt)
        stale_records = res.scalars().all()

        recovered_count = 0
        for r in stale_records:
            if r.retry_count < 3:
                r.retry_count += 1
                r.error_details = f"Recovered after timeout at {utc_now().isoformat()}"
                r.job_status = "PENDING"
            else:
                r.job_status = "FAILED"
                r.error_details = f"Job permanently failed after {r.retry_count} retries (timeout)."
            recovered_count += 1

        if recovered_count > 0:
            await db.commit()
            logger.warning("Recovered %d stale reconciliation jobs", recovered_count)

        return recovered_count

    def _extract_match_flag(self, result: ReconciliationResult, field_name: str) -> Optional[bool]:
        for c in result.field_comparisons:
            if c.field_name == field_name:
                if c.outcome == ReconciliationOutcome.MATCH:
                    return True
                if c.outcome == ReconciliationOutcome.MISMATCH:
                    return False
        return None

    def _reconstruct_from_entity(self, entity: ExtractedEvidenceEntity) -> StructuredInvoiceData:
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

    def _to_response(self, v: EvidenceVerification) -> ReconciliationResponse:
        field_comps = []
        summary = ""
        if isinstance(v.field_comparisons, dict):
            field_comps = v.field_comparisons.get("results", [])
            summary = v.field_comparisons.get("summary", "")
        elif isinstance(v.field_comparisons, list):
            field_comps = v.field_comparisons

        return ReconciliationResponse(
            verification_id=str(v.id),
            evidence_id=str(v.evidence_file_id),
            conversation_id=str(v.conversation_id) if v.conversation_id else "",
            message_id=str(v.message_id) if v.message_id else None,
            job_id=v.job_id,
            job_status=v.job_status,
            reconciliation_status=v.reconciliation_status,
            overall_match_status=v.overall_match_status,
            overall_confidence=v.overall_confidence,
            summary=summary or f"Reconciliation status: {v.reconciliation_status}",
            clarification_status=v.clarification_status,
            clarification_prompt=v.clarification_prompt,
            field_comparisons=field_comps,
            extracted_claims=v.extracted_claims if isinstance(v.extracted_claims, list) else [],
            is_quarantined=False,
            created_at=v.created_at,
            processing_started_at=v.processing_started_at,
            verified_at=v.verified_at,
            resolved_at=v.resolved_at,
        )


default_reconciliation_service = ReconciliationService()
