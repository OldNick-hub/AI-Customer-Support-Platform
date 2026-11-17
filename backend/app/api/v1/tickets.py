"""Intelligent Ticket Management API Router.

Endpoints:
  POST   /api/v1/tickets/synthesize                             — Synthesize ticket from conversation
  GET    /api/v1/tickets/{ticket_id}/synthesis                  — Get synthesis result
  POST   /api/v1/tickets/{ticket_id}/synthesis/completeness     — Re-evaluate completeness
  POST   /api/v1/tickets/{ticket_id}/synthesis/clarify          — Generate clarification questions
  POST   /api/v1/tickets/{ticket_id}/synthesis/respond          — Submit customer clarification response
  GET    /api/v1/tickets/{ticket_id}/synthesis/clarifications   — List clarification rounds
  POST   /api/v1/tickets/{ticket_id}/synthesis/complete         — Mark ticket synthesis complete (agent/admin)
"""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ResolvedAuthContext, get_auth_context, get_correlation_id, get_db
from app.core.logging import configure_safe_logger
from app.models.customer import Customer
from app.models.evidence import EvidenceFile
from app.models.ticket import Ticket
from app.models.ticket_synthesis import TicketClarificationRequest, TicketSynthesis
from app.services.storage_service import validate_tenant_id
from app.modules.evidence import EvidenceListItem, EvidenceListResponse
from app.modules.ticketing import (
    ActionConcurrencyConflictError,
    ActionMaxAttemptsExceededError,
    ActionNotApprovedError,
    ActionNotFoundError,
    ActionOrchestrationError,
    AgentCapacityExceededError,
    AgentNotFoundError,
    AgentRoleNotAuthorizedError,
    ResolutionAuthorizationError,
    ResolutionError,
    ResolutionNotFoundError,
    ResolutionVerificationError,
    TicketAuthorizationError,
    TicketNotFoundError,
    TicketRoutingAuthorizationError,
    TicketSynthesisConflictError,
    InvalidSynthesisTransitionError,
    get_ticket_synthesis_service,
    get_ticket_routing_service,
    get_ticket_resolution_service,
)

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/tickets", tags=["Intelligent Ticket Management"])


# ==============================================================================
# Request / Response Schemas
# ==============================================================================

class SynthesizeTicketRequest(BaseModel):
    """Request to synthesize a ticket from its linked conversation."""
    ticket_id: uuid.UUID = Field(..., description="UUID of the ticket to synthesize.")
    force_resynthesis: bool = Field(
        default=False,
        description="If True, re-synthesize even if synthesis already exists. "
                    "Supersedes pending clarification rounds.",
    )


class SynthesizeTicketResponse(BaseModel):
    """Response from ticket synthesis."""
    ticket_id: str
    synthesis_status: str
    synthesis: Dict[str, Any]
    completeness: Dict[str, Any]


class SynthesisDetailResponse(BaseModel):
    """Detailed view of the current synthesis for a ticket."""
    ticket_id: str
    synthesis_status: str
    synthesis_id: Optional[str]
    problem_statement: Optional[str]
    product_or_service: Optional[str]
    reference_identifiers: List[Dict[str, str]]
    issue_category: Optional[str]
    issue_subcategory: Optional[str]
    resolution_sought: Optional[str]
    actions_already_attempted: List[str]
    missing_fields: List[str]
    contradictions: List[Dict[str, str]]
    completeness_score: float
    extraction_confidence: float
    extraction_provenance: Dict[str, str]
    llm_model_used: Optional[str]
    synthesis_version: int


class CompletenessResponse(BaseModel):
    """Completeness evaluation result."""
    ticket_id: str
    issue_category: str
    completeness_score: float
    is_complete: bool
    mandatory_fields_total: int
    mandatory_fields_present: int
    missing_mandatory_fields: List[str]
    missing_optional_fields: List[str]
    contradictions_detected: List[str]
    reasons: List[str]
    already_provided_fields: List[str]


class ClarifyRequest(BaseModel):
    """Request to generate a clarification round."""
    ticket_id: uuid.UUID


class ClarifyResponse(BaseModel):
    """Response from clarification generation."""
    ticket_id: str
    should_ask: bool
    round_number: Optional[int] = None
    questions: List[Dict[str, Any]] = Field(default_factory=list)
    targeted_missing_fields: List[str] = Field(default_factory=list)
    synthesis_status: str
    stop_reason: Optional[str] = None


class ClarificationRespondRequest(BaseModel):
    """Customer response to a clarification round."""
    clarification_request_id: uuid.UUID = Field(
        ..., description="UUID of the TicketClarificationRequest being answered."
    )
    customer_response: str = Field(..., min_length=1, max_length=5000)


class ClarificationRespondResponse(BaseModel):
    """Result of processing a customer's clarification response."""
    ticket_id: str
    clarification_request_id: str
    round_number: int
    response_absorbed: bool
    ambiguous_response: bool
    new_completeness: Dict[str, Any]
    should_continue_clarification: bool
    synthesis_status: str


