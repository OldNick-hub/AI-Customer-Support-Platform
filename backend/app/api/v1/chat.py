"""Conversational Chat & Advanced RAG API Router.

Implements canonical endpoint:
POST /api/v1/chat/message

Strict Scope Boundary (Task 4 Phase 4D):
This endpoint exposes the Advanced RAG Knowledge Assistant engine only.
Downstream Modules 5 (Sentiment) and 6 (Multilingual) are not yet integrated.
"""

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ResolvedAuthContext,
    get_auth_context,
    get_correlation_id,
    get_db,
    get_multilingual_orchestrator,
    get_rag_orchestrator,
)
from app.core.logging import configure_safe_logger
from app.core.rate_limiter import check_chat_rate_limit
from app.models.chat import ChatIdempotencyRecord
from app.models.customer import Conversation, Customer, Session as CustomerSession
from app.modules.multilingual.orchestrator import MultilingualOrchestrator
from app.modules.rag.generator import RAGResolutionResult
from app.modules.rag.orchestrator import RAGInternalRequest, RAGOrchestrator
from app.services.storage_service import validate_tenant_id

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/chat", tags=["Conversational Chat & RAG"])


# ==============================================================================
# REQUEST & RESPONSE SCHEMAS
# ==============================================================================

class ChatMessageRequest(BaseModel):
    """Canonical request payload for POST /api/v1/chat/message.

    Enforces zero-trust: strictly forbids client-supplied role, access_tier,
    customer_id, or security_context.
    """

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
        "populate_by_name": True,
    }

    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        alias="query",
        description="Customer or staff natural language inquiry",
        examples=["What is the return window for electronics in the US?"],
    )
    conversation_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional conversation UUID for multi-turn dialogue correlation",
    )
    effective_as_of_date: Optional[date] = Field(
        default=None,
        description="Historical policy evaluation date (YYYY-MM-DD)",
        examples=["2026-03-01"],
    )
    product_line: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional product line filter (e.g. 'ELECTRONICS')",
    )
    document_code: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional target policy code to pin retrieval (e.g. 'POL-RET-US-2026')",
    )
    preferred_language: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Optional client language preference (en, es, fr, de, hi)",
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional client idempotency key for duplicate prevention",
    )

    @field_validator("message")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Query message cannot be empty or whitespace.")
        return v.strip()

    @field_validator("preferred_language")
    @classmethod
    def validate_preferred_language(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v_norm = v.strip().lower()
            if v_norm not in ("en", "es", "fr", "de", "hi"):
                raise ValueError(
                    f"Invalid preferred_language '{v}'. Supported languages: en, es, fr, de, hi."
                )
            return v_norm
        return v


class CitationDTO(BaseModel):
    """Authoritative inline citation metadata."""

    citation_id: int
    document_code: str
    document_title: str
    version_number: int
    effective_date: str
    chunk_excerpt: str


class ClaimVerificationDTO(BaseModel):
    """Claim-to-passage verification metrics."""

    total_claims: int
    verified_claims: int
    all_claims_verified: bool


class ChatMessageData(BaseModel):
    """Structured data payload for grounded chat answers."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    reply_text: str = Field(..., description="Grounded answer text or explanatory refusal")
    is_resolved: bool = Field(..., description="True if definitively answered from knowledge base")
    refusal_triggered: bool = Field(..., description="True if query triggered policy refusal/abstention")
    refusal_reason: Optional[str] = Field(None, description="Standardized refusal classification reason code")
    citations: List[CitationDTO] = Field(default_factory=list, description="Verified inline citations")
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    effective_as_of_date_applied: str
    suggested_ticket_category: Optional[str] = None
    claim_verification: ClaimVerificationDTO
    model_used: str
    retrieval_latency_ms: float
    generation_latency_ms: float
    total_latency_ms: float
    detected_language: str = Field(default="en", description="Inbound detected language code")
    detected_language_confidence: float = Field(default=1.0, description="Language detection confidence score")
    primary_language: str = Field(default="en", description="Effective dialogue language")
    context_restored: bool = Field(default=False, description="True if idle session was restored")
    clarification_needed: bool = Field(default=False, description="True if language clarification is needed")
    technical_error: bool = Field(default=False, description="True if technical fallback notice is returned")
    technical_error_code: Optional[str] = Field(default=None, description="Standardized error code if technical error")
    turn_sequence_number: Optional[int] = Field(default=None, description="Atomic monotonic turn sequence number")
    conversation_id: Optional[str] = Field(default=None, description="Active conversation thread UUID")
    context_window_complete: bool = Field(default=True, description="True if timeline history is contiguous")
    is_escalated: bool = Field(default=False, description="True if conversation has been escalated")
    escalation_reason: Optional[str] = Field(default=None, description="Escalation trigger condition or reason if escalated")


class ResponseMeta(BaseModel):
    """Standardized response metadata envelope."""

    timestamp: str
    request_id: str
    caller_auth_type: str


class ChatMessageResponse(BaseModel):
    """Unified success envelope per API_SPECIFICATION.md Section 1.2."""

    success: bool = True
    data: ChatMessageData
    meta: ResponseMeta


# ==============================================================================
# ROUTE HANDLER
# ==============================================================================

@router.post(
    "/message",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Send Conversational Chat Turn & Query Knowledge Assistant",
    description=(
        "Authenticates caller via X-Session-Token or Authorization: Bearer JWT. "
        "Coordinates input sanitization, 3-layer security retrieval, temporal policy resolution, "
        "and evidence-grounded answer generation with mandatory inline citations."
    ),
)
async def post_chat_message(
    payload: ChatMessageRequest,
    auth: ResolvedAuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    correlation_id: str = Depends(get_correlation_id),
    orchestrator: RAGOrchestrator = Depends(get_rag_orchestrator),
    multilingual_orchestrator: MultilingualOrchestrator = Depends(get_multilingual_orchestrator),
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    _rate_limit: None = Depends(check_chat_rate_limit),
) -> ChatMessageResponse:
    """Executes verified RAG query lifecycle through MultilingualOrchestrator and RAGOrchestrator."""
    logger.info(
        "Received chat turn query on correlation %s (auth_type: %s)",
        correlation_id,
        auth.auth_type,
    )

    effective_idempotency_key = idempotency_key_header or payload.idempotency_key

    # =========================================================================
    # Phase 1: Authoritative Session & Customer Resolution Strategy
    # =========================================================================
    if auth.auth_type == "JWT":
        # Authenticated Staff Caller (AGENT, SUPERVISOR, ADMIN, etc.)
        raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
        if not raw_tenant or not isinstance(raw_tenant, str) or not raw_tenant.strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Staff authentication token must contain a valid, non-empty tenant_id claim.",
            )
        try:
            staff_tenant = validate_tenant_id(raw_tenant)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid tenant identifier.",
            )

        is_global_admin = any(
            r.upper() in {"SUPER_ADMIN", "GLOBAL_ADMIN"}
            for r in (auth.roles or [])
        )

        if payload.conversation_id:
            # Multi-turn continuation on an existing conversation thread
            conv_stmt = select(Conversation).where(Conversation.id == payload.conversation_id)
            conv_res = await db.execute(conv_stmt)
            existing_conv = conv_res.scalar_one_or_none()
            if not existing_conv:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conversation not found.",
                )

            # Strict tenant authorization check for staff caller
            if not is_global_admin:
                if not existing_conv.customer_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Conversation not found.",
                    )
                cust_res = await db.execute(select(Customer).where(Customer.id == existing_conv.customer_id))
                cust = cust_res.scalar_one_or_none()
                cust_tenant = (cust.metadata_ or {}).get("tenant_id") if cust else None
                if not cust_tenant or cust_tenant != staff_tenant:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Conversation not found.",
                    )

            session_uuid = existing_conv.session_id
            customer_uuid = existing_conv.customer_id
        else:
            # Initiating a new conversation thread: resolve/create dedicated internal staff test customer & session
            staff_user_id = str(auth.user_id_or_email)
            staff_external_id = f"STAFF-USR-{staff_user_id}-{staff_tenant}"
            cust_stmt = select(Customer).where(Customer.external_id == staff_external_id)
            cust_res = await db.execute(cust_stmt)
            staff_cust = cust_res.scalar_one_or_none()

            if not staff_cust:
                staff_cust = Customer(
                    external_id=staff_external_id,
                    email=(auth.jwt_claims or {}).get("email") or f"{staff_user_id}@{staff_tenant}.internal",
                    full_name=(auth.jwt_claims or {}).get("name") or "Staff Internal Agent",
                    customer_tier="ENTERPRISE",
                    metadata_={
                        "is_staff_internal": True,
                        "staff_user_id": staff_user_id,
                        "tenant_id": staff_tenant,
                    },
                )
                db.add(staff_cust)
                await db.flush()

            now = datetime.now(timezone.utc)
            staff_sess = None

            if effective_idempotency_key:
                idem_stmt = select(ChatIdempotencyRecord).where(
                    ChatIdempotencyRecord.customer_id == staff_cust.id,
                    ChatIdempotencyRecord.idempotency_key == effective_idempotency_key,
                )
                existing_idem = (await db.execute(idem_stmt)).scalar_one_or_none()
                if existing_idem and existing_idem.session_id:
                    sess_res = await db.execute(
                        select(CustomerSession).where(CustomerSession.id == existing_idem.session_id)
                    )
                    staff_sess = sess_res.scalar_one_or_none()

            if not staff_sess:
                active_sess_stmt = (
                    select(CustomerSession)
                    .where(
                        CustomerSession.customer_id == staff_cust.id,
                        CustomerSession.status == "ACTIVE",
                        CustomerSession.expires_at > now,
                    )
                    .order_by(CustomerSession.last_activity_at.desc())
                )
                staff_sess = (await db.execute(active_sess_stmt)).scalars().first()

            if not staff_sess:
                staff_token = f"staff-sess-{staff_user_id}-{staff_tenant}-{uuid.uuid4().hex[:8]}"
                staff_sess = CustomerSession(
                    customer_id=staff_cust.id,
                    session_token=staff_token,
                    current_language=payload.preferred_language or "en",
                    status="ACTIVE",
                    last_activity_at=now,
                    expires_at=now + timedelta(days=7),
                    context_summary=f"Staff session for {staff_user_id} ({staff_tenant})",
                )
                db.add(staff_sess)
                await db.flush()

            session_uuid = staff_sess.id
            customer_uuid = staff_cust.id

    elif auth.auth_type == "SESSION_TOKEN":
        # Customer session token authentication (X-Session-Token)
        try:
            customer_uuid = uuid.UUID(str(auth.customer_id)) if auth.customer_id else None
            session_uuid = uuid.UUID(str(auth.session_id)) if auth.session_id else None
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid customer session credentials.",
            )
        if not customer_uuid or not session_uuid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Customer session authentication missing valid customer or session reference.",
            )

        if payload.conversation_id:
            conv_stmt = select(Conversation).where(Conversation.id == payload.conversation_id)
            conv_res = await db.execute(conv_stmt)
            existing_conv = conv_res.scalar_one_or_none()
            if not existing_conv or existing_conv.customer_id != customer_uuid:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conversation not found.",
                )
            session_uuid = existing_conv.session_id
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported authentication credential type.",
        )

    # Propagate any dependency-overridden RAG orchestrator
    multilingual_orchestrator.rag_orchestrator = orchestrator

    try:
        res = await multilingual_orchestrator.process_chat_turn(
            session=db,
            customer_id=customer_uuid,
            session_id=session_uuid,
            message=payload.message,
            conversation_id=payload.conversation_id,
            preferred_language=payload.preferred_language,
            idempotency_key=effective_idempotency_key,
            effective_as_of_date=payload.effective_as_of_date,
            product_line=payload.product_line,
            document_code=payload.document_code,
            auth_context=auth,
            correlation_id=correlation_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Unhandled exception in chat endpoint on correlation %s: %s",
            correlation_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request. Please try again later.",
        )

    # Enforce HTTP 403 Forbidden on authenticated unauthorized access attempts
    if res.get("refusal_triggered") and res.get("refusal_reason") in (
        "AUTHORIZATION_DENIED",
        "UNAUTHORIZED_ACCESS",
    ):
        logger.warning(
            "Access denied for correlation %s (refusal_reason: %s)",
            correlation_id,
            res.get("refusal_reason"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to requested knowledge resources is denied.",
        )

    # Map citations
    citations_dto: List[CitationDTO] = [
        CitationDTO(
            citation_id=c.get("citation_id", i + 1) if isinstance(c, dict) else getattr(c, "citation_number", i + 1),
            document_code=c.get("document_code", "DOC") if isinstance(c, dict) else getattr(c, "document_code", "DOC"),
            document_title=c.get("document_title", "Document") if isinstance(c, dict) else getattr(c, "document_title", "Document"),
            version_number=c.get("version_number", 1) if isinstance(c, dict) else getattr(c, "version_number", 1),
            effective_date=str(c.get("effective_date", "")) if isinstance(c, dict) else str(getattr(c, "effective_from", "")),
            chunk_excerpt=c.get("chunk_excerpt", "") if isinstance(c, dict) else getattr(c, "excerpt", ""),
        )
        for i, c in enumerate(res.get("citations", []))
    ]

    # Map claim verification
    raw_claims = res.get("claim_verification", {})
    claim_dto = ClaimVerificationDTO(
        total_claims=raw_claims.get("total_claims", 0) if isinstance(raw_claims, dict) else getattr(raw_claims, "total_claims", 0),
        verified_claims=raw_claims.get("verified_claims", 0) if isinstance(raw_claims, dict) else getattr(raw_claims, "verified_claims", 0),
        all_claims_verified=raw_claims.get("all_claims_verified", True) if isinstance(raw_claims, dict) else getattr(raw_claims, "all_claims_verified", True),
    )

    data = ChatMessageData(
        message_id=res.get("message_id", str(uuid.uuid4())),
        reply_text=res["reply_text"],
        is_resolved=res.get("is_resolved", True),
        refusal_triggered=res.get("refusal_triggered", False),
        refusal_reason=(
            "SECURITY_INJECTION_DETECTED"
            if res.get("refusal_reason") == "PROMPT_INJECTION_DETECTED"
            else res.get("refusal_reason")
        ),
        citations=citations_dto,
        confidence_score=res.get("confidence_score", 1.0),
        effective_as_of_date_applied=res.get("effective_as_of_date_applied", str(date.today())),
        suggested_ticket_category=res.get("suggested_ticket_category"),
        claim_verification=claim_dto,
        model_used=res.get("model_used", "gemini-3.8-flash"),
        retrieval_latency_ms=res.get("retrieval_latency_ms", 0.0),
        generation_latency_ms=res.get("generation_latency_ms", 0.0),
        total_latency_ms=res.get("total_latency_ms", 0.0),
        detected_language=res.get("detected_language", "en"),
        detected_language_confidence=res.get("detected_language_confidence", 1.0),
        primary_language=res.get("primary_language", "en"),
        context_restored=res.get("context_restored", False),
        clarification_needed=res.get("clarification_needed", False),
        technical_error=res.get("technical_error", False),
        technical_error_code=res.get("technical_error_code"),
        turn_sequence_number=res.get("turn_sequence_number"),
        conversation_id=res.get("conversation_id"),
        context_window_complete=res.get("context_window_complete", True),
        is_escalated=res.get("is_escalated", False),
        escalation_reason=res.get("escalation_reason"),
    )

    meta = ResponseMeta(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request_id=correlation_id,
        caller_auth_type=auth.auth_type,
    )

    return ChatMessageResponse(success=True, data=data, meta=meta)
