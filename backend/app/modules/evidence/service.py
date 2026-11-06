"""Evidence Intake & OCR Service.

Coordinates validation, disk staging, AES-256-GCM encryption at rest,
deduplication idempotency, multi-tenant access control, and synchronous/asynchronous
document extraction pipelines.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Optional, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import ResolvedAuthContext
from app.core.clock import system_clock
from app.core.config import settings
from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.db.base import utc_now
from app.models.customer import Conversation, Customer
from app.models.evidence import EvidenceFile, ExtractedEvidenceEntity
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

logger = configure_safe_logger(logging.getLogger(__name__))


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

        # 6. Extraction Pipeline: Synchronous vs Asynchronous
        structured_output: Optional[StructuredInvoiceData] = None

        if sync_processing:
            evidence_file.status = "ANALYZING"
            await db.flush()

            pipeline_result: PipelineProcessingResult = await self.pipeline.process_document(
                file_bytes=validated.file_bytes,
                mime_type=validated.detected_mime_type,
                file_name=validated.sanitized_client_name,
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
                raw_ocr_text=pipeline_result.raw_ocr_text,
                ocr_confidence=pipeline_result.ocr_confidence,
                pii_redacted_text=pipeline_result.pii_redacted_text,
                extraction_metadata=structured_output.model_dump(mode="json"),
                extracted_at=system_clock.now(),
            )
            db.add(extracted_entity)

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
            .options(selectinload(EvidenceFile.extracted_entities))
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
        stmt = select(EvidenceFile).where(EvidenceFile.id == evidence_id)
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
            .options(selectinload(EvidenceFile.extracted_entities))
        )
        res = await db.execute(stmt)
        evidence = res.scalar_one_or_none()

        if not evidence:
            raise EvidenceNotFoundException(f"Evidence file '{evidence_id}' not found.")

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

    def _check_evidence_access(self, evidence: EvidenceFile, auth_context: ResolvedAuthContext) -> None:
        """Enforces multi-tenant customer boundary and staff RBAC."""
        is_staff = any(r in ("ADMIN", "AGENT", "STAFF") for r in auth_context.roles)
        if is_staff:
            return

        if auth_context.customer_id:
            auth_cust_id = uuid.UUID(auth_context.customer_id)
            if evidence.customer_id and evidence.customer_id != auth_cust_id:
                logger.warning(
                    "Multi-tenant violation: Customer %s attempted access to evidence %s owned by %s",
                    auth_cust_id,
                    evidence.id,
                    evidence.customer_id,
                )
                raise EvidenceAuthorizationError("You do not have permission to access this evidence.")

