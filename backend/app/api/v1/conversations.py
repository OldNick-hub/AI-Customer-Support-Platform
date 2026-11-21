"""Intelligent Support Conversation & Message History API Router.

Endpoints:
  GET /api/v1/conversations                            — List conversations with customer isolation
  GET /api/v1/conversations/{conversation_id}          — Get single conversation details
  GET /api/v1/conversations/{conversation_id}/messages — List historical messages with ownership checks
"""

from datetime import datetime, timezone
import logging
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ResolvedAuthContext, get_auth_context, get_db
from app.core.logging import configure_safe_logger
from app.models.chat import ChatIdempotencyRecord, ChatOutboxEvent
from app.models.customer import Conversation, Customer, Message
from app.models.evidence import EvidenceFile
from app.models.ticket import Ticket
from app.modules.evidence import EvidenceListItem, EvidenceListResponse
from app.services.storage_service import validate_tenant_id

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/conversations", tags=["Conversational Dialog & History"])

APPROVED_STAFF_ROLES = {"ADMIN", "SUPERVISOR", "AGENT", "STAFF", "SUPPORT_AGENT", "SUPER_ADMIN", "GLOBAL_ADMIN"}
GLOBAL_ADMIN_ROLES = {"SUPER_ADMIN", "GLOBAL_ADMIN"}


# ==============================================================================
# Schemas
# ==============================================================================

class ConversationListItem(BaseModel):
    """Conversation summary item."""
    id: str
    customer_id: Optional[str] = None
    session_id: str
    primary_language: str
    status: str
    turn_count: int
    overall_sentiment_score: float
    is_escalated: bool
    started_at: datetime
    last_message_at: datetime
    closed_at: Optional[datetime] = None
    custom_title: Optional[str] = None


class UpdateConversationRequest(BaseModel):
    """Payload to update conversation properties (e.g., custom title or status)."""
    custom_title: Optional[str] = Field(None, max_length=255, description="Custom human-readable thread title")
    status: Optional[str] = Field(None, description="Updated status, e.g. OPEN, RESOLVED, CLOSED")


class ConversationListResponse(BaseModel):
    """Paginated conversation list response."""
    items: List[ConversationListItem]
    total_count: int
    limit: int
    offset: int


class MessageItem(BaseModel):
    """Individual dialog message item (PII-masked content)."""
    id: str
    conversation_id: str
    sender_type: str
    sender_id: Optional[str] = None
    content: str
    detected_language: str
    turn_sequence_number: Optional[int] = None
    created_at: datetime


class ConversationMessagesResponse(BaseModel):
    """Paginated conversation messages response."""
    conversation_id: str
    items: List[MessageItem]
    total_count: int
    limit: int
    offset: int


# ==============================================================================
# Helpers
# ==============================================================================

def _get_customer_id(auth: ResolvedAuthContext) -> Optional[str]:
    """Returns customer_id for SESSION_TOKEN callers; None for JWT staff."""
    if auth.auth_type == "SESSION_TOKEN":
        return auth.customer_id
    return None


def _require_staff(auth: ResolvedAuthContext) -> None:
    """Raises 403 if the caller is not an authenticated staff member with a recognized staff role."""
    if auth.auth_type != "JWT":
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires staff authentication.",
        )
    staff_roles = {r.upper() for r in (auth.roles or [])}
    if not staff_roles.intersection(APPROVED_STAFF_ROLES):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="A recognized staff role is required.",
        )


async def _get_authorized_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession,
    auth: ResolvedAuthContext,
) -> Conversation:
    """Retrieve a single conversation and strictly verify customer ownership or staff tenant boundary."""
    caller_customer_id = _get_customer_id(auth)

    conv_res = await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    conversation = conv_res.scalar_one_or_none()

    if conversation is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found.",
        )

    if caller_customer_id is not None:
        if conversation.customer_id is None or str(conversation.customer_id) != str(caller_customer_id):
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found.",
            )
    else:
        _require_staff(auth)
        is_global_admin = any(
            r.upper() in GLOBAL_ADMIN_ROLES
            for r in (auth.roles or [])
        )
        if not is_global_admin:
            raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
            if not raw_tenant or not isinstance(raw_tenant, str) or not raw_tenant.strip():
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Staff authentication token must contain a valid, non-empty tenant_id claim.",
                )
            try:
                staff_tenant = validate_tenant_id(raw_tenant)
            except Exception:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Invalid tenant identifier.",
                )
            if not conversation.customer_id:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=f"Conversation {conversation_id} not found.",
                )
            cust_res = await db.execute(select(Customer).where(Customer.id == conversation.customer_id))
            customer = cust_res.scalar_one_or_none()
            cust_tenant = (customer.metadata_ or {}).get("tenant_id") if customer else None
            if not cust_tenant or not isinstance(cust_tenant, str) or cust_tenant.strip() != staff_tenant:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=f"Conversation {conversation_id} not found.",
                )

    return conversation