class ClarificationListItem(BaseModel):
    """Summary of a single clarification round."""
    id: str
    round_number: int
    status: str
    questions: List[Dict[str, Any]]
    targeted_missing_fields: List[str]
    completeness_score_before: float
    completeness_score_after: Optional[float]
    created_at: str
    responded_at: Optional[str]


class ClarificationListResponse(BaseModel):
    """All clarification rounds for a ticket."""
    ticket_id: str
    rounds: List[ClarificationListItem]


class PriorityEvaluateResponse(BaseModel):
    """Response from evaluating ticket priority."""
    ticket_id: str
    priority: str
    priority_score: float
    priority_floor: str
    reason: str
    supporting_facts: List[str]


class RouteTicketResponse(BaseModel):
    """Response from intelligent ticket routing."""
    ticket_id: str
    target_department: str
    assigned_agent_id: Optional[str]
    assigned_agent_name: Optional[str]
    routing_status: str
    reason: str
    candidates_evaluated: int
    candidate_details: List[Dict[str, Any]]


class AssignTicketRequest(BaseModel):
    """Request for manual assignment override."""
    agent_id: uuid.UUID
    reason: str = Field(..., min_length=5, description="Auditable justification for the assignment")
    force_override: bool = Field(default=False, description="Whether to override agent capacity limit (supervisors/admins only)")


class AssignTicketResponse(BaseModel):
    """Response from manual assignment override."""
    ticket_id: str
    assigned_agent_id: str
    assigned_agent_name: str
    assigned_team: str
    routing_status: str
    active_tickets: int
    max_active_tickets: int
    reason: str
    capacity_overridden: bool = False


class RoutingHistoryItem(BaseModel):
    """A single routing or priority event in the audit trail."""
    event_id: str
    event_type: str
    event_payload: Dict[str, Any]
    created_at: Optional[str]


class RoutingHistoryResponse(BaseModel):
    """Chronological routing and priority audit history for a ticket."""
    ticket_id: str
    events: List[RoutingHistoryItem]


class TeamWorkloadItem(BaseModel):
    """Active capacity and workload breakdown for a support department."""
    department: str
    active_agents: int
    total_capacity: int
    active_tickets: int
    utilization_pct: float


class TeamWorkloadResponse(BaseModel):
    """Department-level workload overview for supervisors and administrators."""
    teams: List[TeamWorkloadItem]


# ==============================================================================
# Resolution & Action Orchestration Schemas (Day 14)
# ==============================================================================

class ResolutionActionItem(BaseModel):
    action_id: str
    action_type: str
    action_key: str
    action_params: Dict[str, Any]
    status: str
    is_sensitive: bool
    requires_approval: bool
    approved_by: Optional[str] = None
    execution_attempts: int = 0
    execution_result: Dict[str, Any] = Field(default_factory=dict)


class EvaluateResolutionResponse(BaseModel):
    ticket_id: str
    resolution_id: str
    status: str
    playbook_category: str
    recommended_resolution: str
    confidence_score: float
    eligibility_decision: str
    requires_human_approval: bool
    supporting_evidence: List[str]
    rejection_or_escalation_reason: Optional[str] = None
    actions: List[ResolutionActionItem] = Field(default_factory=list)


class ProposeActionRequest(BaseModel):
    action_type: str = Field(..., min_length=2)
    action_params: Dict[str, Any] = Field(default_factory=dict)
    is_sensitive: bool = False
    requires_approval: bool = False


class ApproveActionRequest(BaseModel):
    reason: str = Field(..., min_length=3, description="Mandatory reason for action approval.")


class RejectActionRequest(BaseModel):
    reason: str = Field(..., min_length=3, description="Mandatory reason for action rejection.")


class UpdateActionParamsRequest(BaseModel):
    action_params: Dict[str, Any] = Field(..., description="Updated structured parameters for the action.")


class ExecuteActionResponse(BaseModel):
    action_id: str
    status: str
    execution_attempts: int
    result: Dict[str, Any]


class VerifyResolutionResponse(BaseModel):
    verified: bool
    resolution_status: str
    ticket_status: str
    failures: Optional[List[str]] = None
    escalated: Optional[bool] = None
    verified_at: Optional[str] = None
    actions_executed: Optional[int] = None


class ResolutionHistoryResponse(BaseModel):
    ticket_id: str
    events: List[RoutingHistoryItem]


class TicketListItem(BaseModel):
    id: str
    ticket_number: str
    conversation_id: Optional[str] = None
    customer_id: str
    assigned_agent_id: Optional[str] = None
    assigned_team: Optional[str] = None
    title: str
    description: str
    masked_summary: str
    priority: str
    priority_score: float
    status: str
    category: str
    synthesis_status: str
    routing_status: str
    resolution_status: str
    sla_target_minutes: int
    sla_warning_at: Optional[datetime] = None
    sla_breach_at: datetime
    sla_breached: bool
    created_at: datetime
    updated_at: datetime


