"""Evidence Reconciliation & Intelligent Mismatch Detection API Router.

Exposes REST endpoints for:
1. POST /api/v1/evidence/reconcile — Run or initiate evidence reconciliation against customer claims.
2. GET  /api/v1/evidence/reconcile/{id} — Fetch detailed verification verdict by ID.
3. GET  /api/v1/evidence/reconcile/status/{job_id} — Poll background reconciliation job status.
4. GET  /api/v1/evidence/reconcile/clarifications/{conversation_id} — Get active clarification requests.
5. POST /api/v1/evidence/reconcile/clarifications/{id}/respond — Customer clarification response & re-evaluation.
"""

import logging
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ResolvedAuthContext,
    get_auth_context,
    get_correlation_id,
    get_db,
)
from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.modules.evidence.reconciliation_service import (
    ReconciliationAuthorizationError,
    ReconciliationNotFoundException,
    ReconciliationService,
    default_reconciliation_service,
)
from app.modules.evidence.schemas import (
    ClarificationItemResponse,
    ClarificationResponseRequest,
    ReconciliationJobStatusResponse,
    ReconciliationRequest,
    ReconciliationResponse,
)

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/evidence/reconcile", tags=["Evidence Reconciliation & Mismatch Detection"])


def get_reconciliation_service() -> ReconciliationService:
    return default_reconciliation_service


@router.post(
    "",
    response_model=ReconciliationResponse,
    status_code=status.HTTP_200_OK,
    summary="Evaluate customer claims against uploaded evidence",
)
@router.post(
    "/",
    response_model=ReconciliationResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def reconcile_evidence(
    request: Request,
    payload: ReconciliationRequest,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    service: ReconciliationService = Depends(get_reconciliation_service),
) -> ReconciliationResponse:
    """Evaluates customer factual assertions against OCR-extracted document evidence."""
    try:
        return await service.evaluate_reconciliation(
            db=db,
            request=payload,
            customer_id=auth_context.customer_id,
            roles=auth_context.roles,
        )
    except PlatformException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get(
    "/status/{job_id}",
    response_model=ReconciliationJobStatusResponse,
    summary="Poll status of background reconciliation job",
)
async def get_reconciliation_job_status(
    job_id: str,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    service: ReconciliationService = Depends(get_reconciliation_service),
) -> ReconciliationJobStatusResponse:
    """Returns current status, retry count, and errors of an async reconciliation job."""
    try:
        return await service.get_job_status(
            db=db,
            job_id=job_id,
            customer_id=auth_context.customer_id,
            roles=auth_context.roles,
        )
    except PlatformException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get(
    "/clarifications/{conversation_id}",
    response_model=List[ClarificationItemResponse],
    summary="List active clarification requests for a conversation",
)
async def get_conversation_clarifications(
    conversation_id: str,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    service: ReconciliationService = Depends(get_reconciliation_service),
) -> List[ClarificationItemResponse]:
    """Retrieves all pending customer clarification inquiries for a conversation."""
    try:
        return await service.get_conversation_clarifications(
            db=db,
            conversation_id=conversation_id,
            customer_id=auth_context.customer_id,
            roles=auth_context.roles,
        )
    except PlatformException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post(
    "/clarifications/{verification_id}/respond",
    response_model=ReconciliationResponse,
    summary="Submit customer response to clarification request and re-evaluate",
)
async def respond_to_clarification(
    verification_id: str,
    payload: ClarificationResponseRequest,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    service: ReconciliationService = Depends(get_reconciliation_service),
) -> ReconciliationResponse:
    """Processes customer answer to clarification inquiry and re-runs reconciliation."""
    try:
        return await service.respond_to_clarification(
            db=db,
            verification_id=verification_id,
            payload=payload,
            customer_id=auth_context.customer_id,
            roles=auth_context.roles,
        )
    except PlatformException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get(
    "/{verification_id}",
    response_model=ReconciliationResponse,
    summary="Fetch evidence verification result by ID",
)
async def get_reconciliation_verification(
    verification_id: str,
    auth_context: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    service: ReconciliationService = Depends(get_reconciliation_service),
) -> ReconciliationResponse:
    """Retrieves a single reconciliation record and field comparisons."""
    try:
        return await service.get_verification(
            db=db,
            verification_id=verification_id,
            customer_id=auth_context.customer_id,
            roles=auth_context.roles,
        )
    except PlatformException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