# ==============================================================================
# Endpoints
# ==============================================================================

@router.get(
    "",
    response_model=ConversationListResponse,
    summary="List conversations with customer isolation and pagination",
)
async def list_conversations(
    status: Optional[str] = Query(None, description="Filter by status (e.g., OPEN, RESOLVED, ESCALATED)"),
    customer_id: Optional[uuid.UUID] = Query(None, description="Filter by customer UUID (staff only)"),
    limit: int = Query(50, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ConversationListResponse:
    """Retrieve paginated conversations with customer isolation.

    - Customer sessions (SESSION_TOKEN) can only view their own conversations.
    - Staff members (JWT) can view conversations within their tenant, or filter by customer_id.
    - Global administrators can view conversations across all tenants.
    """
    caller_customer_id = _get_customer_id(auth)

    base_query = select(Conversation)
    count_query = select(func.count()).select_from(Conversation)

    # Multi-tenancy / customer isolation enforcement
    if caller_customer_id is not None:
        cust_uuid = uuid.UUID(str(caller_customer_id))
        base_query = base_query.where(Conversation.customer_id == cust_uuid)
        count_query = count_query.where(Conversation.customer_id == cust_uuid)
    else:
        _require_staff(auth)
        is_global_admin = any(
            r.upper() in GLOBAL_ADMIN_ROLES
            for r in (auth.roles or [])
        )
        if not is_global_admin:
            raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
            if not raw_tenant or not isinstance(raw_tenant, str) or not raw_tenant.strip():
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Staff authentication token must contain a valid, non-empty tenant_id claim.",
                )
            try:
                staff_tenant = validate_tenant_id(raw_tenant)
            except Exception:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Invalid tenant identifier.",
                )
            base_query = base_query.join(Customer, Conversation.customer_id == Customer.id).where(
                Customer.metadata_["tenant_id"].as_string() == staff_tenant
            )
            count_query = count_query.join(Customer, Conversation.customer_id == Customer.id).where(
                Customer.metadata_["tenant_id"].as_string() == staff_tenant
            )

        safe_customer_id = customer_id if isinstance(customer_id, uuid.UUID) else None
        if safe_customer_id is not None:
            base_query = base_query.where(Conversation.customer_id == safe_customer_id)
            count_query = count_query.where(Conversation.customer_id == safe_customer_id)

    if status and isinstance(status, str):
        if status.upper().strip() != "ALL":
            base_query = base_query.where(Conversation.status == status.upper().strip())
            count_query = count_query.where(Conversation.status == status.upper().strip())
    else:
        # Default: exclude CLOSED and ARCHIVED threads so deleted threads disappear from the active sidebar
        base_query = base_query.where(Conversation.status.notin_(["CLOSED", "ARCHIVED"]))
        count_query = count_query.where(Conversation.status.notin_(["CLOSED", "ARCHIVED"]))

    # Deterministic ordering by recent message timestamp, secondary by ID
    safe_limit = limit if isinstance(limit, int) else 50
    safe_offset = offset if isinstance(offset, int) else 0
    base_query = base_query.order_by(
        Conversation.last_message_at.desc(), Conversation.id.asc()
    ).limit(safe_limit).offset(safe_offset)

    total_count_res = await db.execute(count_query)
    total_count = total_count_res.scalar_one()

    results = await db.execute(base_query)
    conversations = results.scalars().all()

    items = [
        ConversationListItem(
            id=str(c.id),
            customer_id=str(c.customer_id) if c.customer_id else None,
            session_id=str(c.session_id),
            primary_language=c.primary_language,
            status=c.status,
            turn_count=c.turn_count,
            overall_sentiment_score=c.overall_sentiment_score,
            is_escalated=c.is_escalated,
            started_at=c.started_at,
            last_message_at=c.last_message_at,
            closed_at=c.closed_at,
            custom_title=c.custom_title,
        )
        for c in conversations
    ]

    return ConversationListResponse(
        items=items,
        total_count=total_count,
        limit=safe_limit,
        offset=safe_offset,
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationListItem,
    summary="Get conversation details by ID",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ConversationListItem:
    """Retrieve details for a single conversation by ID with ownership checks."""
    conversation = await _get_authorized_conversation(conversation_id, db, auth)

    return ConversationListItem(
        id=str(conversation.id),
        customer_id=str(conversation.customer_id) if conversation.customer_id else None,
        session_id=str(conversation.session_id),
        primary_language=conversation.primary_language,
        status=conversation.status,
        turn_count=conversation.turn_count,
        overall_sentiment_score=conversation.overall_sentiment_score,
        is_escalated=conversation.is_escalated,
        started_at=conversation.started_at,
        last_message_at=conversation.last_message_at,
        closed_at=conversation.closed_at,
        custom_title=conversation.custom_title,
    )


@router.patch(
    "/{conversation_id}",
    response_model=ConversationListItem,
    summary="Update conversation title or status with ownership checks",
)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: UpdateConversationRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ConversationListItem:
    """Rename a conversation thread or update its lifecycle status."""
    conversation = await _get_authorized_conversation(conversation_id, db, auth)

    if payload.custom_title is not None:
        trimmed = payload.custom_title.strip()
        conversation.custom_title = trimmed if trimmed else None

    if payload.status is not None:
        new_status = payload.status.upper().strip()
        if new_status in ("OPEN", "RESOLVED", "ESCALATED", "ABANDONED", "CLOSED"):
            conversation.status = new_status
            if new_status in ("RESOLVED", "CLOSED"):
                conversation.closed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(conversation)

    return ConversationListItem(
        id=str(conversation.id),
        customer_id=str(conversation.customer_id) if conversation.customer_id else None,
        session_id=str(conversation.session_id),
        primary_language=conversation.primary_language,
        status=conversation.status,
        turn_count=conversation.turn_count,
        overall_sentiment_score=conversation.overall_sentiment_score,
        is_escalated=conversation.is_escalated,
        started_at=conversation.started_at,
        last_message_at=conversation.last_message_at,
        closed_at=conversation.closed_at,
        custom_title=conversation.custom_title,
    )


@router.delete(
    "/{conversation_id}",
    summary="Delete a conversation and its messages with ownership checks",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> dict:
    """Close and remove a conversation thread with customer ownership / tenant isolation."""
    conversation = await _get_authorized_conversation(conversation_id, db, auth)

    now_utc = datetime.now(timezone.utc)

    # 1. Synchronize and close all linked tickets to maintain cross-module data consistency
    tickets_res = await db.execute(select(Ticket).where(Ticket.conversation_id == conversation_id))
    for tck in tickets_res.scalars().all():
        tck.status = "CLOSED"
        tck.resolution_status = "CLOSED"
        tck.closed_at = now_utc
        tck.sla_breached = False

    # 2. Mark any associated evidence files as PURGED
    evidence_res = await db.execute(select(EvidenceFile).where(EvidenceFile.conversation_id == conversation_id))
    for ef in evidence_res.scalars().all():
        ef.status = "PURGED"
        ef.purged_at = now_utc

    # 3. Attempt hard-delete of chat events and conversation if permissions allow; otherwise soft-delete to CLOSED
    try:
        async with db.begin_nested():
            await db.execute(delete(ChatOutboxEvent).where(ChatOutboxEvent.conversation_id == conversation_id))
            await db.execute(delete(ChatIdempotencyRecord).where(ChatIdempotencyRecord.conversation_id == conversation_id))
            await db.delete(conversation)
    except Exception:
        # In compliance environments with append-only message audit triggers, mark CLOSED
        conversation.status = "CLOSED"
        conversation.closed_at = now_utc

    await db.commit()
    logger.info(f"Conversation {conversation_id} successfully closed/deleted by auth_type={auth.auth_type}.")
    return {"success": True, "message": f"Conversation {conversation_id} deleted successfully."}


@router.get(
    "/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
    summary="List messages for a conversation with ownership checks",
)
async def get_conversation_messages(
    conversation_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500, description="Message page limit"),
    offset: int = Query(0, ge=0, description="Message page offset"),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ConversationMessagesResponse:
    """Retrieve chronologically ordered messages for a conversation.

    Customer isolation:
    - Customer callers (SESSION_TOKEN) can only view messages of conversations they own.
    - Non-existent or unowned conversations return 404.
    """
    await _get_authorized_conversation(conversation_id, db, auth)

    msg_count_query = (
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == conversation_id)
    )
    total_res = await db.execute(msg_count_query)
    total_count = total_res.scalar_one()

    safe_limit = limit if isinstance(limit, int) else 100
    safe_offset = offset if isinstance(offset, int) else 0
    msg_query = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(
            Message.created_at.asc(),
            case(
                (Message.sender_type == "CUSTOMER", 1),
                (Message.sender_type == "AI_AGENT", 2),
                (Message.sender_type == "HUMAN_AGENT", 3),
                else_=4,
            ),
            Message.id.asc(),
        )
        .limit(safe_limit)
        .offset(safe_offset)
    )
    msgs_res = await db.execute(msg_query)
    messages = msgs_res.scalars().all()

    items = [
        MessageItem(
            id=str(m.id),
            conversation_id=str(m.conversation_id),
            sender_type=m.sender_type,
            sender_id=str(m.sender_id) if m.sender_id else None,
            content=m.content,
            detected_language=m.detected_language,
            turn_sequence_number=m.turn_sequence_number,
            created_at=m.created_at,
        )
        for m in messages
    ]

    return ConversationMessagesResponse(
        conversation_id=str(conversation_id),
        items=items,
        total_count=total_count,
        limit=safe_limit,
        offset=safe_offset,
    )