class TicketListResponse(BaseModel):
    items: List[TicketListItem]
    total_count: int
    limit: int
    offset: int


class TicketDetailResponse(BaseModel):
    id: str
    ticket_number: str
    conversation_id: Optional[str] = None
    customer_id: str
    assigned_agent_id: Optional[str] = None
    assigned_team: Optional[str] = None
    title: str
    description: str
    masked_summary: str
    priority: str
    priority_score: float
    priority_explanation: Optional[str] = None
    status: str
    category: str
    cluster_group_id: Optional[str] = None
    sla_calendar_id: str
    sla_target_minutes: int
    sla_warning_at: datetime
    sla_breach_at: datetime
    sla_warning_sent: bool
    sla_breached: bool
    sla_paused_at: Optional[datetime] = None
    total_paused_duration_seconds: int = 0
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    synthesis_status: str
    routing_status: str
    resolution_status: str
    created_at: datetime
    updated_at: datetime


# ==============================================================================
# Auth helpers
# ==============================================================================

APPROVED_STAFF_ROLES = {"ADMIN", "SUPERVISOR", "AGENT", "STAFF", "SUPPORT_AGENT", "SUPER_ADMIN", "GLOBAL_ADMIN"}
GLOBAL_ADMIN_ROLES = {"SUPER_ADMIN", "GLOBAL_ADMIN"}


def _get_customer_id(auth: ResolvedAuthContext) -> Optional[str]:
    """Returns customer_id for SESSION_TOKEN callers; None for JWT staff."""
    if auth.auth_type == "SESSION_TOKEN":
        return auth.customer_id
    return None  # Staff bypass


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


# ==============================================================================
# Endpoints
# ==============================================================================

