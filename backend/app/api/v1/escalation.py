"""Intelligent Escalation & After-Hours API Router.

Exposes endpoints for:
1. Evaluating conversation turns for escalation triggers (POST /api/v1/escalation/evaluate).
2. Querying operational business hours & next eligible business time (GET /api/v1/escalation/after-hours/status).
3. Querying immutable escalation audit trails (GET /api/v1/escalation/audits).
4. Triggering batch scan of 15m unresolved negative cases (POST /api/v1/escalation/scan-unresolved).
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ResolvedAuthContext,
    get_auth_context,
    get_correlation_id,
    get_db,
)
from app.core.clock import system_clock
from app.core.logging import configure_safe_logger
from app.models.customer import Conversation, Customer, Message
from app.models.ticket import EscalationAudit, Ticket
from app.modules.ticketing.after_hours import evaluate_after_hours
from app.modules.ticketing.escalation_service import EscalationService
from app.modules.ticketing.worker import EscalationWorker
from app.services.storage_service import validate_tenant_id

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/escalation", tags=["Intelligent Escalation & After-Hours"])

GLOBAL_ADMIN_ROLES = {"SUPER_ADMIN", "GLOBAL_ADMIN"}


# ==============================================================================
# SCHEMAS
# ==============================================================================


class EscalationEvaluateRequest(BaseModel):
    """Payload for evaluating conversation turns for escalation."""

    conversation_id: uuid.UUID = Field(
        ..., description="UUID of the conversation to evaluate"
    )
    message_id: Optional[uuid.UUID] = Field(
        default=None, description="Optional UUID of the triggering message"
    )
    message_content: Optional[str] = Field(
        default=None, description="Optional raw text content of recent customer message"
    )


class EscalationEvaluateResponse(BaseModel):
    """Result of an escalation evaluation."""

    should_escalate: bool
    trigger_condition: Optional[str] = None
    reason: Optional[str] = None
    is_after_hours: bool
    routing_destination: str
    next_eligible_business_time: Optional[datetime] = None
    audit_record_id: Optional[uuid.UUID] = None
    already_escalated: bool = False


class AfterHoursStatusResponse(BaseModel):
    """Current after-hours determination and next business opening time."""

    is_after_hours: bool
    reason: str
    calendar_timezone: str
    local_time: datetime
    next_eligible_business_time: datetime


class EscalationAuditItem(BaseModel):
    """Serialized escalation audit record."""

    id: uuid.UUID
    conversation_id: Optional[uuid.UUID]
    ticket_id: Optional[uuid.UUID]
    trigger_condition: str
    previous_state: str
    new_state: str
    escalation_reason: str
    escalation_payload: Dict[str, Any]
    triggered_at: datetime


class ScanUnresolvedResponse(BaseModel):
    """Result of a 15-minute unresolved negative cases batch scan."""

    scanned_at: datetime
    escalated_count: int
    audit_ids: List[uuid.UUID]


# ==============================================================================
# ENDPOINTS
# ==============================================================================


@router.post(
    "/evaluate",
    response_model=EscalationEvaluateResponse,
    summary="Evaluate conversation turn for escalation",
)
async def evaluate_conversation_escalation(
    payload: EscalationEvaluateRequest,
    db: AsyncSession = Depends(get_db),
    auth_ctx: ResolvedAuthContext = Depends(get_auth_context),
    correlation_id: str = Depends(get_correlation_id),
) -> EscalationEvaluateResponse:
    """Evaluates sentiment, repeated negative interactions (Rule A), and after-hours (Rules C & D)."""
    # Verify conversation access / tenant isolation
    stmt = select(Conversation).where(Conversation.id == payload.conversation_id)
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()

    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {payload.conversation_id} not found.",
        )

    # Customer isolation check if authenticated as customer session
    if auth_ctx.auth_type == "SESSION_TOKEN" and auth_ctx.customer_id:
        if conv.customer_id and str(conv.customer_id) != auth_ctx.customer_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: customer identity does not match conversation owner.",
            )

    # Load message if ID supplied
    message: Optional[Message] = None
    if payload.message_id:
        msg_stmt = select(Message).where(Message.id == payload.message_id)
        msg_res = await db.execute(msg_stmt)
        message = msg_res.scalar_one_or_none()
    elif payload.message_content:
        message = Message(
            conversation_id=conv.id,
            sender_type="CUSTOMER",
            content=payload.message_content,
        )
        db.add(message)
        await db.flush()

    service = EscalationService()
    decision = await service.evaluate_and_escalate_turn(
        session=db,
        conversation_id=conv.id,
        message=message,
    )
    await db.commit()

    return EscalationEvaluateResponse(
        should_escalate=decision.should_escalate,
        trigger_condition=decision.trigger_condition,
        reason=decision.reason,
        is_after_hours=decision.is_after_hours,
        routing_destination=decision.routing_destination,
        next_eligible_business_time=decision.next_eligible_business_time,
        audit_record_id=decision.audit_record_id,
        already_escalated=decision.already_escalated,
    )


@router.get(
    "/after-hours/status",
    response_model=AfterHoursStatusResponse,
    summary="Check business hours status and next eligible business opening",
)
async def get_after_hours_status(
    check_time: Optional[datetime] = Query(None, description="Optional ISO timestamp to evaluate"),
    db: AsyncSession = Depends(get_db),
    auth_ctx: ResolvedAuthContext = Depends(get_auth_context),
) -> AfterHoursStatusResponse:
    """Computes whether a given time (or now) is after hours, and the next eligible business time."""
    service = EscalationService()
    calendar = await service.get_or_create_default_calendar(db)
    dt = check_time or system_clock.now()

    ah_eval = evaluate_after_hours(dt, calendar)
    return AfterHoursStatusResponse(
        is_after_hours=ah_eval.is_after_hours,
        reason=ah_eval.reason,
        calendar_timezone=ah_eval.calendar_timezone,
        local_time=ah_eval.local_time,
        next_eligible_business_time=ah_eval.next_eligible_business_time,
    )


@router.get(
    "/audits",
    response_model=List[EscalationAuditItem],
    summary="List escalation audit trail records",
)
async def list_escalation_audits(
    conversation_id: Optional[uuid.UUID] = Query(None),
    ticket_id: Optional[uuid.UUID] = Query(None),
    trigger_condition: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    auth_ctx: ResolvedAuthContext = Depends(get_auth_context),
) -> List[EscalationAuditItem]:
    """Retrieves immutable escalation audit records."""
    conv_id = conversation_id if isinstance(conversation_id, uuid.UUID) else None
    tck_id = ticket_id if isinstance(ticket_id, uuid.UUID) else None
    trig = trigger_condition if isinstance(trigger_condition, str) else None
    limit_val = limit if isinstance(limit, int) else 50

    stmt = select(EscalationAudit)

    # Tenant and customer isolation
    caller_customer_id = auth_ctx.customer_id
    if auth_ctx.auth_type == "SESSION_TOKEN" or (auth_ctx.roles and all(r.upper() in ("CUSTOMER", "VIP_CUSTOMER") for r in auth_ctx.roles)):
        if caller_customer_id:
            cust_uuid = uuid.UUID(str(caller_customer_id))
            stmt = (
                stmt.outerjoin(Ticket, EscalationAudit.ticket_id == Ticket.id)
                .outerjoin(Conversation, EscalationAudit.conversation_id == Conversation.id)
                .where(or_(Ticket.customer_id == cust_uuid, Conversation.customer_id == cust_uuid))
            )
    else:
        is_global_admin = any(r.upper() in GLOBAL_ADMIN_ROLES for r in (auth_ctx.roles or []))
        if not is_global_admin:
            raw_tenant = (auth_ctx.jwt_claims or {}).get("tenant_id")
            if raw_tenant and isinstance(raw_tenant, str) and raw_tenant.strip():
                try:
                    staff_tenant = validate_tenant_id(raw_tenant)
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid tenant identifier.",
                    )
                stmt = (
                    stmt.outerjoin(Ticket, EscalationAudit.ticket_id == Ticket.id)
                    .outerjoin(Conversation, EscalationAudit.conversation_id == Conversation.id)
                    .outerjoin(Customer, or_(Ticket.customer_id == Customer.id, Conversation.customer_id == Customer.id))
                    .where(Customer.metadata_["tenant_id"].as_string() == staff_tenant)
                )

    if conv_id:
        stmt = stmt.where(EscalationAudit.conversation_id == conv_id)
    if tck_id:
        stmt = stmt.where(EscalationAudit.ticket_id == tck_id)
    if trig:
        stmt = stmt.where(EscalationAudit.trigger_condition == trig)

    stmt = stmt.order_by(EscalationAudit.triggered_at.desc()).limit(limit_val)
    res = await db.execute(stmt)
    records = res.scalars().all()

    return [
        EscalationAuditItem(
            id=r.id,
            conversation_id=r.conversation_id,
            ticket_id=r.ticket_id,
            trigger_condition=r.trigger_condition,
            previous_state=r.previous_state,
            new_state=r.new_state,
            escalation_reason=r.escalation_reason,
            escalation_payload=r.escalation_payload,
            triggered_at=r.triggered_at,
        )
        for r in records
    ]


@router.post(
    "/scan-unresolved",
    response_model=ScanUnresolvedResponse,
    summary="Scan and escalate unresolved negative cases (Rule B)",
)
async def scan_unresolved_negative_cases(
    batch_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    auth_ctx: ResolvedAuthContext = Depends(get_auth_context),
) -> ScanUnresolvedResponse:
    """Manually triggers Rule B scan: escalates negative cases unresolved for >= 15m."""
    limit_val = batch_size if isinstance(batch_size, int) else 50
    worker = EscalationWorker()
    audits = await worker.scan_and_escalate_batch(db, batch_size=limit_val)
    return ScanUnresolvedResponse(
        scanned_at=system_clock.now(),
        escalated_count=len(audits),
        audit_ids=[a.id for a in audits],
    )
