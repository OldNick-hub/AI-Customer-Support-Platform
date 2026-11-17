"""Evidence Intake & OCR Service.

Coordinates validation, disk staging, AES-256-GCM encryption at rest,
deduplication idempotency, multi-tenant access control, and synchronous/asynchronous
document extraction pipelines.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional, Tuple
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import ResolvedAuthContext
from app.core.clock import system_clock
from app.core.config import settings
from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.core.pii import PIIMaskingEngine
from app.db.base import utc_now
from app.models.chat import ChatOutboxEvent
from app.models.customer import Conversation, Customer, Message
from app.models.evidence import EvidenceFile, ExtractedEvidenceEntity
from app.modules.multilingual.outbox_worker import generate_outbox_event_id
from app.modules.evidence.pipeline import EvidenceProcessingPipeline, PipelineProcessingResult
from app.modules.evidence.schemas import (
    EvidenceDetailResponse,
    EvidenceExtractionResponse,
    EvidenceStatusResponse,
    EvidenceUploadResponse,
    StructuredInvoiceData,
)
from app.modules.evidence.storage import EvidenceStorageManager, default_evidence_storage
from app.modules.evidence.validator import FileValidator, ValidatedFileMetadata
from app.services.storage_service import validate_tenant_id

logger = configure_safe_logger(logging.getLogger(__name__))

APPROVED_STAFF_ROLES = {"ADMIN", "SUPERVISOR", "AGENT", "STAFF", "SUPPORT_AGENT", "SUPER_ADMIN", "GLOBAL_ADMIN"}
GLOBAL_ADMIN_ROLES = {"SUPER_ADMIN", "GLOBAL_ADMIN"}


class EvidenceAuthorizationError(PlatformException):
    """Raised when a customer attempts to access evidence belonging to another tenant."""
    status_code: int = 403
    error_code: str = "FORBIDDEN"
    default_message: str = "Access to requested evidence is forbidden."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message=message or self.default_message, status_code=403, error_code="FORBIDDEN")


class EvidenceNotFoundException(PlatformException):
    """Raised when requested evidence record is not found."""
    status_code: int = 404
    error_code: str = "NOT_FOUND"
    default_message: str = "Requested evidence file was not found."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message=message or self.default_message, status_code=404, error_code="NOT_FOUND")


class EvidenceService:
    """Core domain service for Multimodal Evidence Intake & OCR."""

    def __init__(
        self,
        storage_manager: Optional[EvidenceStorageManager] = None,
        file_validator: Optional[FileValidator] = None,
        pipeline: Optional[EvidenceProcessingPipeline] = None,
    ):
        self.storage = storage_manager or default_evidence_storage
        self.validator = file_validator or FileValidator()
        self.pipeline = pipeline or EvidenceProcessingPipeline()

    async def upload_evidence(
        self,
        db: AsyncSession,
        auth_context: ResolvedAuthContext,
        file_bytes: bytes,
        client_filename: str,
        client_content_type: str,
        conversation_id: uuid.UUID,
        customer_id: Optional[uuid.UUID] = None,
        message_id: Optional[uuid.UUID] = None,
        ticket_id: Optional[uuid.UUID] = None,
        sync_processing: bool = True,
        sync_timeout_seconds: Optional[float] = None,
    ) -> EvidenceUploadResponse:
        """Validates, stores, deduplicates, and extracts multimodal evidence."""
        # 1. Authoritative Identity Resolution
        resolved_customer_id: Optional[uuid.UUID] = None
        if auth_context.customer_id:
            resolved_customer_id = uuid.UUID(auth_context.customer_id)
        elif customer_id:
            resolved_customer_id = customer_id

        # 2. Verify Conversation Existence & Ownership
        conv_stmt = select(Conversation).where(Conversation.id == conversation_id)
        conv_res = await db.execute(conv_stmt)
        conversation = conv_res.scalar_one_or_none()
        if not conversation:
            raise EvidenceNotFoundException(f"Conversation '{conversation_id}' not found.")

        # Multi-tenant cross-customer protection
        if resolved_customer_id and conversation.customer_id and conversation.customer_id != resolved_customer_id:
            logger.warning(
                "Multi-tenant violation: Customer %s attempted upload to conversation %s owned by %s",
                resolved_customer_id,
                conversation_id,
                conversation.customer_id,
            )
            raise EvidenceAuthorizationError("Cannot upload evidence to another customer's conversation.")

        # If conversation has a customer, ensure evidence inherits it
        if not resolved_customer_id and conversation.customer_id:
            resolved_customer_id = conversation.customer_id

        # 3. File Signature & Security Validation
        validated: ValidatedFileMetadata = self.validator.validate(
            file_bytes=file_bytes,
            client_file_name=client_filename,
            client_mime_type=client_content_type,
        )

        # 4. Idempotency & Deduplication Check (Same conversation + same SHA-256 hash)
        existing_stmt = (
            select(EvidenceFile)
            .where(
                EvidenceFile.conversation_id == conversation_id,
                EvidenceFile.sha256_hash == validated.sha256_hash,
            )
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        existing_res = await db.execute(existing_stmt)
        existing_evidence = existing_res.scalar_one_or_none()

        if existing_evidence:
            logger.info(
                "Idempotent upload hit: Evidence %s already exists for hash %s in conversation %s",
                existing_evidence.id,
                validated.sha256_hash,
                conversation_id,
            )
            structured_data: Optional[StructuredInvoiceData] = None
            if existing_evidence.extracted_entities:
                entity = existing_evidence.extracted_entities[0]
                structured_data = StructuredInvoiceData(**entity.extraction_metadata)

            return EvidenceUploadResponse(
                evidence_id=str(existing_evidence.id),
                conversation_id=str(existing_evidence.conversation_id),
                customer_id=str(existing_evidence.customer_id) if existing_evidence.customer_id else None,
                file_name=existing_evidence.file_name,
                mime_type=existing_evidence.mime_type,
                file_size_bytes=existing_evidence.file_size_bytes,
                sha256_hash=existing_evidence.sha256_hash,
                status=existing_evidence.status,
                is_malicious=existing_evidence.is_malicious,
                quarantine_reason=existing_evidence.quarantine_reason,
                retention_expires_at=existing_evidence.retention_expires_at,
                created_at=existing_evidence.created_at,
                structured_entities=structured_data,
            )

        # 5. Staged Disk Persistence with AES-256-GCM Encryption
        evidence_id = uuid.uuid4()
        storage_uri = self.storage.store_file(
            file_bytes=validated.file_bytes,
            evidence_id=evidence_id,
            customer_id=resolved_customer_id,
            extension=validated.canonical_extension,
        )

        now = system_clock.now()
        retention_days = getattr(settings, "EVIDENCE_RETENTION_DAYS", 30)
        retention_expires_at = now + timedelta(days=retention_days)

        evidence_file = EvidenceFile(
            id=evidence_id,
            message_id=message_id,
            conversation_id=conversation_id,
            customer_id=resolved_customer_id,
            ticket_id=ticket_id,
            file_name=validated.sanitized_client_name,
            mime_type=validated.detected_mime_type,
            file_size_bytes=validated.file_size_bytes,
            storage_uri=storage_uri,
            sha256_hash=validated.sha256_hash,
            status="STAGED",
            is_malicious=False,
            retention_expires_at=retention_expires_at,
            created_at=now,
        )
        db.add(evidence_file)
        await db.flush()

        # 6. Extraction Pipeline: Synchronous vs Asynchronous (with 30-second timeout fallback)
        structured_output: Optional[StructuredInvoiceData] = None
        timeout_seconds = (
            sync_timeout_seconds
            if sync_timeout_seconds is not None
            else getattr(settings, "EVIDENCE_SYNC_TIMEOUT_SECONDS", 30.0)
        )

        if sync_processing:
            evidence_file.status = "ANALYZING"
            await db.flush()

            try:
                pipeline_result: PipelineProcessingResult = await asyncio.wait_for(
                    self.pipeline.process_document(
                        file_bytes=validated.file_bytes,
                        mime_type=validated.detected_mime_type,
                        file_name=validated.sanitized_client_name,
                    ),
                    timeout=timeout_seconds,
                )

                evidence_file.status = pipeline_result.status
                evidence_file.is_malicious = pipeline_result.is_malicious
                evidence_file.quarantine_reason = pipeline_result.quarantine_reason
                evidence_file.processing_error = pipeline_result.processing_error
                evidence_file.processed_at = system_clock.now()
                structured_output = pipeline_result.structured_entities

                # Persist extracted entity record
                extracted_entity = ExtractedEvidenceEntity(
                    id=uuid.uuid4(),
                    evidence_file_id=evidence_id,
                    extracted_order_id=structured_output.order_id,
                    extracted_date=structured_output.invoice_date or structured_output.transaction_date,
                    extracted_amount=structured_output.total_amount,
                    extracted_currency=structured_output.currency,
                    extracted_product_sku=structured_output.product_name,
                    raw_ocr_text=PIIMaskingEngine.mask_text(pipeline_result.raw_ocr_text),
                    ocr_confidence=pipeline_result.ocr_confidence,
                    pii_redacted_text=pipeline_result.pii_redacted_text,
                    extraction_metadata=structured_output.model_dump(mode="json"),
                    extracted_at=system_clock.now(),
                )
                db.add(extracted_entity)
            except asyncio.TimeoutError:
                logger.warning(
                    "Evidence %s sync processing exceeded %.1fs timeout. Transitioning to background STAGED.",
                    evidence_id,
                    timeout_seconds,
                )
                evidence_file.status = "STAGED"
                evidence_file.processing_error = (
                    f"Sync processing exceeded {timeout_seconds}s timeout; transitioned to background processing."
                )
                structured_output = None

        await db.commit()
        await db.refresh(evidence_file)

        return EvidenceUploadResponse(
            evidence_id=str(evidence_file.id),
            conversation_id=str(evidence_file.conversation_id),
            customer_id=str(evidence_file.customer_id) if evidence_file.customer_id else None,
            file_name=evidence_file.file_name,
            mime_type=evidence_file.mime_type,
            file_size_bytes=evidence_file.file_size_bytes,
            sha256_hash=evidence_file.sha256_hash,
            status=evidence_file.status,
            is_malicious=evidence_file.is_malicious,
            quarantine_reason=evidence_file.quarantine_reason,
            processing_error=evidence_file.processing_error,
            retention_expires_at=evidence_file.retention_expires_at,
            created_at=evidence_file.created_at,
            structured_entities=structured_output,
        )

    async def get_evidence(
        self,
        db: AsyncSession,
        evidence_id: uuid.UUID,
        auth_context: ResolvedAuthContext,
    ) -> EvidenceDetailResponse:
        """Retrieves evidence details with strict multi-tenant authorization."""
        stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == evidence_id)
            .options(
                selectinload(EvidenceFile.extracted_entities),
                selectinload(EvidenceFile.customer),
            )
        )
        res = await db.execute(stmt)
        evidence = res.scalar_one_or_none()

        if not evidence:
            raise EvidenceNotFoundException(f"Evidence file '{evidence_id}' not found.")

        # Multi-tenant Authorization Check
        self._check_evidence_access(evidence, auth_context)

        return EvidenceDetailResponse(
            evidence_id=str(evidence.id),
            conversation_id=str(evidence.conversation_id) if evidence.conversation_id else None,
            customer_id=str(evidence.customer_id) if evidence.customer_id else None,
            message_id=str(evidence.message_id) if evidence.message_id else None,
            ticket_id=str(evidence.ticket_id) if evidence.ticket_id else None,
            file_name=evidence.file_name,
            mime_type=evidence.mime_type,
            file_size_bytes=evidence.file_size_bytes,
            sha256_hash=evidence.sha256_hash,
            status=evidence.status,
            is_malicious=evidence.is_malicious,
            quarantine_reason=evidence.quarantine_reason,
            processing_error=evidence.processing_error,
            retention_expires_at=evidence.retention_expires_at,
            created_at=evidence.created_at,
            processed_at=evidence.processed_at,
        )

    async def get_evidence_status(
        self,
        db: AsyncSession,
        evidence_id: uuid.UUID,
        auth_context: ResolvedAuthContext,
    ) -> EvidenceStatusResponse:
        """Lightweight status polling endpoint for asynchronous evidence processing."""
        stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == evidence_id)
            .options(selectinload(EvidenceFile.customer))
        )
        res = await db.execute(stmt)
        evidence = res.scalar_one_or_none()

        if not evidence:
            raise EvidenceNotFoundException(f"Evidence file '{evidence_id}' not found.")

        self._check_evidence_access(evidence, auth_context)

        return EvidenceStatusResponse(
            evidence_id=str(evidence.id),
            status=evidence.status,
            is_malicious=evidence.is_malicious,
            quarantine_reason=evidence.quarantine_reason,
            processing_error=evidence.processing_error,
            processed_at=evidence.processed_at,
        )

    async def get_evidence_extraction(
        self,
        db: AsyncSession,
        evidence_id: uuid.UUID,
        auth_context: ResolvedAuthContext,
    ) -> EvidenceExtractionResponse:
        """Retrieves authorized structured entities parsed from an evidence document."""
        stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == evidence_id)
            .options(
                selectinload(EvidenceFile.extracted_entities),
                selectinload(EvidenceFile.customer),
            )
        )
        res = await db.execute(stmt)
        evidence = res.scalar_one_or_none()

        if not evidence:
            raise EvidenceNotFoundException(f"Evidence file '{evidence_id}' not found.")

        self._check_evidence_access(evidence, auth_context)

        if not evidence.extracted_entities:
            raise EvidenceNotFoundException(f"No structured extraction results found for evidence '{evidence_id}'.")

        entity = evidence.extracted_entities[0]
        structured_data = StructuredInvoiceData(**entity.extraction_metadata)

        return EvidenceExtractionResponse(
            evidence_id=str(evidence.id),
            status=evidence.status,
            extracted_at=entity.extracted_at,
            ocr_confidence=entity.ocr_confidence,
            pii_redacted_text=entity.pii_redacted_text,
            entities=structured_data,
        )

    def _check_evidence_access(
        self,
        evidence: EvidenceFile,
        auth_context: ResolvedAuthContext,
        customer: Optional[Customer] = None,
    ) -> None:
        """Enforces multi-tenant customer boundary and staff RBAC.

        Fail-Closed Security Invariant:
        - GLOBAL_ADMIN and SUPER_ADMIN have authorized platform-wide access.
        - Tenant-scoped staff (ADMIN, SUPERVISOR, AGENT, STAFF, SUPPORT_AGENT) MUST:
            1. Have a valid, non-empty tenant_id claim in their JWT.
            2. Evidence must have an associated customer ownership.
            3. Customer must exist and have non-empty tenant_id metadata matching staff tenant.
        - Customer callers (SESSION_TOKEN) MUST:
            1. Have a verified customer_id.
            2. Evidence must have an associated customer ownership.
            3. Both customer IDs must strictly match.
        - Missing caller tenant, missing evidence ownership, missing customer metadata, or unrecognized roles fail closed.
        """
        # Staff caller authorization
        if auth_context.auth_type == "JWT":
            staff_roles = {r.upper() for r in (auth_context.roles or [])}
            if not staff_roles.intersection(APPROVED_STAFF_ROLES):
                raise EvidenceAuthorizationError("A recognized staff role is required to access evidence.")

            is_global_admin = bool(staff_roles.intersection(GLOBAL_ADMIN_ROLES))
            if is_global_admin:
                return

            raw_tenant = (auth_context.jwt_claims or {}).get("tenant_id")
            if not raw_tenant or not isinstance(raw_tenant, str) or not raw_tenant.strip():
                raise EvidenceAuthorizationError("Staff authentication token must contain a valid, non-empty tenant_id claim.")

            try:
                staff_tenant = validate_tenant_id(raw_tenant)
            except Exception:
                raise EvidenceAuthorizationError("Invalid tenant identifier.")

            if not evidence.customer_id:
                raise EvidenceAuthorizationError("Evidence has no associated customer ownership.")

            cust = customer or evidence.customer
            if cust is None:
                raise EvidenceAuthorizationError("Evidence customer has no associated tenant metadata.")

            cust_tenant = (cust.metadata_ or {}).get("tenant_id")
            if not cust_tenant or not isinstance(cust_tenant, str) or not cust_tenant.strip():
                raise EvidenceAuthorizationError("Evidence customer has no associated tenant metadata.")

            if cust_tenant.strip() != staff_tenant:
                logger.warning(
                    "Tenant isolation violation: Staff %s (tenant: %s) attempted access to evidence %s (tenant: %s)",
                    auth_context.user_id_or_email,
                    staff_tenant,
                    evidence.id,
                    cust_tenant.strip(),
                )
                raise EvidenceAuthorizationError("You do not have permission to access evidence outside your tenant.")
            return

        elif auth_context.auth_type == "SESSION_TOKEN":
            if not auth_context.customer_id:
                logger.warning(
                    "Evidence access denied: Customer caller has no customer identity for evidence %s",
                    evidence.id,
                )
                raise EvidenceAuthorizationError("Authentication with customer identity is required to access evidence.")

            if not evidence.customer_id:
                logger.warning(
                    "Evidence access denied: Evidence %s has no associated customer ownership",
                    evidence.id,
                )
                raise EvidenceAuthorizationError("Evidence has no associated customer ownership.")

            try:
                auth_cust_id = uuid.UUID(str(auth_context.customer_id))
            except (ValueError, TypeError):
                raise EvidenceAuthorizationError("Invalid customer identity context.")

            if evidence.customer_id != auth_cust_id:
                logger.warning(
                    "Multi-tenant violation: Customer %s attempted access to evidence %s owned by %s",
                    auth_cust_id,
                    evidence.id,
                    evidence.customer_id,
                )
                raise EvidenceAuthorizationError("You do not have permission to access this evidence.")
            return

        else:
            raise EvidenceAuthorizationError("Valid customer session or staff authentication is required.")

    async def process_staged_evidence(
        self,
        db: AsyncSession,
        evidence_id: uuid.UUID,
    ) -> Optional[EvidenceFile]:
        """Claims and executes extraction on a STAGED evidence file in the background."""
        stmt = (
            select(EvidenceFile)
            .where(EvidenceFile.id == evidence_id, EvidenceFile.status == "STAGED")
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        bind = db.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        res = await db.execute(stmt)
        evidence = res.scalar_one_or_none()
        if not evidence:
            return None

        evidence.status = "ANALYZING"
        await db.flush()

        try:
            file_bytes = self.storage.load_file(storage_uri=evidence.storage_uri, evidence_id=evidence.id)
            pipeline_result = await self.pipeline.process_document(
                file_bytes=file_bytes,
                mime_type=evidence.mime_type,
                file_name=evidence.file_name,
            )

            evidence.status = pipeline_result.status
            evidence.is_malicious = pipeline_result.is_malicious
            evidence.quarantine_reason = pipeline_result.quarantine_reason
            evidence.processing_error = pipeline_result.processing_error
            evidence.processed_at = system_clock.now()

            structured_output = pipeline_result.structured_entities
            extracted_entity = ExtractedEvidenceEntity(
                id=uuid.uuid4(),
                evidence_file_id=evidence.id,
                extracted_order_id=structured_output.order_id,
                extracted_date=structured_output.invoice_date or structured_output.transaction_date,
                extracted_amount=structured_output.total_amount,
                extracted_currency=structured_output.currency,
                extracted_product_sku=structured_output.product_name,
                raw_ocr_text=PIIMaskingEngine.mask_text(pipeline_result.raw_ocr_text),
                ocr_confidence=pipeline_result.ocr_confidence,
                pii_redacted_text=pipeline_result.pii_redacted_text,
                extraction_metadata=structured_output.model_dump(mode="json"),
                extracted_at=system_clock.now(),
            )
            db.add(extracted_entity)

            # Stage outbox notification event transactionally with evidence processing
            await self._stage_evidence_notification(db, evidence)

            await db.commit()
            await db.refresh(evidence)
            return evidence
        except Exception as exc:
            safe_err = PIIMaskingEngine.mask_text(str(exc))
            logger.error("Error processing staged evidence %s: %s", evidence_id, safe_err)
            evidence.status = "STAGED"
            evidence.processing_error = safe_err
            await db.commit()
            return evidence

    async def _stage_evidence_notification(
        self, db: AsyncSession, evidence: EvidenceFile
    ) -> Optional[ChatOutboxEvent]:
        """Stages an idempotent transactional ChatOutboxEvent notifying customer of completed evidence processing."""
        idempotency_key = f"evidence-notification:{evidence.id}"

        # 1. Idempotency check: don't stage duplicate notification events
        existing_stmt = select(ChatOutboxEvent).where(ChatOutboxEvent.idempotency_key == idempotency_key)
        existing_event = (await db.execute(existing_stmt)).scalar_one_or_none()
        if existing_event:
            logger.info("Notification event for evidence %s already staged; skipping.", evidence.id)
            return existing_event

        # 2. Check conversation association
        if not evidence.conversation_id:
            logger.info("No conversation associated with evidence %s; skipping chat notification.", evidence.id)
            return None

        conv_stmt = select(Conversation).where(Conversation.id == evidence.conversation_id)
        conv_res = await db.execute(conv_stmt)
        conversation = conv_res.scalar_one_or_none()
        if not conversation:
            logger.warning("Conversation %s not found for evidence %s; skipping chat notification.", evidence.conversation_id, evidence.id)
            return None

        customer_id = evidence.customer_id or conversation.customer_id
        session_id = conversation.session_id
        if not customer_id or not session_id:
            logger.warning("Customer or session not resolved for conversation %s; skipping chat notification.", conversation.id)
            return None

        # 3. Determine user-facing notification result and message
        # Invariant: Never disclose raw OCR, payment credentials, or internal security data
        if evidence.status in ("VERIFIED", "EXTRACTED"):
            result_status = "VERIFIED"
            user_message = f"Your uploaded document '{evidence.file_name}' has been processed and verified successfully."
        elif evidence.status in ("FLAGGED_UNSAFE", "CLAIM_CONFLICT") or evidence.is_malicious:
            result_status = "REJECTED"
            user_message = f"Your uploaded document '{evidence.file_name}' could not be verified and has been rejected. Please review submission requirements."
        elif evidence.status in ("PROCESSING_FAILED", "FAILED"):
            result_status = "REQUIRES_ADDITIONAL_INFORMATION"
            user_message = f"We encountered difficulty reading your uploaded document '{evidence.file_name}'. Please provide a clearer or alternate document."
        else:
            result_status = "PROCESSED"
            user_message = f"Your uploaded document '{evidence.file_name}' processing is complete."

        # 4. Monotonic Sequence Allocation
        try:
            alloc_res = await db.execute(
                text("SELECT fn_allocate_next_turn_sequence(:cid)"),
                {"cid": str(conversation.id)},
            )
            allocated_turn_seq = int(alloc_res.scalar_one())
        except Exception:
            curr = int(conversation.turn_count) if conversation.turn_count else 0
            allocated_turn_seq = curr + 1
            conversation.turn_count = allocated_turn_seq
            db.add(conversation)

        # 5. Create paired messages for turn integrity
        now = system_clock.now()
        cust_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            sender_type="CUSTOMER",
            sender_id=customer_id,
            content=f"[Document Upload Processed: {evidence.file_name}]",
            detected_language="en",
            language_confidence=1.0,
            turn_sequence_number=allocated_turn_seq,
            created_at=now,
        )
        db.add(cust_msg)

        agent_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            sender_type="AI_AGENT",
            sender_id=None,
            content=user_message,
            detected_language="en",
            language_confidence=1.0,
            turn_sequence_number=allocated_turn_seq,
            created_at=now,
        )
        db.add(agent_msg)
        await db.flush()

        outbox_event_id = generate_outbox_event_id(conversation.id, allocated_turn_seq)
        payload = {
            "event_type": "EVIDENCE_PROCESSING_COMPLETED",
            "evidence_id": str(evidence.id),
            "file_name": evidence.file_name,
            "status": evidence.status,
            "result": result_status,
            "notification_message": user_message,
            "processed_at": (evidence.processed_at or now).isoformat(),
            "conversation_id": str(conversation.id),
            "turn_sequence_number": allocated_turn_seq,
        }

        outbox_event = ChatOutboxEvent(
            id=outbox_event_id,
            customer_id=customer_id,
            session_id=session_id,
            conversation_id=conversation.id,
            customer_message_id=cust_msg.id,
            agent_message_id=agent_msg.id,
            sequence_number=allocated_turn_seq,
            idempotency_key=idempotency_key,
            event_type="EVIDENCE_PROCESSING_COMPLETED",
            payload=payload,
            status="PENDING",
            created_at=now,
            next_retry_at=now,
        )
        db.add(outbox_event)
        await db.flush()
        return outbox_event