@router.get(
    "",
    response_model=TicketListResponse,
    summary="List support tickets with pagination, status, and priority filters",
)
async def list_tickets(
    status: Optional[str] = Query(None, description="Filter by ticket status (e.g., OPEN, RESOLVED)"),
    priority: Optional[str] = Query(None, description="Filter by priority (e.g., LOW, MEDIUM, HIGH, CRITICAL, URGENT)"),
    category: Optional[str] = Query(None, description="Filter by category"),
    customer_id: Optional[uuid.UUID] = Query(None, description="Filter by customer ID (staff only)"),
    exclude_closed: bool = Query(False, description="Exclude closed tickets from list"),
    limit: int = Query(50, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> TicketListResponse:
    """Retrieve paginated tickets with optional status, priority, and category filters.

    Customer isolation:
    - Customer callers (SESSION_TOKEN) can only list tickets they own.
    - Staff callers (JWT) can view tickets across their tenant, or filter by customer_id.
    - Global administrators can view tickets across all tenants.
    """
    caller_customer_id = _get_customer_id(auth)

    base_query = select(Ticket)
    count_query = select(func.count()).select_from(Ticket)

    # Multi-tenancy / customer isolation enforcement
    if caller_customer_id is not None:
        cust_uuid = uuid.UUID(str(caller_customer_id))
        base_query = base_query.where(Ticket.customer_id == cust_uuid)
        count_query = count_query.where(Ticket.customer_id == cust_uuid)
    else:
        # Staff caller multi-tenant isolation
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
            base_query = base_query.join(Customer, Ticket.customer_id == Customer.id).where(
                Customer.metadata_["tenant_id"].as_string() == staff_tenant
            )
            count_query = count_query.join(Customer, Ticket.customer_id == Customer.id).where(
                Customer.metadata_["tenant_id"].as_string() == staff_tenant
            )

        safe_customer_id = customer_id if isinstance(customer_id, uuid.UUID) else None
        if safe_customer_id is not None:
            base_query = base_query.where(Ticket.customer_id == safe_customer_id)
            count_query = count_query.where(Ticket.customer_id == safe_customer_id)

    if status and isinstance(status, str):
        base_query = base_query.where(Ticket.status == status.upper().strip())
        count_query = count_query.where(Ticket.status == status.upper().strip())
    elif exclude_closed:
        base_query = base_query.where(Ticket.status != "CLOSED")
        count_query = count_query.where(Ticket.status != "CLOSED")

    if priority and isinstance(priority, str):
        base_query = base_query.where(Ticket.priority == priority.upper().strip())
        count_query = count_query.where(Ticket.priority == priority.upper().strip())

    if category and isinstance(category, str):
        base_query = base_query.where(Ticket.category == category.strip())
        count_query = count_query.where(Ticket.category == category.strip())

    # Deterministic ordering
    safe_limit = limit if isinstance(limit, int) else 50
    safe_offset = offset if isinstance(offset, int) else 0
    base_query = base_query.order_by(Ticket.created_at.desc(), Ticket.id.asc()).limit(safe_limit).offset(safe_offset)

    total_count_res = await db.execute(count_query)
    total_count = total_count_res.scalar_one()

    results = await db.execute(base_query)
    tickets = results.scalars().all()

    items = [
        TicketListItem(
            id=str(t.id),
            ticket_number=t.ticket_number,
            conversation_id=str(t.conversation_id) if t.conversation_id else None,
            customer_id=str(t.customer_id),
            assigned_agent_id=str(t.assigned_agent_id) if t.assigned_agent_id else None,
            assigned_team=t.assigned_team,
            title=t.title,
            description=t.description,
            masked_summary=t.masked_summary,
            priority=t.priority,
            priority_score=t.priority_score,
            status=t.status,
            category=t.category,
            synthesis_status=t.synthesis_status,
            routing_status=t.routing_status,
            resolution_status=t.resolution_status,
            sla_target_minutes=t.sla_target_minutes,
            sla_warning_at=t.sla_warning_at,
            sla_breach_at=t.sla_breach_at,
            sla_breached=t.sla_breached,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tickets
    ]

    return TicketListResponse(
        items=items,
        total_count=total_count,
        limit=safe_limit,
        offset=safe_offset,
    )


@router.get(
    "/{ticket_id}",
    response_model=TicketDetailResponse,
    summary="Get detailed support ticket by ID with authorized ownership checks",
)
async def get_ticket_detail(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> TicketDetailResponse:
    """Retrieve full details of a single ticket by UUID.

    Customer isolation:
    - Customer callers (SESSION_TOKEN) can only view tickets they own.
    - Attempting to access another customer's ticket returns 404 to prevent enumeration.
    """
    caller_customer_id = _get_customer_id(auth)

    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()

    if ticket is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found.",
        )

    # Customer isolation check: customers can only view their own tickets
    if caller_customer_id is not None:
        if str(ticket.customer_id) != str(caller_customer_id):
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found.",
            )
    else:
        # Staff caller multi-tenant isolation check
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
            cust_res = await db.execute(select(Customer).where(Customer.id == ticket.customer_id))
            customer = cust_res.scalar_one_or_none()
            cust_tenant = (customer.metadata_ or {}).get("tenant_id") if customer else None
            if not cust_tenant or not isinstance(cust_tenant, str) or cust_tenant.strip() != staff_tenant:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=f"Ticket {ticket_id} not found.",
                )

    return TicketDetailResponse(
        id=str(ticket.id),
        ticket_number=ticket.ticket_number,
        conversation_id=str(ticket.conversation_id) if ticket.conversation_id else None,
        customer_id=str(ticket.customer_id),
        assigned_agent_id=str(ticket.assigned_agent_id) if ticket.assigned_agent_id else None,
        assigned_team=ticket.assigned_team,
        title=ticket.title,
        description=ticket.description,
        masked_summary=ticket.masked_summary,
        priority=ticket.priority,
        priority_score=ticket.priority_score,
        priority_explanation=ticket.priority_explanation,
        status=ticket.status,
        category=ticket.category,
        cluster_group_id=str(ticket.cluster_group_id) if ticket.cluster_group_id else None,
        sla_calendar_id=str(ticket.sla_calendar_id),
        sla_target_minutes=ticket.sla_target_minutes,
        sla_warning_at=ticket.sla_warning_at,
        sla_breach_at=ticket.sla_breach_at,
        sla_warning_sent=ticket.sla_warning_sent,
        sla_breached=ticket.sla_breached,
        sla_paused_at=ticket.sla_paused_at,
        total_paused_duration_seconds=ticket.total_paused_duration_seconds,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        synthesis_status=ticket.synthesis_status,
        routing_status=ticket.routing_status,
        resolution_status=ticket.resolution_status,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


@router.get(
    "/{ticket_id}/evidence",
    response_model=EvidenceListResponse,
    summary="List active evidence files associated with a support ticket",
)
async def list_ticket_evidence(
    ticket_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> EvidenceListResponse:
    """Retrieve active evidence records associated with a ticket.

    Requires staff authentication (JWT with approved staff role).
    Enforces strict tenant isolation: tickets belonging to another tenant return 404.
    Excludes PURGED files.
    """
    _require_staff(auth)

    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found.",
        )

    # Multi-tenant isolation verification
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
        cust_res = await db.execute(select(Customer).where(Customer.id == ticket.customer_id))
        customer = cust_res.scalar_one_or_none()
        cust_tenant = (customer.metadata_ or {}).get("tenant_id") if customer else None
        if not cust_tenant or not isinstance(cust_tenant, str) or cust_tenant.strip() != staff_tenant:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found.",
            )

    safe_limit = limit if isinstance(limit, int) else 20
    safe_offset = offset if isinstance(offset, int) else 0

    base_query = (
        select(EvidenceFile)
        .where(EvidenceFile.ticket_id == ticket_id, EvidenceFile.status != "PURGED")
    )
    count_query = (
        select(func.count())
        .select_from(EvidenceFile)
        .where(EvidenceFile.ticket_id == ticket_id, EvidenceFile.status != "PURGED")
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


@router.post(
    "/synthesize",
    response_model=SynthesizeTicketResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Synthesize structured ticket information from conversation",
)
async def synthesize_ticket(
    request: SynthesizeTicketRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
    correlation_id: str = Depends(get_correlation_id),
) -> SynthesizeTicketResponse:
    """Extract structured ticket data from the linked conversation using LLM + heuristics.

    For customers: can only synthesize their own tickets.
    For staff (JWT): can synthesize any ticket.

    If a synthesis already exists and force_resynthesis is False, returns 409.
    """
    svc = get_ticket_synthesis_service()
    customer_id = _get_customer_id(auth)

    try:
        result = await svc.synthesize_ticket(
            db=db,
            ticket_id=request.ticket_id,
            customer_id=customer_id,
            force_resynthesis=request.force_resynthesis,
        )
        await db.commit()
        return SynthesizeTicketResponse(**result)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc))
    except TicketAuthorizationError as exc:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=str(exc))
    except TicketSynthesisConflictError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidSynthesisTransitionError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        logger.error("Ticket synthesis failed for ticket %s: %s", request.ticket_id, exc)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ticket synthesis encountered an internal error.",
        )


