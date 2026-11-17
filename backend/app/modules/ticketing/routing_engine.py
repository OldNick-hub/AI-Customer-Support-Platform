"""Intelligent Ticket Routing Engine and Shared Agent Capacity Locker.

Determines the most appropriate department and agent using category-to-department
mapping, agent skills, real-time workload, and after-hours on-call availability.

Core Invariants:
1. Transaction-safe capacity enforcement: locks the candidate User row with FOR UPDATE.
2. Checks active ticket workload inside the locked transaction.
3. Uses the same shared locking helper for both automatic routing and manual assignment.
4. Never exceeds max_active_tickets through concurrent assignments.
5. Safe fallback to department team queue when all eligible agents are saturated.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import configure_safe_logger
from app.models.auth import Role, User, UserRole
from app.models.ticket import SLACalendar, Ticket
from app.modules.ticketing.after_hours import evaluate_after_hours

logger = configure_safe_logger(logging.getLogger(__name__))

# Canonical category to department mappings with common aliases
CATEGORY_DEPARTMENT_MAP: Dict[str, str] = {
    "payment_issue": "Billing",
    "billing_issue": "Billing",
    "delivery_issue": "Logistics",
    "cancellation_return": "Logistics",
    "technical_support": "Technical",
    "account_issue": "Account",
    "security_incident": "Security",
    "account_access": "Account",
    "damaged_product": "Support",
    "product_quality": "Support",
    "product_defect": "Technical",
    "general": "Support",
    "general_inquiry": "Support",
}

DEPARTMENT_SKILLS_MAP: Dict[str, List[str]] = {
    "Billing": ["billing", "payments", "refunds", "invoices", "charges"],
    "Logistics": ["shipping", "delivery", "tracking", "courier", "returns"],
    "Technical": ["technical", "api", "software", "bugs", "infrastructure"],
    "Account": ["account", "access", "security", "identity", "authentication"],
    "Security": ["security", "authentication", "gdpr", "compliance", "fraud"],
    "Support": ["general", "support", "quality", "product", "hardware"],
}


class RoutingError(Exception):
    """Base exception for routing failures."""


class AgentCapacityExceededError(RoutingError):
    """Raised when an agent is at or over maximum active ticket capacity."""


class AgentNotFoundError(RoutingError):
    """Raised when the requested agent is inactive or does not exist."""


class AgentRoleNotAuthorizedError(RoutingError):
    """Raised when an assigned user does not possess an authorized support role."""


@dataclass(frozen=True)
class AgentCandidate:
    """An agent evaluated for ticket assignment."""
    user_id: uuid.UUID
    email: str
    full_name: str
    department: str
    max_active_tickets: int
    current_active_tickets: int
    is_on_call: bool
    skill_match_score: int


class RoutingDecision(BaseModel):
    """Result of an intelligent routing evaluation."""
    target_department: str
    assigned_agent_id: Optional[str] = None
    assigned_agent_name: Optional[str] = None
    routing_status: str  # "ASSIGNED" or "TEAM_QUEUED"
    reason: str
    candidates_evaluated: int = 0
    candidate_details: List[Dict[str, Any]] = Field(default_factory=list)


async def acquire_agent_capacity_lock(
    db: AsyncSession,
    agent_id: uuid.UUID,
    allow_override: bool = False,
) -> Tuple[User, int]:
    """Acquires a row-level lock on the User and evaluates active capacity.

    This is the SINGLE SHARED HELPER used by both automatic routing and
    manual assignment. Locking the User row serializes all concurrent assignment
    attempts for this agent, preventing capacity race conditions.
    """
    bind = db.bind
    is_sqlite = bind is not None and "sqlite" in bind.dialect.name.lower()

    # 1. Lock the selected User row
    stmt = (
        select(User)
        .where(User.id == agent_id)
        .options(selectinload(User.roles))
    )
    if not is_sqlite:
        stmt = stmt.with_for_update()

    agent = await db.scalar(stmt)
    if not agent or not agent.is_active:
        raise AgentNotFoundError(f"Active agent with ID {agent_id} not found.")

    # 2. Verify authorized role (AGENT, SUPERVISOR, or ADMIN)
    role_names = [r.name.upper() for r in (agent.roles or [])]
    if not any(r in ("AGENT", "SUPERVISOR", "ADMIN") for r in role_names):
        raise AgentRoleNotAuthorizedError(
            f"User {agent.full_name} does not possess an authorized support role (roles: {role_names})."
        )

    # 3. Accurately count current active workload inside the locked transaction
    count_stmt = (
        select(func.count(Ticket.id))
        .where(
            Ticket.assigned_agent_id == agent_id,
            Ticket.status.in_(["OPEN", "IN_PROGRESS", "PENDING_CUSTOMER", "ESCALATED"]),
        )
    )
    active_count = (await db.scalar(count_stmt)) or 0

    # 4. Enforce capacity limit
    if active_count >= agent.max_active_tickets and not allow_override:
        raise AgentCapacityExceededError(
            f"Agent {agent.full_name} ({agent.email}) has reached maximum capacity "
            f"({active_count}/{agent.max_active_tickets} active tickets)."
        )

    return agent, active_count


class RoutingEngine:
    """Evaluates department match, candidate agents, after-hours status, and capacity."""

    def resolve_department(self, issue_category: str) -> str:
        """Maps an issue category to the target department."""
        cat_clean = (issue_category or "general").lower().strip()
        return CATEGORY_DEPARTMENT_MAP.get(cat_clean, "Support")

    async def find_best_agent(
        self,
        db: AsyncSession,
        department: str,
        category: str,
        now: Optional[datetime] = None,
        sla_calendar: Optional[SLACalendar] = None,
    ) -> Tuple[Optional[User], Optional[int], List[AgentCandidate]]:
        """Finds the best available non-saturated agent in the department.

        Locks candidate agents in deterministic order to prevent deadlocks and
        race conditions. Returns (selected_agent, active_count, evaluated_candidates).
        """
        current_time = now or datetime.now(timezone.utc)

        # Check operating hours
        is_after_hours = False
        if sla_calendar:
            ah_eval = evaluate_after_hours(current_time, sla_calendar)
            is_after_hours = ah_eval.is_after_hours

        # 1. Query candidate agents in target department
        dept_stmt = (
            select(User)
            .where(User.department == department, User.is_active == True)
            .options(selectinload(User.roles))
            .order_by(User.id.asc())  # Deterministic order
        )
        res = await db.execute(dept_stmt)
        candidates = res.scalars().all()

        eligible_candidates: List[User] = []
        for u in candidates:
            roles = [r.name.upper() for r in (u.roles or [])]
            if not any(r in ("AGENT", "SUPERVISOR", "ADMIN") for r in roles):
                continue
            if is_after_hours and not u.is_on_call:
                continue
            eligible_candidates.append(u)

        if not eligible_candidates:
            return None, None, []

        # 2. Score candidates by skill match and workload
        scored_candidates: List[Tuple[int, int, User]] = []
        candidate_records: List[AgentCandidate] = []

        relevant_skills = set(DEPARTMENT_SKILLS_MAP.get(department, []))
        if category:
            relevant_skills.add(category.lower())

        # Measure baseline active counts for ranking
        for u in eligible_candidates:
            count_stmt = select(func.count(Ticket.id)).where(
                Ticket.assigned_agent_id == u.id,
                Ticket.status.in_(["OPEN", "IN_PROGRESS", "PENDING_CUSTOMER", "ESCALATED"]),
            )
            act_count = (await db.scalar(count_stmt)) or 0

            user_skills = set(s.lower() for s in (u.skills or []))
            match_score = len(user_skills.intersection(relevant_skills))

            candidate_records.append(
                AgentCandidate(
                    user_id=u.id,
                    email=u.email,
                    full_name=u.full_name,
                    department=u.department,
                    max_active_tickets=u.max_active_tickets,
                    current_active_tickets=act_count,
                    is_on_call=u.is_on_call,
                    skill_match_score=match_score,
                )
            )

            if act_count < u.max_active_tickets:
                # Ranking key: (-skill_match_score, act_count) -> best skills first, lowest workload first
                scored_candidates.append((-match_score, act_count, u))

        scored_candidates.sort(key=lambda item: (item[0], item[1]))

        # 3. Lock the best candidate using shared capacity locker
        for _, _, candidate_user in scored_candidates:
            try:
                locked_agent, active_count = await acquire_agent_capacity_lock(
                    db, candidate_user.id, allow_override=False
                )
                return locked_agent, active_count, candidate_records
            except AgentCapacityExceededError:
                # Concurrent assignment filled this agent's capacity; try next candidate
                continue

        return None, None, candidate_records


_default_routing_engine: Optional[RoutingEngine] = None


def get_routing_engine() -> RoutingEngine:
    global _default_routing_engine
    if _default_routing_engine is None:
        _default_routing_engine = RoutingEngine()
    return _default_routing_engine
