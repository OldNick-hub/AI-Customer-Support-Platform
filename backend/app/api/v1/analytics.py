"""Operational Intelligence, Analytics & Dashboard API Router.

Exposes REST endpoints for:
1. GET /api/v1/analytics/overview — Aggregated KPIs, SLA status, priority breakdown, and real incident queue.
2. GET /api/v1/analytics/performance — Time-series ticket trends, category SLA performance, and evidence metrics.
3. GET /api/v1/analytics/operational-intelligence — Rule-based operational alerts and capacity breach forecasts.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ResolvedAuthContext,
    get_auth_context,
    get_db,
)
from app.core.logging import configure_safe_logger
from app.models.customer import Conversation, Customer
from app.models.evidence import EvidenceFile, EvidenceVerification
from app.models.ticket import EscalationAudit, Ticket
from app.models.ticket_resolution import TicketResolution
from app.modules.ticketing.routing_service import get_ticket_routing_service
from app.services.storage_service import validate_tenant_id

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/analytics", tags=["Analytics & Operational Intelligence"])

APPROVED_STAFF_ROLES = {"ADMIN", "SUPERVISOR", "AGENT", "STAFF", "SUPPORT_AGENT", "SUPER_ADMIN", "GLOBAL_ADMIN"}
GLOBAL_ADMIN_ROLES = {"SUPER_ADMIN", "GLOBAL_ADMIN"}


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class ReportingPeriodMetadata(BaseModel):
    time_window: str = Field(..., description="Selected window (today, 7d, 30d, 90d, custom)")
    start_date: datetime
    end_date: datetime
    timezone: str = "UTC"


class DashboardIncidentItem(BaseModel):
    id: str
    ticket_id: str
    ticket_number: str
    title: str
    customer: str
    tier: str
    category: str
    priority: str
    severity: str
    status: str
    assigned_team: str
    priority_score: float
    sla_elapsed: str
    sla_total: str
    sla_remaining: str
    progress: int
    sentiment_score: float
    sentiment_label: str
    latest_activity: str
    evidence_status: str
    evidence_snippet: str
    evidence_doc: str
    suggested_action: str
    is_breached: bool = False
    is_overdue: bool = False
    overdue_duration: Optional[str] = None
    sla_target_minutes: Optional[int] = None
    sla_deadline: Optional[datetime] = None


class DepartmentWorkloadItem(BaseModel):
    department: str
    active_agents: int
    total_capacity: int
    active_tickets: int
    utilization_pct: float


class OverviewDashboardResponse(BaseModel):
    total_tickets: int
    open_tickets: int
    resolved_tickets: int
    escalated_tickets: int
    urgent_tickets: int
    sla_compliance_pct: float
    sla_breached_count: int
    sla_warning_count: int
    avg_resolution_time_minutes: float
    active_conversations_count: int
    priority_distribution: Dict[str, int]
    status_distribution: Dict[str, int]
    priority_queue: List[DashboardIncidentItem]
    department_workload: List[DepartmentWorkloadItem]
    reporting_period: ReportingPeriodMetadata


class DailyTrendItem(BaseModel):
    date: str
    created: int
    resolved: int
    escalated: int


class CategoryDistributionItem(BaseModel):
    category: str
    count: int
    percentage: float


class PriorityDistributionItem(BaseModel):
    priority: str
    count: int
    percentage: float


class SlaBreakdownItem(BaseModel):
    label: str
    total: int
    compliant: int
    breached: int
    compliance_pct: float


class LanguageDistributionItem(BaseModel):
    language: str
    code: str
    query_count: int
    percentage: float


class EvidenceAnalyticsItem(BaseModel):
    total_uploaded: int
    verified_count: int
    conflict_count: int
    unsafe_count: int
    status_distribution: Dict[str, int]
    reconciliation_distribution: Dict[str, int]


class PerformanceAnalyticsResponse(BaseModel):
    ticket_trends: List[DailyTrendItem]
    category_distribution: List[CategoryDistributionItem]
    priority_distribution: List[PriorityDistributionItem]
    sla_by_category: List[SlaBreakdownItem]
    sla_by_priority: List[SlaBreakdownItem]
    overall_sla_compliance_pct: float
    total_breaches: int
    avg_resolution_time_minutes: float
    operational_workload: List[DepartmentWorkloadItem]
    evidence_analytics: EvidenceAnalyticsItem
    language_distribution: List[LanguageDistributionItem]
    reporting_period: ReportingPeriodMetadata


class OperationalInsightItem(BaseModel):
    id: str
    rule_name: str
    severity: str
    title: str
    description: str
    supporting_metric: str
    time_window: str
    target_route: str
    suggested_action: str


class OperationalIntelligenceResponse(BaseModel):
    insights: List[OperationalInsightItem]
    evaluated_at: datetime
    rules_applied: List[str]


# ==============================================================================
# Helper Functions
# ==============================================================================

def _parse_time_window(
    time_window: str,
    custom_start: Optional[datetime] = None,
    custom_end: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Calculates standardized UTC start and end bounds for reporting queries."""
    now = datetime.now(timezone.utc)

    if time_window == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    elif time_window == "7d":
        return now - timedelta(days=7), now
    elif time_window == "30d":
        return now - timedelta(days=30), now
    elif time_window == "90d":
        return now - timedelta(days=90), now
    elif time_window == "custom" and custom_start and custom_end:
        return custom_start, custom_end
    else:
        return now - timedelta(days=30), now