@router.get(
    "/{ticket_id}/synthesis",
    response_model=SynthesisDetailResponse,
    summary="Get the current synthesis for a ticket",
)
async def get_synthesis(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> SynthesisDetailResponse:
    """Retrieve the latest synthesis record for a ticket."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.customer import Customer
    from app.models.ticket import Ticket

    result = await db.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(selectinload(Ticket.ticket_synthesis))
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found.")

    customer_id = _get_customer_id(auth)
    if customer_id and str(ticket.customer_id) != str(customer_id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    ts = ticket.ticket_synthesis
    return SynthesisDetailResponse(
        ticket_id=str(ticket_id),
        synthesis_status=ticket.synthesis_status,
        synthesis_id=str(ts.id) if ts else None,
        problem_statement=ts.problem_statement if ts else None,
        product_or_service=ts.product_or_service if ts else None,
        reference_identifiers=(ts.reference_identifiers or []) if ts else [],
        issue_category=ts.issue_category if ts else None,
        issue_subcategory=ts.issue_subcategory if ts else None,
        resolution_sought=ts.resolution_sought if ts else None,
        actions_already_attempted=(ts.actions_already_attempted or []) if ts else [],
        missing_fields=(ts.missing_fields or []) if ts else [],
        contradictions=(ts.contradictions or []) if ts else [],
        completeness_score=ts.completeness_score if ts else 0.0,
        extraction_confidence=ts.extraction_confidence if ts else 0.0,
        extraction_provenance=(ts.extraction_provenance or {}) if ts else {},
        llm_model_used=ts.llm_model_used if ts else None,
        synthesis_version=ts.synthesis_version if ts else 0,
    )


@router.post(
    "/{ticket_id}/synthesis/completeness",
    response_model=CompletenessResponse,
    summary="Re-evaluate ticket completeness",
)
async def evaluate_completeness(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> CompletenessResponse:
    """Re-evaluate completeness of an existing ticket synthesis."""
    svc = get_ticket_synthesis_service()
    customer_id = _get_customer_id(auth)

    try:
        result = await svc.evaluate_completeness(db=db, ticket_id=ticket_id, customer_id=customer_id)
        return CompletenessResponse(ticket_id=str(ticket_id), **result.model_dump())
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/synthesis/clarify",
    response_model=ClarifyResponse,
    status_code=http_status.HTTP_200_OK,
    summary="Generate targeted clarification questions",
)
async def generate_clarification(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ClarifyResponse:
    """Generate a new clarification round for a ticket needing information.

    Returns should_ask=False and a stop_reason when no new questions are needed.
    """
    svc = get_ticket_synthesis_service()
    customer_id = _get_customer_id(auth)

    try:
        result = await svc.generate_clarification(db=db, ticket_id=ticket_id, customer_id=customer_id)
        await db.commit()
        return ClarifyResponse(**result)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except InvalidSynthesisTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/{ticket_id}/synthesis/respond",
    response_model=ClarificationRespondResponse,
    summary="Submit customer clarification response",
)
async def respond_to_clarification(
    ticket_id: uuid.UUID,
    request: ClarificationRespondRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ClarificationRespondResponse:
    """Process a customer's response to a clarification round.

    Merges new information, re-evaluates completeness, and updates synthesis_status.
    """
    svc = get_ticket_synthesis_service()
    customer_id = _get_customer_id(auth)

    try:
        result = await svc.respond_to_clarification(
            db=db,
            ticket_id=ticket_id,
            clarification_request_id=request.clarification_request_id,
            customer_response=request.customer_response,
            customer_id=customer_id,
        )
        await db.commit()
        return ClarificationRespondResponse(**result)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except TicketSynthesisConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get(
    "/{ticket_id}/synthesis/clarifications",
    response_model=ClarificationListResponse,
    summary="List all clarification rounds for a ticket",
)
async def list_clarifications(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ClarificationListResponse:
    """Return all clarification rounds (all statuses) for a ticket."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.customer import Customer
    from app.models.ticket import Ticket

    result = await db.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(
            selectinload(Ticket.ticket_synthesis).selectinload(
                TicketSynthesis.clarification_requests
            )
        )
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found.")

    customer_id = _get_customer_id(auth)
    if customer_id and str(ticket.customer_id) != str(customer_id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    rounds: List[ClarificationListItem] = []
    if ticket.ticket_synthesis:
        for cr in sorted(
            ticket.ticket_synthesis.clarification_requests or [],
            key=lambda r: r.round_number,
        ):
            rounds.append(
                ClarificationListItem(
                    id=str(cr.id),
                    round_number=cr.round_number,
                    status=cr.status,
                    questions=cr.questions or [],
                    targeted_missing_fields=cr.targeted_missing_fields or [],
                    completeness_score_before=cr.completeness_score_before,
                    completeness_score_after=cr.completeness_score_after,
                    created_at=cr.created_at.isoformat() if cr.created_at else "",
                    responded_at=cr.responded_at.isoformat() if cr.responded_at else None,
                )
            )

    return ClarificationListResponse(ticket_id=str(ticket_id), rounds=rounds)


@router.post(
    "/{ticket_id}/synthesis/complete",
    summary="Mark ticket synthesis as complete (agent/admin only)",
)
async def mark_complete(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """Explicitly mark a ticket's synthesis as COMPLETE. Requires staff JWT."""
    _require_staff(auth)
    svc = get_ticket_synthesis_service()

    try:
        result = await svc.mark_complete(db=db, ticket_id=ticket_id)
        await db.commit()
        return result
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidSynthesisTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ==============================================================================
# Priority & Intelligent Routing Endpoints
# ==============================================================================

@router.post(
    "/{ticket_id}/priority/evaluate",
    response_model=PriorityEvaluateResponse,
    summary="Evaluate and update ticket priority (staff only)",
)
async def evaluate_priority_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> PriorityEvaluateResponse:
    """Evaluates ticket priority deterministically with explainable supporting facts.

    Strict RBAC: Customers cannot directly evaluate or manipulate internal ticket priority.
    """
    _require_staff(auth)
    routing_svc = get_ticket_routing_service()

    try:
        result = await routing_svc.evaluate_priority(
            db=db,
            ticket_id=ticket_id,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return PriorityEvaluateResponse(
            ticket_id=str(ticket_id),
            priority=result.priority.value,
            priority_score=result.priority_score,
            priority_floor=result.priority_floor.value,
            reason=result.reason,
            supporting_facts=result.supporting_facts,
        )
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketRoutingAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/route",
    response_model=RouteTicketResponse,
    summary="Intelligently route ticket to best team/agent (staff only)",
)
async def route_ticket_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> RouteTicketResponse:
    """Intelligently routes a ticket based on department expertise, skills, and workload.

    Enforces transaction-safe agent capacity row locking.
    Falls back safely to department team queue if all agents are saturated.
    """
    _require_staff(auth)
    routing_svc = get_ticket_routing_service()

    try:
        decision = await routing_svc.route_ticket(
            db=db,
            ticket_id=ticket_id,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return RouteTicketResponse(
            ticket_id=str(ticket_id),
            target_department=decision.target_department,
            assigned_agent_id=decision.assigned_agent_id,
            assigned_agent_name=decision.assigned_agent_name,
            routing_status=decision.routing_status,
            reason=decision.reason,
            candidates_evaluated=decision.candidates_evaluated,
            candidate_details=decision.candidate_details,
        )
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketRoutingAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/assign",
    response_model=AssignTicketResponse,
    summary="Manually assign ticket to an agent (staff only)",
)
async def manual_assign_endpoint(
    ticket_id: uuid.UUID,
    request: AssignTicketRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> AssignTicketResponse:
    """Explicitly assign or reassign a ticket to a specific agent with capacity locking."""
    _require_staff(auth)
    routing_svc = get_ticket_routing_service()

    try:
        result = await routing_svc.manually_assign_ticket(
            db=db,
            ticket_id=ticket_id,
            target_agent_id=request.agent_id,
            reason=request.reason,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            force_override=request.force_override,
            customer_id=None,
        )
        await db.commit()
        return AssignTicketResponse(**result)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketRoutingAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except AgentRoleNotAuthorizedError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except AgentCapacityExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get(
    "/{ticket_id}/routing-history",
    response_model=RoutingHistoryResponse,
    summary="Get routing and priority audit trail for a ticket",
)
async def get_routing_history_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> RoutingHistoryResponse:
    """Returns the immutable chronological history of routing and priority decisions.

    Customers may only view audit history for tickets they own.
    """
    customer_id = _get_customer_id(auth)
    routing_svc = get_ticket_routing_service()

    try:
        events = await routing_svc.get_routing_history(
            db=db,
            ticket_id=ticket_id,
            customer_id=customer_id,
        )
        return RoutingHistoryResponse(
            ticket_id=str(ticket_id),
            events=[RoutingHistoryItem(**e) for e in events],
        )
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TicketRoutingAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get(
    "/teams/workload",
    response_model=TeamWorkloadResponse,
    summary="Get department capacity and workload breakdown (supervisor/admin only)",
)
async def get_teams_workload_endpoint(
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> TeamWorkloadResponse:
    """Aggregated capacity and active workload overview by department."""
    _require_staff(auth)
    routing_svc = get_ticket_routing_service()

    try:
        workloads = await routing_svc.get_teams_workload(
            db=db,
            actor_roles=auth.roles,
        )
        return TeamWorkloadResponse(
            teams=[TeamWorkloadItem(**w) for w in workloads]
        )
    except TicketRoutingAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ==============================================================================
# 5. RESOLUTION & ACTION ORCHESTRATION ENDPOINTS (DAY 14)
# ==============================================================================

def _action_to_item(action) -> ResolutionActionItem:
    return ResolutionActionItem(
        action_id=str(action.id),
        action_type=action.action_type,
        action_key=action.action_key,
        action_params=action.action_params or {},
        status=action.status,
        is_sensitive=action.is_sensitive,
        requires_approval=action.requires_approval,
        approved_by=action.approved_by,
        execution_attempts=action.execution_attempts,
        execution_result=action.execution_result or {},
    )


def _resolution_to_response(res, actions: Optional[List[Any]] = None) -> EvaluateResolutionResponse:
    action_items = [_action_to_item(a) for a in (actions if actions is not None else (getattr(res, "actions", []) or []))]
    return EvaluateResolutionResponse(
        ticket_id=str(res.ticket_id),
        resolution_id=str(res.id),
        status=res.status,
        playbook_category=res.playbook_category,
        recommended_resolution=res.recommended_resolution,
        confidence_score=res.confidence_score,
        eligibility_decision=res.eligibility_decision,
        requires_human_approval=res.requires_human_approval,
        supporting_evidence=res.supporting_evidence or [],
        rejection_or_escalation_reason=res.rejection_or_escalation_reason,
        actions=action_items,
    )


@router.post(
    "/{ticket_id}/resolution/evaluate",
    response_model=EvaluateResolutionResponse,
    summary="Evaluate ticket against category playbook and derive resolution plan (staff only)",
)
async def evaluate_resolution_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> EvaluateResolutionResponse:
    """Evaluates ticket against category playbook, proposing actions and verifying safeguards."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        res = await svc.evaluate_resolution(
            db=db,
            ticket_id=ticket_id,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        actions = await svc.get_resolution_actions(db, res.id)
        await db.commit()
        return _resolution_to_response(res, actions)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get(
    "/{ticket_id}/resolution",
    response_model=EvaluateResolutionResponse,
    summary="Get current resolution evaluation and actions for a ticket",
)
async def get_resolution_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> EvaluateResolutionResponse:
    """Retrieves current resolution plan and actions for a ticket."""
    customer_id = _get_customer_id(auth)
    svc = get_ticket_resolution_service()

    try:
        res = await svc.get_resolution(db=db, ticket_id=ticket_id, customer_id=customer_id)
        actions = await svc.get_resolution_actions(db, res.id)
        return _resolution_to_response(res, actions)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/resolution/actions",
    response_model=ResolutionActionItem,
    summary="Manually propose an action for ticket resolution (staff only)",
)
async def propose_action_endpoint(
    ticket_id: uuid.UUID,
    request: ProposeActionRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ResolutionActionItem:
    """Propose an action as part of ticket resolution."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        action = await svc.propose_action(
            db=db,
            ticket_id=ticket_id,
            action_type=request.action_type,
            action_params=request.action_params,
            is_sensitive=request.is_sensitive,
            requires_approval=request.requires_approval,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return _action_to_item(action)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/resolution/actions/{action_id}/approve",
    response_model=ResolutionActionItem,
    summary="Approve a sensitive resolution action (supervisor/admin only)",
)
async def approve_action_endpoint(
    ticket_id: uuid.UUID,
    action_id: uuid.UUID,
    request: ApproveActionRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ResolutionActionItem:
    """Explicitly approve a sensitive action prior to execution."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        action = await svc.approve_action(
            db=db,
            ticket_id=ticket_id,
            action_id=action_id,
            approval_reason=request.reason,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return _action_to_item(action)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionOrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/resolution/actions/{action_id}/reject",
    response_model=ResolutionActionItem,
    summary="Reject a proposed resolution action (supervisor/admin only)",
)
async def reject_action_endpoint(
    ticket_id: uuid.UUID,
    action_id: uuid.UUID,
    request: RejectActionRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ResolutionActionItem:
    """Explicitly reject a proposed action."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        action = await svc.reject_action(
            db=db,
            ticket_id=ticket_id,
            action_id=action_id,
            rejection_reason=request.reason,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return _action_to_item(action)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionOrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.patch(
    "/{ticket_id}/resolution/actions/{action_id}",
    response_model=ResolutionActionItem,
    summary="Update parameters of a proposed action (staff only; invalidates previous approval if modified)",
)
async def update_action_params_endpoint(
    ticket_id: uuid.UUID,
    action_id: uuid.UUID,
    request: UpdateActionParamsRequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ResolutionActionItem:
    """Updates parameters for an action. If action was approved, invalidates approval and forces re-approval."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        action = await svc.update_action_params(
            db=db,
            ticket_id=ticket_id,
            action_id=action_id,
            new_params=request.action_params,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return _action_to_item(action)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionOrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/resolution/actions/{action_id}/execute",
    response_model=ExecuteActionResponse,
    summary="Execute an approved or non-sensitive resolution action (staff only)",
)
async def execute_action_endpoint(
    ticket_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ExecuteActionResponse:
    """Executes a single resolution action safely with database row locks."""
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        res = await svc.execute_action(
            db=db,
            ticket_id=ticket_id,
            action_id=action_id,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return ExecuteActionResponse(**res)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ActionNotApprovedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ActionMaxAttemptsExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ActionConcurrencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ActionOrchestrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{ticket_id}/resolution/verify",
    response_model=VerifyResolutionResponse,
    summary="Verify resolution completion criteria and finalize ticket (staff only)",
)
async def verify_resolution_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> VerifyResolutionResponse:
    """Verifies that all actions, evidence, and approvals satisfy the playbook criteria.

    If verified, transitions operational ticket status to RESOLVED.
    If unverified or failed, transitions ticket to ESCALATED and triggers Day 13 routing.
    """
    _require_staff(auth)
    svc = get_ticket_resolution_service()

    try:
        res = await svc.verify_resolution(
            db=db,
            ticket_id=ticket_id,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            customer_id=None,
        )
        await db.commit()
        return VerifyResolutionResponse(**res)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get(
    "/{ticket_id}/resolution/history",
    response_model=ResolutionHistoryResponse,
    summary="Get resolution audit trail for a ticket",
)
async def get_resolution_history_endpoint(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> ResolutionHistoryResponse:
    """Returns the immutable chronological history of resolution decisions and actions."""
    customer_id = _get_customer_id(auth)
    svc = get_ticket_resolution_service()

    try:
        events = await svc.get_resolution_history(
            db=db,
            ticket_id=ticket_id,
            customer_id=customer_id,
        )
        return ResolutionHistoryResponse(
            ticket_id=str(ticket_id),
            events=[RoutingHistoryItem(**e) for e in events],
        )
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ResolutionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


class RecalculateSLARequest(BaseModel):
    calendar_id: Optional[uuid.UUID] = Field(default=None, description="Optional SLA calendar ID to filter or apply.")
    priority_targets: Optional[Dict[str, int]] = Field(
        default=None,
        description="Optional map of priority to target minutes, e.g. {'URGENT': 60, 'HIGH': 120}."
    )
    dry_run: bool = Field(default=False, description="If True, calculate changes without committing updates.")


class RecalculateSLAResponse(BaseModel):
    total_eligible: int
    updated_count: int
    skipped_count: int
    failed_count: int
    dry_run: bool
    details: List[Dict[str, Any]] = Field(default_factory=list)


@router.post(
    "/sla/recalculate",
    response_model=RecalculateSLAResponse,
    summary="Recalculate SLA targets and deadlines for open tickets",
)
async def recalculate_sla_endpoint(
    body: RecalculateSLARequest,
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> RecalculateSLAResponse:
    """Administratively recalculates SLA deadlines across OPEN and IN_PROGRESS tickets.

    Enforces administrative authorization, validates priority targets, preserves paused durations,
    respects business hours/holidays, and emits immutable TicketEvent records.
    """
    roles_upper = [r.upper() for r in (auth.roles or [])]
    if not any(r in ("ADMIN", "SUPERVISOR", "STAFF") for r in roles_upper):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Administrative authorization required to recalculate ticket SLAs.",
        )

    if body.priority_targets:
        for k, v in body.priority_targets.items():
            if not isinstance(v, int) or v <= 0:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Priority target minutes for '{k}' must be a strictly positive integer, got {v}.",
                )

    svc = get_ticket_synthesis_service()
    try:
        res = await svc.recalculate_open_tickets_sla(
            db=db,
            calendar_id=body.calendar_id,
            priority_targets=body.priority_targets,
            actor_id=auth.user_id_or_email or "SYSTEM",
            actor_roles=auth.roles,
            dry_run=body.dry_run,
        )
        if not body.dry_run:
            await db.commit()
        return RecalculateSLAResponse(**res)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to recalculate SLA: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"SLA recalculation failed: {str(exc)}")