@router.get(
    "/{conversation_id}/evidence",
    response_model=EvidenceListResponse,
    summary="List active evidence files associated with a conversation",
)
async def list_conversation_evidence(
    conversation_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> EvidenceListResponse:
    """Retrieve active evidence files associated with a conversation.

    Access control:
    - Customer callers (SESSION_TOKEN): Only access evidence for conversations they own.
    - Staff callers (JWT): Verify conversation belongs to caller's tenant.
    - Return 404 for missing conversations or conversations outside caller's boundary.
    - Excludes PURGED files.
    - Does NOT expose internal file storage paths or encryption keys.
    """
    await _get_authorized_conversation(conversation_id, db, auth)

    safe_limit = limit if isinstance(limit, int) else 20
    safe_offset = offset if isinstance(offset, int) else 0

    base_query = (
        select(EvidenceFile)
        .where(EvidenceFile.conversation_id == conversation_id, EvidenceFile.status != "PURGED")
    )
    count_query = (
        select(func.count())
        .select_from(EvidenceFile)
        .where(EvidenceFile.conversation_id == conversation_id, EvidenceFile.status != "PURGED")
    )

    total_res = await db.execute(count_query)
    total_count = total_res.scalar_one()

    files_res = await db.execute(
        base_query.order_by(EvidenceFile.created_at.asc(), EvidenceFile.id.asc())
        .limit(safe_limit)
        .offset(safe_offset)
    )
    evidence_files = files_res.scalars().all()

    items = []
    for ef in evidence_files:
        ocr_text = None
        if ef.extracted_entities:
            ocr_text = ef.extracted_entities[0].pii_redacted_text

        file_ext = ef.file_name.rsplit(".", 1)[-1].upper() if "." in ef.file_name else "UNKNOWN"
        is_quar = bool(ef.is_malicious or ef.status in ("FLAGGED_UNSAFE", "QUARANTINED"))

        items.append(
            EvidenceListItem(
                id=str(ef.id),
                evidence_id=str(ef.id),
                conversation_id=str(ef.conversation_id) if ef.conversation_id else None,
                customer_id=str(ef.customer_id) if ef.customer_id else None,
                message_id=str(ef.message_id) if ef.message_id else None,
                ticket_id=str(ef.ticket_id) if ef.ticket_id else None,
                file_name=ef.file_name,
                file_type=file_ext,
                file_size_bytes=ef.file_size_bytes,
                mime_type=ef.mime_type,
                classification="EVIDENCE",
                verification_status=ef.status,
                status=ef.status,
                uploaded_at=ef.created_at,
                created_at=ef.created_at,
                processed_at=ef.processed_at,
                is_quarantined=is_quar,
                is_malicious=ef.is_malicious,
                quarantine_reason=ef.quarantine_reason,
                processing_error=ef.processing_error,
                ocr_extracted_text=ocr_text,
                sha256_hash=ef.sha256_hash,
                retention_expires_at=ef.retention_expires_at,
                metadata={
                    "retention_expires_at": ef.retention_expires_at.isoformat() if ef.retention_expires_at else None,
                    "sha256_hash": ef.sha256_hash,
                },
            )
        )

    return EvidenceListResponse(
        items=items,
        total_count=total_count,
        limit=safe_limit,
        offset=safe_offset,
    )