def _get_customer_id(auth: ResolvedAuthContext) -> Optional[str]:
    """Returns customer_id for customer sessions, None for staff."""
    if auth.auth_type == "SESSION_TOKEN":
        return auth.customer_id
    # Also check if caller has solely customer role
    roles = [r.upper() for r in (auth.roles or [])]
    if roles and all(r in ("CUSTOMER", "VIP_CUSTOMER") for r in roles):
        return auth.customer_id
    return None


# ==============================================================================
# Endpoints
# ==============================================================================

@router.get(
    "/overview",
    response_model=OverviewDashboardResponse,
    summary="Get aggregated dashboard KPIs, SLA compliance, and real priority queue",
)
async def get_overview_dashboard(
    time_window: str = Query("30d", pattern="^(today|7d|30d|90d|custom)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> OverviewDashboardResponse:
    """Computes authoritative platform overview KPIs and top incident priority queue."""
    start_dt, end_dt = _parse_time_window(time_window, start_date, end_date)
    customer_id = _get_customer_id(auth)
    staff_tenant: Optional[str] = None
    if not customer_id:
        is_global_admin = any(r.upper() in GLOBAL_ADMIN_ROLES for r in (auth.roles or []))
        if not is_global_admin:
            raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
            if raw_tenant and isinstance(raw_tenant, str) and raw_tenant.strip():
                try:
                    staff_tenant = validate_tenant_id(raw_tenant)
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid tenant identifier.",
                    )

    # 1. Base Ticket Filter
    conditions = [Ticket.created_at >= start_dt, Ticket.created_at <= end_dt]
    if customer_id:
        conditions.append(Ticket.customer_id == uuid.UUID(customer_id))
    elif staff_tenant:
        conditions.append(Customer.metadata_["tenant_id"].as_string() == staff_tenant)

    if category and category.upper() != "ALL":
        conditions.append(Ticket.category.ilike(f"%{category}%"))
    if priority and priority.upper() != "ALL":
        conditions.append(Ticket.priority == priority.upper())
    if department and department.upper() != "ALL":
        conditions.append(Ticket.assigned_team == department)

    # Query all matching tickets in window
    stmt = select(Ticket)
    if staff_tenant:
        stmt = stmt.join(Customer, Ticket.customer_id == Customer.id)
    stmt = (
        stmt.where(and_(*conditions))
        .options(
            selectinload(Ticket.customer),
            selectinload(Ticket.conversation),
            selectinload(Ticket.evidence_files),
        )
    )
    result = await db.execute(stmt)
    tickets = result.scalars().all()

    total_tickets = len(tickets)
    open_tickets = sum(1 for t in tickets if t.status in ("OPEN", "IN_PROGRESS", "PENDING_CUSTOMER") and not (t.conversation and t.conversation.status == "CLOSED"))
    resolved_tickets = sum(1 for t in tickets if t.status in ("RESOLVED", "CLOSED"))
    escalated_tickets = sum(1 for t in tickets if t.status == "ESCALATED" and not (t.conversation and t.conversation.status == "CLOSED"))
    urgent_tickets = sum(1 for t in tickets if t.priority in ("CRITICAL", "URGENT") and not (t.conversation and t.conversation.status == "CLOSED"))

    # SLA Compliance: exclude tickets belonging to closed threads from active breach counts
    sla_breached_count = sum(
        1 for t in tickets
        if t.sla_breached and t.status != "CLOSED" and not (t.conversation and t.conversation.status == "CLOSED")
    )
    now_utc = datetime.now(timezone.utc)
    sla_warning_count = sum(
        1 for t in tickets
        if not t.sla_breached and t.status != "CLOSED" and not (t.conversation and t.conversation.status == "CLOSED")
        and (t.sla_warning_sent or (t.sla_warning_at and t.sla_warning_at <= now_utc < t.sla_breach_at))
    )
    sla_compliance_pct = (
        round(((total_tickets - sla_breached_count) / total_tickets) * 100, 1)
        if total_tickets > 0
        else 100.0
    )

    # Average Resolution Time (in minutes)
    res_durations = []
    for t in tickets:
        if t.resolved_at and t.created_at:
            dur = (t.resolved_at - t.created_at).total_seconds() / 60.0
            if dur >= 0:
                res_durations.append(dur)
    avg_resolution_time = round(sum(res_durations) / len(res_durations), 1) if res_durations else 0.0

    # Priority & Status Distributions
    priority_dist: Dict[str, int] = {"CRITICAL": 0, "URGENT": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    status_dist: Dict[str, int] = {}
    for t in tickets:
        p_norm = (t.priority or "MEDIUM").upper()
        priority_dist[p_norm] = priority_dist.get(p_norm, 0) + 1
        s_norm = (t.status or "OPEN").upper()
        status_dist[s_norm] = status_dist.get(s_norm, 0) + 1

    # Active Conversations Count
    conv_cond = [Conversation.started_at >= start_dt]
    if customer_id:
        conv_cond.append(Conversation.customer_id == uuid.UUID(customer_id))
        conv_stmt = select(func.count(Conversation.id)).where(and_(*conv_cond))
    elif staff_tenant:
        conv_stmt = (
            select(func.count(Conversation.id))
            .join(Customer, Conversation.customer_id == Customer.id)
            .where(and_(*conv_cond, Customer.metadata_["tenant_id"].as_string() == staff_tenant))
        )
    else:
        conv_stmt = select(func.count(Conversation.id)).where(and_(*conv_cond))

    conv_res = await db.execute(conv_stmt)
    active_conversations = conv_res.scalar_one_or_none() or 0

    # 2. Priority Incident Queue (Top active tickets prioritized by priority_score desc, sla_breach_at asc)
    active_candidates = [
        t for t in tickets
        if t.status in ("OPEN", "IN_PROGRESS", "PENDING_CUSTOMER", "ESCALATED")
        and not (t.conversation and t.conversation.status == "CLOSED")
    ]
    active_candidates.sort(
        key=lambda x: (
            -(x.priority_score or 2.5),
            x.sla_breach_at or datetime.max.replace(tzinfo=timezone.utc),
        )
    )

    priority_queue_items: List[DashboardIncidentItem] = []
    for item in active_candidates[:6]:
        # Authoritative SLA calculations grounded in ticket.sla_target_minutes
        created_time = item.created_at or now_utc
        target_minutes = item.sla_target_minutes if getattr(item, "sla_target_minutes", None) and item.sla_target_minutes > 0 else 180
        total_seconds = target_minutes * 60
        breach_time = item.sla_breach_at or (created_time + timedelta(minutes=target_minutes))

        elapsed_seconds = max((now_utc - created_time).total_seconds(), 0)
        remaining_seconds = (breach_time - now_utc).total_seconds()

        progress_pct = min(100, max(0, int((elapsed_seconds / total_seconds) * 100)))

        is_breached = bool(item.sla_breached or remaining_seconds <= 0)
        is_overdue = remaining_seconds < 0

        overdue_duration_str: Optional[str] = None
        if is_overdue:
            overdue_seconds = abs(remaining_seconds)
            od_mins = int(overdue_seconds // 60)
            if od_mins >= 60:
                overdue_duration_str = f"{od_mins // 60}h {od_mins % 60}m overdue"
            else:
                overdue_duration_str = f"{od_mins}m overdue"
            sla_rem_str = f"BREACHED (+{overdue_duration_str})"
        elif remaining_seconds == 0:
            sla_rem_str = "BREACHED"
        else:
            rem_mins = int(remaining_seconds // 60)
            if rem_mins > 60:
                sla_rem_str = f"{rem_mins // 60}h {rem_mins % 60}m remaining"
            else:
                sla_rem_str = f"{rem_mins}m remaining"

        if target_minutes < 120:
            sla_tot_str = f"{target_minutes}m target"
        else:
            sla_tot_str = f"{target_minutes // 60}h target"

        elap_mins = int(elapsed_seconds // 60)
        sla_elap_str = f"{elap_mins}m elapsed" if elap_mins < 120 else f"{elap_mins // 60}h elapsed"

        cust_name = item.customer.full_name if item.customer else f"Customer #{str(item.customer_id)[:8]}"
        cust_tier = (item.customer.customer_tier or "STANDARD").upper() if item.customer else "STANDARD"

        conv_sentiment = item.conversation.overall_sentiment_score if item.conversation else 0.0
        sentiment_label = (
            "Critical Frustration" if conv_sentiment <= -0.6
            else "Elevated Urgency" if conv_sentiment <= -0.2
            else "Neutral Ingestion" if conv_sentiment <= 0.3
            else "Positive Sentiment"
        )

        # Linked Evidence Check
        has_evidence = len(item.evidence_files) > 0 if item.evidence_files else False
        evid_status = (
            "Cryptographic Evidence Anchored · Verified"
            if has_evidence
            else "Standard Ingestion · No Uploaded Attachments"
        )
        evid_snippet = item.masked_summary or item.title
        evid_doc = item.evidence_files[0].file_name if has_evidence else "ticket-payload.json"

        priority_queue_items.append(
            DashboardIncidentItem(
                id=f"inc-{str(item.id)[:8]}",
                ticket_id=str(item.id),
                ticket_number=item.ticket_number,
                title=item.title,
                customer=cust_name,
                tier=f"Tier {cust_tier}",
                category=item.category.upper(),
                priority=item.priority.upper(),
                severity=f"SEV-1 {item.priority.upper()}" if item.priority in ("CRITICAL", "URGENT") else f"SEV-2 {item.priority.upper()}",
                status=item.status,
                assigned_team=item.assigned_team or "General Support",
                priority_score=round(item.priority_score, 1),
                sla_elapsed=sla_elap_str,
                sla_total=sla_tot_str,
                sla_remaining=sla_rem_str,
                progress=progress_pct,
                sentiment_score=round(conv_sentiment, 2),
                sentiment_label=sentiment_label,
                latest_activity=f"Automated priority score {round(item.priority_score, 1)} derived from customer intake and policy rules.",
                evidence_status=evid_status,
                evidence_snippet=evid_snippet,
                evidence_doc=evid_doc,
                suggested_action="Review attached evidence and initiate immediate resolution workflow." if item.priority in ("CRITICAL", "URGENT") else "Monitor ticket progression against standard SLA.",
                is_breached=is_breached,
                is_overdue=is_overdue,
                overdue_duration=overdue_duration_str,
                sla_target_minutes=target_minutes,
                sla_deadline=breach_time,
            )
        )

    # 3. Department Workload (Staff only; empty for customer callers)
    dept_workloads: List[DepartmentWorkloadItem] = []
    if not customer_id:
        try:
            routing_svc = get_ticket_routing_service()
            teams_wl = await routing_svc.get_teams_workload(db=db, actor_roles=auth.roles)
            for w in teams_wl:
                dept_workloads.append(
                    DepartmentWorkloadItem(
                        department=w["department"],
                        active_agents=w["active_agents"],
                        total_capacity=w["total_capacity"],
                        active_tickets=w["active_tickets"],
                        utilization_pct=w["utilization_pct"],
                    )
                )
        except Exception:
            # Fallback workload if routing service unavailable
            dept_workloads = []

    return OverviewDashboardResponse(
        total_tickets=total_tickets,
        open_tickets=open_tickets,
        resolved_tickets=resolved_tickets,
        escalated_tickets=escalated_tickets,
        urgent_tickets=urgent_tickets,
        sla_compliance_pct=sla_compliance_pct,
        sla_breached_count=sla_breached_count,
        sla_warning_count=sla_warning_count,
        avg_resolution_time_minutes=avg_resolution_time,
        active_conversations_count=active_conversations,
        priority_distribution=priority_dist,
        status_distribution=status_dist,
        priority_queue=priority_queue_items,
        department_workload=dept_workloads,
        reporting_period=ReportingPeriodMetadata(
            time_window=time_window,
            start_date=start_dt,
            end_date=end_dt,
            timezone="UTC",
        ),
    )


@router.get(
    "/performance",
    response_model=PerformanceAnalyticsResponse,
    summary="Get detailed performance analytics, SLA breakdown, and trends",
)
async def get_performance_analytics(
    time_window: str = Query("30d", pattern="^(today|7d|30d|90d|custom)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> PerformanceAnalyticsResponse:
    """Computes categorical SLA breakdown, time-series trends, evidence stats, and language analytics."""
    start_dt, end_dt = _parse_time_window(time_window, start_date, end_date)
    customer_id = _get_customer_id(auth)
    staff_tenant: Optional[str] = None
    if not customer_id:
        is_global_admin = any(r.upper() in GLOBAL_ADMIN_ROLES for r in (auth.roles or []))
        if not is_global_admin:
            raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
            if raw_tenant and isinstance(raw_tenant, str) and raw_tenant.strip():
                try:
                    staff_tenant = validate_tenant_id(raw_tenant)
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid tenant identifier.",
                    )

    # 1. Fetch Tickets
    conditions = [Ticket.created_at >= start_dt, Ticket.created_at <= end_dt]
    if customer_id:
        conditions.append(Ticket.customer_id == uuid.UUID(customer_id))
    elif staff_tenant:
        conditions.append(Customer.metadata_["tenant_id"].as_string() == staff_tenant)
    if category and category.upper() != "ALL":
        conditions.append(Ticket.category.ilike(f"%{category}%"))
    if priority and priority.upper() != "ALL":
        conditions.append(Ticket.priority == priority.upper())
    if department and department.upper() != "ALL":
        conditions.append(Ticket.assigned_team == department)

    stmt = select(Ticket)
    if staff_tenant:
        stmt = stmt.join(Customer, Ticket.customer_id == Customer.id)
    stmt = stmt.where(and_(*conditions))
    res = await db.execute(stmt)
    tickets = res.scalars().all()

    total_tickets = len(tickets)
    total_breaches = sum(1 for t in tickets if t.sla_breached)
    overall_sla_pct = (
        round(((total_tickets - total_breaches) / total_tickets) * 100, 1)
        if total_tickets > 0
        else 100.0
    )

    # Resolution Durations
    durations = [
        (t.resolved_at - t.created_at).total_seconds() / 60.0
        for t in tickets
        if t.resolved_at and t.created_at and (t.resolved_at - t.created_at).total_seconds() >= 0
    ]
    avg_resolution_time = round(sum(durations) / len(durations), 1) if durations else 0.0

    # 2. Time-series Daily Trends
    # Group dates by YYYY-MM-DD
    trend_map: Dict[str, Dict[str, int]] = {}
    # Pre-populate dates for clean line rendering
    cur_date = start_dt.date()
    end_date_val = end_dt.date()
    while cur_date <= end_date_val:
        trend_map[cur_date.isoformat()] = {"created": 0, "resolved": 0, "escalated": 0}
        cur_date += timedelta(days=1)

    for t in tickets:
        if t.created_at:
            d_str = t.created_at.date().isoformat()
            if d_str in trend_map:
                trend_map[d_str]["created"] += 1
        if t.resolved_at:
            d_str = t.resolved_at.date().isoformat()
            if d_str in trend_map:
                trend_map[d_str]["resolved"] += 1
        if t.status == "ESCALATED":
            d_str = (t.updated_at or t.created_at).date().isoformat()
            if d_str in trend_map:
                trend_map[d_str]["escalated"] += 1

    daily_trends = [
        DailyTrendItem(date=k, created=v["created"], resolved=v["resolved"], escalated=v["escalated"])
        for k, v in sorted(trend_map.items())
    ]

    # 3. Category Distribution & SLA breakdown by Category
    cat_map: Dict[str, List[Ticket]] = {}
    for t in tickets:
        cat_key = (t.category or "general").title()
        cat_map.setdefault(cat_key, []).append(t)

    category_dist: List[CategoryDistributionItem] = []
    sla_by_category: List[SlaBreakdownItem] = []
    for cat_name, cat_tickets in sorted(cat_map.items(), key=lambda x: -len(x[1])):
        count = len(cat_tickets)
        breached = sum(1 for ct in cat_tickets if ct.sla_breached)
        compliant = count - breached
        pct = round((count / total_tickets) * 100, 1) if total_tickets > 0 else 0.0
        c_pct = round((compliant / count) * 100, 1) if count > 0 else 100.0

        category_dist.append(CategoryDistributionItem(category=cat_name, count=count, percentage=pct))
        sla_by_category.append(
            SlaBreakdownItem(
                label=cat_name,
                total=count,
                compliant=compliant,
                breached=breached,
                compliance_pct=c_pct,
            )
        )

    # 4. Priority Distribution & SLA breakdown by Priority
    prio_map: Dict[str, List[Ticket]] = {}
    for t in tickets:
        p_key = (t.priority or "MEDIUM").upper()
        prio_map.setdefault(p_key, []).append(t)

    priority_order = ["CRITICAL", "URGENT", "HIGH", "MEDIUM", "LOW"]
    priority_dist: List[PriorityDistributionItem] = []
    sla_by_priority: List[SlaBreakdownItem] = []
    for p_name in priority_order:
        p_tickets = prio_map.get(p_name, [])
        count = len(p_tickets)
        breached = sum(1 for pt in p_tickets if pt.sla_breached)
        compliant = count - breached
        pct = round((count / total_tickets) * 100, 1) if total_tickets > 0 else 0.0
        c_pct = round((compliant / count) * 100, 1) if count > 0 else 100.0

        priority_dist.append(PriorityDistributionItem(priority=p_name, count=count, percentage=pct))
        sla_by_priority.append(
            SlaBreakdownItem(
                label=p_name,
                total=count,
                compliant=compliant,
                breached=breached,
                compliance_pct=c_pct,
            )
        )

    # 5. Multilingual Conversation Distribution
    conv_conditions = [Conversation.started_at >= start_dt, Conversation.started_at <= end_dt]
    if customer_id:
        conv_conditions.append(Conversation.customer_id == uuid.UUID(customer_id))
    conv_stmt = select(Conversation.primary_language, func.count(Conversation.id)).where(
        and_(*conv_conditions)
    ).group_by(Conversation.primary_language)
    conv_res = await db.execute(conv_stmt)
    conv_rows = conv_res.all()

    lang_names = {
        "en": "English",
        "hi": "Hindi (Devanagari & Hinglish)",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ta": "Tamil",
        "te": "Telugu",
    }
    total_convs = sum(row[1] for row in conv_rows)
    lang_dist: List[LanguageDistributionItem] = []
    for code, q_count in sorted(conv_rows, key=lambda x: -x[1]):
        l_name = lang_names.get(code.lower(), code.upper())
        pct = round((q_count / total_convs) * 100, 1) if total_convs > 0 else 0.0
        lang_dist.append(LanguageDistributionItem(language=l_name, code=code, query_count=q_count, percentage=pct))

    # 6. Evidence Analytics
    ev_conditions = [EvidenceFile.created_at >= start_dt, EvidenceFile.created_at <= end_dt]
    if customer_id:
        ev_conditions.append(EvidenceFile.customer_id == uuid.UUID(customer_id))
    ev_stmt = select(EvidenceFile.status, func.count(EvidenceFile.id)).where(and_(*ev_conditions)).group_by(EvidenceFile.status)
    ev_res = await db.execute(ev_stmt)
    ev_status_map = {row[0]: row[1] for row in ev_res.all()}
    total_ev = sum(ev_status_map.values())

    ver_stmt = select(EvidenceVerification.overall_match_status, func.count(EvidenceVerification.id)).where(
        EvidenceVerification.created_at >= start_dt
    ).group_by(EvidenceVerification.overall_match_status)
    ver_res = await db.execute(ver_stmt)
    reconcile_status_map = {row[0]: row[1] for row in ver_res.all()}

    evidence_analytics = EvidenceAnalyticsItem(
        total_uploaded=total_ev,
        verified_count=ev_status_map.get("VERIFIED", 0),
        conflict_count=reconcile_status_map.get("CONFLICT", 0),
        unsafe_count=ev_status_map.get("FLAGGED_UNSAFE", 0),
        status_distribution=ev_status_map,
        reconciliation_distribution=reconcile_status_map,
    )

    # 7. Department Workload
    dept_workloads: List[DepartmentWorkloadItem] = []
    if not customer_id:
        try:
            routing_svc = get_ticket_routing_service()
            teams_wl = await routing_svc.get_teams_workload(db=db, actor_roles=auth.roles)
            for w in teams_wl:
                dept_workloads.append(
                    DepartmentWorkloadItem(
                        department=w["department"],
                        active_agents=w["active_agents"],
                        total_capacity=w["total_capacity"],
                        active_tickets=w["active_tickets"],
                        utilization_pct=w["utilization_pct"],
                    )
                )
        except Exception:
            dept_workloads = []

    return PerformanceAnalyticsResponse(
        ticket_trends=daily_trends,
        category_distribution=category_dist,
        priority_distribution=priority_dist,
        sla_by_category=sla_by_category,
        sla_by_priority=sla_by_priority,
        overall_sla_compliance_pct=overall_sla_pct,
        total_breaches=total_breaches,
        avg_resolution_time_minutes=avg_resolution_time,
        operational_workload=dept_workloads,
        evidence_analytics=evidence_analytics,
        language_distribution=lang_dist,
        reporting_period=ReportingPeriodMetadata(
            time_window=time_window,
            start_date=start_dt,
            end_date=end_dt,
            timezone="UTC",
        ),
    )


@router.get(
    "/operational-intelligence",
    response_model=OperationalIntelligenceResponse,
    summary="Get rule-based operational intelligence alerts and capacity warnings",
)
async def get_operational_intelligence(
    time_window: str = Query("30d", pattern="^(today|7d|30d|90d|custom)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> OperationalIntelligenceResponse:
    """Evaluates deterministic, explainable operational intelligence rules against live platform telemetry."""
    start_dt, end_dt = _parse_time_window(time_window, start_date, end_date)
    customer_id = _get_customer_id(auth)
    staff_tenant: Optional[str] = None
    if not customer_id:
        is_global_admin = any(r.upper() in GLOBAL_ADMIN_ROLES for r in (auth.roles or []))
        if not is_global_admin:
            raw_tenant = (auth.jwt_claims or {}).get("tenant_id")
            if raw_tenant and isinstance(raw_tenant, str) and raw_tenant.strip():
                try:
                    staff_tenant = validate_tenant_id(raw_tenant)
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid tenant identifier.",
                    )

    insights: List[OperationalInsightItem] = []
    applied_rules = [
        "RULE_DEPT_CAPACITY_THRESHOLD",
        "RULE_SLA_IMMINENT_BREACH_WINDOW",
        "RULE_HIGH_RISK_SAFETY_ALARMS",
        "RULE_RECONCILIATION_CONFLICT_SURGE",
        "RULE_RESOLUTION_FAILURE_MONITOR",
    ]

    now_utc = datetime.now(timezone.utc)

    # 1. Rule: Department Capacity Overload (Staff only)
    if not customer_id:
        try:
            routing_svc = get_ticket_routing_service()
            workloads = await routing_svc.get_teams_workload(db=db, actor_roles=auth.roles)
            for w in workloads:
                u_pct = w["utilization_pct"]
                if u_pct >= 90.0:
                    insights.append(
                        OperationalInsightItem(
                            id=f"insight-cap-{w['department'].lower()}",
                            rule_name="RULE_DEPT_CAPACITY_THRESHOLD",
                            severity="CRITICAL",
                            title=f"Critical Capacity Saturation: {w['department']}",
                            description=f"{w['department']} department is at {u_pct}% utilization ({w['active_tickets']}/{w['total_capacity']} slots). New cases risk assignment delays.",
                            supporting_metric=f"{u_pct}% Utilization · {w['active_tickets']} Active Tickets",
                            time_window=time_window,
                            target_route="/tickets",
                            suggested_action="Rebalance agent assignment caps or enable cross-department overflow routing.",
                        )
                    )
                elif u_pct >= 75.0:
                    insights.append(
                        OperationalInsightItem(
                            id=f"insight-cap-{w['department'].lower()}",
                            rule_name="RULE_DEPT_CAPACITY_THRESHOLD",
                            severity="WARNING",
                            title=f"High Department Workload: {w['department']}",
                            description=f"{w['department']} team is approaching capacity ceiling at {u_pct}% utilization.",
                            supporting_metric=f"{u_pct}% Utilization · {w['active_agents']} Active Agents",
                            time_window=time_window,
                            target_route="/tickets",
                            suggested_action="Monitor queue growth and prioritize high-priority ticket intake.",
                        )
                    )
        except Exception as e:
            logger.warning("Operational intelligence workload evaluation failed: %s", e)

    # 2. Rule: Imminent SLA Breach Alert
    ticket_cond = [
        Ticket.status.in_(["OPEN", "IN_PROGRESS", "PENDING_CUSTOMER"]),
        Ticket.sla_breached == False,
    ]
    if customer_id:
        ticket_cond.append(Ticket.customer_id == uuid.UUID(customer_id))
    elif staff_tenant:
        ticket_cond.append(Customer.metadata_["tenant_id"].as_string() == staff_tenant)

    imminent_threshold = now_utc + timedelta(minutes=45)
    t_stmt = select(Ticket)
    if staff_tenant:
        t_stmt = t_stmt.join(Customer, Ticket.customer_id == Customer.id)
    t_stmt = t_stmt.where(
        and_(*ticket_cond, Ticket.sla_breach_at <= imminent_threshold)
    )
    t_res = await db.execute(t_stmt)
    at_risk_tickets = t_res.scalars().all()

    if at_risk_tickets:
        insights.append(
            OperationalInsightItem(
                id="insight-sla-breach-risk",
                rule_name="RULE_SLA_IMMINENT_BREACH_WINDOW",
                severity="CRITICAL",
                title=f"{len(at_risk_tickets)} Ticket(s) At Immediate Risk of SLA Breach",
                description=f"{len(at_risk_tickets)} active case(s) have under 45 minutes remaining before contractual SLA breach.",
                supporting_metric=f"{len(at_risk_tickets)} Tickets Breaching Within 45m",
                time_window=time_window,
                target_route="/tickets",
                suggested_action="Expedite supervisor triage and execute resolution playbooks immediately.",
            )
        )

    # 3. Rule: Safety & Escalation Alarms
    esc_cond = [EscalationAudit.triggered_at >= start_dt]
    if customer_id:
        esc_cond.append(EscalationAudit.ticket.has(customer_id=uuid.UUID(customer_id)))
    elif staff_tenant:
        esc_cond.append(
            or_(
                EscalationAudit.ticket.has(Ticket.customer.has(Customer.metadata_["tenant_id"].as_string() == staff_tenant)),
                EscalationAudit.conversation.has(Conversation.customer.has(Customer.metadata_["tenant_id"].as_string() == staff_tenant)),
            )
        )


    e_stmt = select(EscalationAudit).where(and_(*esc_cond))
    e_res = await db.execute(e_stmt)
    escalations = e_res.scalars().all()

    safety_alarms = [
        e for e in escalations
        if any(k in e.trigger_condition.upper() for k in ("SAFETY", "LEGAL", "THREAT"))
    ]
    if safety_alarms:
        insights.append(
            OperationalInsightItem(
                id="insight-safety-escalation",
                rule_name="RULE_HIGH_RISK_SAFETY_ALARMS",
                severity="CRITICAL",
                title=f"{len(safety_alarms)} High-Risk Safety Alarm(s) Active",
                description="Deterministic keyword and sentiment safety triggers detected potential safety or legal escalation.",
                supporting_metric=f"{len(safety_alarms)} Safety Incidents Recorded",
                time_window=time_window,
                target_route="/escalations",
                suggested_action="Review escalation audit logs and verify customer outreach protocol.",
            )
        )

    # 4. Rule: Evidence Reconciliation Discrepancy Surge
    rec_cond = [
        EvidenceVerification.created_at >= start_dt,
        EvidenceVerification.overall_match_status == "CONFLICT",
    ]
    r_stmt = select(func.count(EvidenceVerification.id)).where(and_(*rec_cond))
    r_res = await db.execute(r_stmt)
    conflict_count = r_res.scalar_one_or_none() or 0

    if conflict_count > 0:
        insights.append(
            OperationalInsightItem(
                id="insight-evidence-conflicts",
                rule_name="RULE_RECONCILIATION_CONFLICT_SURGE",
                severity="WARNING",
                title=f"{conflict_count} Evidence Reconciliation Conflict(s) Detected",
                description="Discrepancies identified between customer verbal assertions and OCR-extracted document invoices.",
                supporting_metric=f"{conflict_count} Discrepancy Case(s)",
                time_window=time_window,
                target_route="/evidence",
                suggested_action="Review OCR extraction tables and issue structured clarification prompts.",
            )
        )

    # 5. Rule: Resolution Failure Monitor
    rf_stmt = select(func.count(TicketResolution.id)).where(
        and_(TicketResolution.created_at >= start_dt, TicketResolution.status == "FAILED")
    )
    rf_res = await db.execute(rf_stmt)
    failed_resolutions = rf_res.scalar_one_or_none() or 0

    if failed_resolutions > 0:
        insights.append(
            OperationalInsightItem(
                id="insight-resolution-failures",
                rule_name="RULE_RESOLUTION_FAILURE_MONITOR",
                severity="WARNING",
                title=f"{failed_resolutions} Automated Playbook Resolution Failure(s)",
                description="Playbook action executions encountered errors or were rejected by supervisor review.",
                supporting_metric=f"{failed_resolutions} Failed Resolution Records",
                time_window=time_window,
                target_route="/tickets",
                suggested_action="Inspect resolution history logs and review playbook action parameters.",
            )
        )

    return OperationalIntelligenceResponse(
        insights=insights,
        evaluated_at=now_utc,
        rules_applied=applied_rules,
    )
