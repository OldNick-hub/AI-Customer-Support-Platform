"""Ticket Routing and Priority Service.

Coordinates priority evaluation, capacity-safe intelligent routing, manual assignment
overrides, and immutable TicketEvent audit history.

Core Invariants:
1. Strict SLA preservation: SLA targets and breach deadlines are NEVER modified by priority changes.
2. Concurrent capacity safety: locks candidate User rows with FOR UPDATE to prevent capacity overruns.
3. Uses the same shared locking helper for both automatic routing and manual assignment.
4. Strict RBAC enforcement: customers cannot route or re-prioritize tickets directly.
5. Safe fallback to department team queue when all candidate agents are saturated.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import configure_safe_logger
from app.models.auth import Role, User
from app.models.customer import Customer
from app.models.evidence import EvidenceFile
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.models.ticket_synthesis import TicketSynthesis
from app.modules.evidence.schemas import FinancialVerificationLevel
from app.modules.ticketing.on_call import OnCallDispatcher, default_on_call_dispatcher
from app.modules.ticketing.priority_engine import (
    PriorityEngine,
    PriorityEvaluationRequest,
    PriorityEvaluationResult,
    PriorityLevel,
    get_priority_engine,
)
from app.modules.ticketing.routing_engine import (
    AgentCapacityExceededError,
    AgentNotFoundError,
    AgentRoleNotAuthorizedError,
    RoutingDecision,
    RoutingEngine,
    acquire_agent_capacity_lock,
    get_routing_engine,
)

logger = configure_safe_logger(logging.getLogger(__name__))


class TicketRoutingAuthorizationError(Exception):
    """Caller is not authorized to perform the requested routing or priority action."""


class TicketNotFoundError(Exception):
    """Requested ticket was not found."""


class TicketRoutingService:
    """Orchestrates priority scoring, intelligent routing, and audit logging."""

    def __init__(
        self,
        priority_engine: Optional[PriorityEngine] = None,
        routing_engine: Optional[RoutingEngine] = None,
        on_call_dispatcher: Optional[OnCallDispatcher] = None,
    ) -> None:
        self.priority_engine = priority_engine or get_priority_engine()
        self.routing_engine = routing_engine or get_routing_engine()
        self.on_call_dispatcher = on_call_dispatcher or default_on_call_dispatcher

    async def _fetch_ticket_with_context(
        self, db: AsyncSession, ticket_id: uuid.UUID
    ) -> Ticket:
        """Fetches ticket with eager-loaded synthesis, customer, SLA calendar, and evidence."""
        stmt = (
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(
                selectinload(Ticket.ticket_synthesis),
                selectinload(Ticket.customer),
                selectinload(Ticket.sla_calendar),
                selectinload(Ticket.evidence_files),
                selectinload(Ticket.assigned_agent),
            )
        )
        ticket = await db.scalar(stmt)
        if not ticket:
            raise TicketNotFoundError(f"Ticket {ticket_id} not found.")
        return ticket

    def _assert_customer_owns_ticket(
        self, ticket: Ticket, customer_id: Optional[str]
    ) -> None:
        """Enforces customer ownership boundary."""
        if customer_id is None:
            return  # Staff bypass
        if str(ticket.customer_id) != str(customer_id):
            raise TicketRoutingAuthorizationError(
                f"Customer {customer_id} does not own ticket {ticket.id}."
            )

    async def evaluate_priority(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> PriorityEvaluationResult:
        """Evaluates and updates ticket priority.

        STRICT SLA PRESERVATION INVARIANT:
        This method NEVER modifies sla_target_minutes, sla_warning_at,
        sla_breach_at, sla_paused_at, or total_paused_duration_seconds.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        # RBAC Check: Customers cannot directly evaluate or alter internal priority
        if customer_id is not None:
            raise TicketRoutingAuthorizationError(
                "Customer sessions cannot directly evaluate internal ticket priority."
            )

        now = datetime.now(timezone.utc)
        synthesis = ticket.ticket_synthesis
        customer = ticket.customer

        # Check evidence verification
        has_verified_var = False
        var_amount = None
        var_curr = None
        ev_level = FinancialVerificationLevel.EXTRACTED

        for ev in (ticket.evidence_files or []):
            if ev.status == "VERIFIED":
                ev_level = FinancialVerificationLevel.DOCUMENT_CORROBORATED
                has_verified_var = True
                break

        # Check minutes to breach
        minutes_left = None
        if ticket.sla_breach_at:
            breach_tz = ticket.sla_breach_at
            if breach_tz.tzinfo is None:
                breach_tz = breach_tz.replace(tzinfo=timezone.utc)
            delta = (breach_tz - now).total_seconds() / 60.0
            minutes_left = max(0.0, delta)

        req = PriorityEvaluationRequest(
            ticket_id=str(ticket.id),
            issue_category=ticket.category,
            customer_tier=customer.customer_tier if customer else "STANDARD",
            problem_statement=ticket.description,
            resolution_sought=synthesis.resolution_sought if synthesis else None,
            has_contradictions=bool(synthesis and synthesis.contradictions),
            evidence_verification_level=ev_level,
            has_verified_financial_variance=has_verified_var,
            verified_variance_amount=var_amount,
            verified_currency=var_curr,
            sla_breached=ticket.sla_breached,
            sla_warning_sent=ticket.sla_warning_sent,
            minutes_to_sla_breach=minutes_left,
        )

        result = self.priority_engine.evaluate(req)

        # Record audit event
        prev_priority = ticket.priority
        prev_score = ticket.priority_score

        ticket.priority = result.priority.value
        ticket.priority_score = result.priority_score
        ticket.priority_explanation = result.reason
        db.add(ticket)

        audit_event = TicketEvent(
            ticket_id=ticket.id,
            event_type="PRIORITY_EVALUATED",
            event_payload={
                "previous_priority": prev_priority,
                "new_priority": result.priority.value,
                "previous_priority_score": prev_score,
                "new_priority_score": result.priority_score,
                "priority_floor": result.priority_floor.value,
                "reason": result.reason,
                "supporting_facts": result.supporting_facts,
                "actor_id": actor_id,
                "actor_roles": actor_roles,
                "timestamp": now.isoformat(),
            },
        )
        db.add(audit_event)
        await db.flush()

        return result

    async def route_ticket(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> RoutingDecision:
        """Intelligently routes a ticket to the appropriate department and agent.

        Enforces transaction-safe agent capacity locking on the User row.
        Falls back safely to the team queue if all eligible agents are saturated.
        Preserves all SLA timers and timestamps.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        # RBAC: Customers cannot trigger operational routing
        if customer_id is not None:
            raise TicketRoutingAuthorizationError(
                "Customer sessions cannot directly trigger ticket routing."
            )

        now = datetime.now(timezone.utc)

        # Check if ticket is a duplicate whose routing should be suppressed
        if ticket.routing_status == "DUPLICATE_SUPPRESSED":
            target_department = self.routing_engine.resolve_department(ticket.category)
            logger.info("Routing suppressed for duplicate ticket %s (cluster %s).", ticket.ticket_number, ticket.cluster_group_id)
            return RoutingDecision(
                target_department=target_department,
                assigned_agent_id=None,
                assigned_agent_name=None,
                routing_status="DUPLICATE_SUPPRESSED",
                reason=f"Routing suppressed: ticket {ticket.ticket_number} is a duplicate in cluster {ticket.cluster_group_id}.",
            )

        # 1. Resolve department from category
        target_department = self.routing_engine.resolve_department(ticket.category)

        # 2. Find best candidate agent and acquire capacity lock
        agent, active_count, candidates = await self.routing_engine.find_best_agent(
            db=db,
            department=target_department,
            category=ticket.category,
            now=now,
            sla_calendar=ticket.sla_calendar,
        )

        prev_agent_id = str(ticket.assigned_agent_id) if ticket.assigned_agent_id else None
        prev_status = ticket.routing_status

        if agent is not None:
            # Successfully locked and assigned
            ticket.assigned_agent_id = agent.id
            ticket.assigned_team = target_department
            ticket.routing_status = "ASSIGNED"

            reason = (
                f"Assigned to {agent.full_name} ({agent.email}) in department '{target_department}' "
                f"with {active_count}/{agent.max_active_tickets} active tickets."
            )
            decision = RoutingDecision(
                target_department=target_department,
                assigned_agent_id=str(agent.id),
                assigned_agent_name=agent.full_name,
                routing_status="ASSIGNED",
                reason=reason,
                candidates_evaluated=len(candidates),
                candidate_details=[
                    {
                        "user_id": str(c.user_id),
                        "name": c.full_name,
                        "active": c.current_active_tickets,
                        "max": c.max_active_tickets,
                    }
                    for c in candidates
                ],
            )

            event = TicketEvent(
                ticket_id=ticket.id,
                event_type="TICKET_ROUTED",
                event_payload={
                    "previous_agent_id": prev_agent_id,
                    "new_agent_id": str(agent.id),
                    "assigned_team": target_department,
                    "routing_status": "ASSIGNED",
                    "reason": reason,
                    "actor_id": actor_id,
                    "actor_roles": actor_roles,
                    "timestamp": now.isoformat(),
                },
            )
            db.add(event)
        else:
            # Fallback to Team Queue
            ticket.assigned_agent_id = None
            ticket.assigned_team = target_department
            ticket.routing_status = "TEAM_QUEUED"

            reason = (
                f"No eligible non-saturated agent available in '{target_department}'. "
                f"Ticket queued to department queue ({len(candidates)} candidates evaluated)."
            )
            decision = RoutingDecision(
                target_department=target_department,
                assigned_agent_id=None,
                assigned_agent_name=None,
                routing_status="TEAM_QUEUED",
                reason=reason,
                candidates_evaluated=len(candidates),
                candidate_details=[
                    {
                        "user_id": str(c.user_id),
                        "name": c.full_name,
                        "active": c.current_active_tickets,
                        "max": c.max_active_tickets,
                    }
                    for c in candidates
                ],
            )

            event = TicketEvent(
                ticket_id=ticket.id,
                event_type="ROUTING_FALLBACK_TEAM_QUEUE",
                event_payload={
                    "previous_agent_id": prev_agent_id,
                    "new_agent_id": None,
                    "assigned_team": target_department,
                    "routing_status": "TEAM_QUEUED",
                    "reason": reason,
                    "actor_id": actor_id,
                    "actor_roles": actor_roles,
                    "timestamp": now.isoformat(),
                },
            )
            db.add(event)

            # Check after-hours urgent dispatch
            if ticket.priority == "URGENT":
                await self.on_call_dispatcher.dispatch(
                    incident_id=f"INC-{ticket.ticket_number}",
                    priority="URGENT",
                    summary=f"Unassigned URGENT ticket in {target_department}: {ticket.title}",
                    urgency_score=5.0,
                    ticket_id=str(ticket.id),
                )

        db.add(ticket)
        await db.flush()

        return decision

    async def manually_assign_ticket(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        target_agent_id: uuid.UUID,
        reason: str,
        actor_id: str,
        actor_roles: List[str],
        force_override: bool = False,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manually assigns or reassigns a ticket to an agent with RBAC and capacity locking.

        Uses the EXACT SAME acquire_agent_capacity_lock helper as automatic routing.
        Preserves all SLA timers and status.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        # RBAC Check: Customers strictly forbidden
        if customer_id is not None:
            raise TicketRoutingAuthorizationError(
                "Customer sessions cannot manually assign support tickets."
            )

        roles_upper = [r.upper() for r in actor_roles]
        is_admin = "ADMIN" in roles_upper
        is_supervisor = "SUPERVISOR" in roles_upper
        is_agent = "AGENT" in roles_upper

        # Self-assignment vs supervisor override
        if not (is_admin or is_supervisor):
            if is_agent and actor_id != str(target_agent_id):
                raise TicketRoutingAuthorizationError(
                    "Support agents may only assign tickets to themselves."
                )

        if force_override and not (is_admin or is_supervisor):
            raise TicketRoutingAuthorizationError(
                "Only administrators and supervisors may override agent capacity limits."
            )

        # 1. Acquire capacity lock on the target agent
        agent, active_count = await acquire_agent_capacity_lock(
            db=db,
            agent_id=target_agent_id,
            allow_override=force_override,
        )

        now = datetime.now(timezone.utc)
        prev_agent_id = str(ticket.assigned_agent_id) if ticket.assigned_agent_id else None

        ticket.assigned_agent_id = agent.id
        ticket.assigned_team = agent.department
        ticket.routing_status = "ASSIGNED"

        is_reassignment = bool(prev_agent_id and prev_agent_id != str(agent.id))
        if force_override:
            assignment_event_type = "MANUAL_ASSIGNMENT_OVERRIDE"
        elif is_reassignment:
            assignment_event_type = "TICKET_REASSIGNED"
        else:
            assignment_event_type = "TICKET_ASSIGNED"

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type=assignment_event_type,
            event_payload={
                "previous_agent_id": prev_agent_id,
                "new_agent_id": str(agent.id),
                "agent_name": agent.full_name,
                "assigned_team": agent.department,
                "routing_status": "ASSIGNED",
                "reason": reason,
                "is_capacity_override": force_override,
                "active_tickets_at_assignment": active_count,
                "actor_id": actor_id,
                "actor_roles": actor_roles,
                "timestamp": now.isoformat(),
            },
        )
        db.add(event)
        db.add(ticket)
        await db.flush()

        return {
            "ticket_id": str(ticket.id),
            "assigned_agent_id": str(agent.id),
            "assigned_agent_name": agent.full_name,
            "assigned_team": agent.department,
            "routing_status": "ASSIGNED",
            "active_tickets": active_count + 1,
            "max_active_tickets": agent.max_active_tickets,
            "capacity_overridden": force_override,
            "reason": reason,
        }

    async def get_routing_history(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves immutable routing and priority audit trail for a ticket."""
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        stmt = (
            select(TicketEvent)
            .where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.event_type.in_([
                    "PRIORITY_EVALUATED",
                    "TICKET_ROUTED",
                    "TICKET_ASSIGNED",
                    "TICKET_REASSIGNED",
                    "MANUAL_ASSIGNMENT_OVERRIDE",
                    "ROUTING_FALLBACK_TEAM_QUEUE",
                    "ESCALATED",
                ]),
            )
            .order_by(TicketEvent.created_at.asc())
        )
        res = await db.execute(stmt)
        events = res.scalars().all()

        return [
            {
                "event_id": str(e.id),
                "event_type": e.event_type,
                "event_payload": e.event_payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

    async def get_teams_workload(
        self,
        db: AsyncSession,
        actor_roles: List[str],
    ) -> List[Dict[str, Any]]:
        """Computes workload and capacity metrics aggregated by department.

        Staff-only: requires SUPERVISOR or ADMIN role.
        """
        roles_upper = [r.upper() for r in actor_roles]
        if not any(r in ("ADMIN", "SUPERVISOR") for r in roles_upper):
            raise TicketRoutingAuthorizationError(
                "Access to team workload metrics is restricted to supervisors and administrators."
            )

        # 1. Query agents by department
        agent_stmt = (
            select(
                User.department,
                func.count(User.id).label("total_agents"),
                func.sum(User.max_active_tickets).label("total_capacity"),
            )
            .where(User.is_active == True)
            .group_by(User.department)
        )
        agent_res = await db.execute(agent_stmt)
        dept_capacities = {row.department: (row.total_agents, row.total_capacity or 0) for row in agent_res}

        # 2. Query active tickets by assigned team
        ticket_stmt = (
            select(
                Ticket.assigned_team,
                func.count(Ticket.id).label("active_tickets"),
            )
            .where(
                Ticket.status.in_(["OPEN", "IN_PROGRESS", "PENDING_CUSTOMER", "ESCALATED"]),
                Ticket.assigned_team.isnot(None),
            )
            .group_by(Ticket.assigned_team)
        )
        ticket_res = await db.execute(ticket_stmt)
        dept_workloads = {row.assigned_team: row.active_tickets for row in ticket_res}

        # Merge results
        all_depts = set(dept_capacities.keys()).union(set(dept_workloads.keys()))
        output = []
        for dept in sorted(all_depts):
            agents, cap = dept_capacities.get(dept, (0, 0))
            active = dept_workloads.get(dept, 0)
            output.append({
                "department": dept,
                "active_agents": agents,
                "total_agents": agents,
                "total_capacity": cap,
                "active_tickets": active,
                "utilization_pct": round((active / cap * 100), 1) if cap > 0 else 0.0,
            })

        return output


_default_routing_service: Optional[TicketRoutingService] = None


def get_ticket_routing_service() -> TicketRoutingService:
    global _default_routing_service
    if _default_routing_service is None:
        _default_routing_service = TicketRoutingService()
    return _default_routing_service
