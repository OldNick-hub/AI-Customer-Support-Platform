"""Multimodal Evidence Intake & OCR API Router.

Exposes REST endpoints for:
1. POST /api/v1/evidence/upload — Secure multipart evidence intake with signature validation.
2. GET  /api/v1/evidence/{id} — Detailed evidence record & retention metadata.
3. GET  /api/v1/evidence/{id}/status — Lightweight processing status polling.
4. GET  /api/v1/evidence/{id}/extraction — Authorized structured financial extraction results.
"""

import logging
from typing import Optional
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ResolvedAuthContext,
    get_auth_context,
    get_correlation_id,
    get_db,
)
from app.core.logging import configure_safe_logger
from app.modules.evidence.schemas import (
    EvidenceDetailResponse,
    EvidenceExtractionResponse,
    EvidenceStatusResponse,
    EvidenceUploadResponse,
)
from app.modules.evidence.service import (
    EvidenceAuthorizationError,
    EvidenceNotFoundException,
    EvidenceService,
)
from app.modules.evidence.validator import EvidenceValidationError

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/evidence", tags=["Multimodal Evidence & OCR"])

# Module service singleton
_evidence_service = EvidenceService()


def get_evidence_service() -> EvidenceService:
    return _evidence_service


@router.post(
    "/upload",
    response_model=EvidenceUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload multimodal evidence file (Screenshot, Receipt, Invoice, PDF)",
)
async def upload_evidence(
    request: Request,
    file: UploadFile = File(..., description="Multipart binary payload of JPEG, PNG, or PDF"),
    conversation_id: uuid.UUID = Form(..., description="Conversation thread UUID"),
    customer_id: Optional[uuid.UUID] = Form(None, description="Optional customer profile UUID"),
    message_id: Optional[uuid.UUID] = Form(None, description="Optional associated message UUID"),
    ticket_id: Optional[uuid.UUID] = Form(None, description="Optional associated ticket UUID"),
    sync_processing: bool = Form(True, description="Whether to execute OCR synchronously"),
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceUploadResponse:
    """Intakes customer-submitted evidence documents.

    Validates file signature magic bytes, enforces size limits (<= 15MB) and PDF page counts (<= 10),
    encrypts the payload at rest via AES-256-GCM, and executes OCR extraction.
    """
    try:
        file_bytes = await file.read()
        client_name = file.filename or "uploaded_evidence"
        client_content_type = file.content_type or "application/octet-stream"

        response = await evidence_service.upload_evidence(
            db=db,
            auth_context=auth_context,
            file_bytes=file_bytes,
            client_filename=client_name,
            client_content_type=client_content_type,
            conversation_id=conversation_id,
            customer_id=customer_id,
            message_id=message_id,
            ticket_id=ticket_id,
            sync_processing=sync_processing,
        )
        return response

    except EvidenceValidationError as exc:
        logger.warning("Evidence validation failed: %s (%s)", exc.message, exc.error_code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.message},
        )
    except EvidenceAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "FORBIDDEN", "message": exc.message},
        )
    except EvidenceNotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": exc.message},
        )
    except Exception as exc:
        logger.error("Unexpected error during evidence upload: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "INTERNAL_SERVER_ERROR", "message": "An error occurred while processing the evidence upload."},
        )


@router.get(
    "/{evidence_id}",
    response_model=EvidenceDetailResponse,
    summary="Get evidence metadata and status",
)
async def get_evidence(
    evidence_id: uuid.UUID,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceDetailResponse:
    """Retrieves metadata and processing state for a stored evidence file."""
    try:
        return await evidence_service.get_evidence(
            db=db,
            evidence_id=evidence_id,
            auth_context=auth_context,
        )
    except EvidenceAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "FORBIDDEN", "message": exc.message},
        )
    except EvidenceNotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": exc.message},
        )


@router.get(
    "/{evidence_id}/status",
    response_model=EvidenceStatusResponse,
    summary="Get lightweight evidence processing status",
)
async def get_evidence_status(
    evidence_id: uuid.UUID,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceStatusResponse:
    """Polls processing status for an evidence record."""
    try:
        return await evidence_service.get_evidence_status(
            db=db,
            evidence_id=evidence_id,
            auth_context=auth_context,
        )
    except EvidenceAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "FORBIDDEN", "message": exc.message},
        )
    except EvidenceNotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": exc.message},
        )


@router.get(
    "/{evidence_id}/extraction",
    response_model=EvidenceExtractionResponse,
    summary="Get structured OCR extraction results",
)
async def get_evidence_extraction(
    evidence_id: uuid.UUID,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceExtractionResponse:
    """Retrieves parsed financial invoice and receipt entities."""
    try:
        return await evidence_service.get_evidence_extraction(
            db=db,
            evidence_id=evidence_id,
            auth_context=auth_context,
        )
    except EvidenceAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "FORBIDDEN", "message": exc.message},
        )
    except EvidenceNotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": exc.message},
        )

