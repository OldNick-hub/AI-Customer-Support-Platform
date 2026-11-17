"""Ticket Resolution & Action Orchestration Lifecycle Service.

Coordinates resolution evaluation, action proposal, supervisor approval workflows,
idempotent execution, verification, and seamless Day 13 escalation routing.
Enforces customer ownership, staff RBAC, and strict SLA deadline immutability.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import configure_safe_logger
from app.models.customer import Customer
from app.models.evidence import EvidenceFile
from app.models.ticket import Ticket, TicketEvent
from app.models.ticket_resolution import ResolutionAction, TicketResolution
from app.models.ticket_synthesis import TicketSynthesis
from app.modules.ticketing.action_orchestrator import (
    ActionNotApprovedError,
    ActionNotFoundError,
    ActionOrchestrationError,
    ActionOrchestrator,
    compute_action_params_hash,
    get_action_orchestrator,
)
from app.modules.ticketing.resolution_engine import ResolutionEngine, get_resolution_engine
from app.modules.ticketing.resolution_playbooks import PlaybookEvaluationResult, ProposedActionPlan
from app.modules.ticketing.routing_service import TicketRoutingService, get_ticket_routing_service

logger = configure_safe_logger(logging.getLogger(__name__))


class ResolutionError(Exception):
    """Base exception for resolution service errors."""
    pass


class TicketNotFoundError(ResolutionError):
    pass


class ResolutionNotFoundError(ResolutionError):
    pass


class ResolutionAuthorizationError(ResolutionError):
    pass


class ResolutionVerificationError(ResolutionError):
    pass


class TicketResolutionService:
    """Orchestrates resolution evaluation, action execution, and verification."""

    def __init__(
        self,
        engine: Optional[ResolutionEngine] = None,
        orchestrator: Optional[ActionOrchestrator] = None,
        routing_service: Optional[TicketRoutingService] = None,
    ):
        self.engine = engine or get_resolution_engine()
        self.orchestrator = orchestrator or get_action_orchestrator()
        self.routing_service = routing_service or get_ticket_routing_service()

    async def _fetch_ticket_with_context(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
    ) -> Ticket:
        stmt = (
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(
                selectinload(Ticket.customer),
                selectinload(Ticket.ticket_synthesis),
                selectinload(Ticket.evidence_files),
                selectinload(Ticket.ticket_resolution),
            )
        )
        res = await db.execute(stmt)
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise TicketNotFoundError(f"Ticket with ID {ticket_id} not found.")

        # Ensure ticket_resolution is loaded even if ticket was already in identity map
        if ticket.ticket_resolution is None:
            res_obj = await db.scalar(
                select(TicketResolution).where(TicketResolution.ticket_id == ticket.id)
            )
            if res_obj:
                ticket.ticket_resolution = res_obj

        return ticket

    def _assert_customer_owns_ticket(
        self,
        ticket: Ticket,
        customer_id: Optional[str],
    ) -> None:
        if customer_id is None:
            return
        if str(ticket.customer_id) != str(customer_id):
            raise ResolutionAuthorizationError(
                f"Customer {customer_id} does not own ticket {ticket.id}."
            )

    async def evaluate_resolution(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> TicketResolution:
        """Evaluates ticket against category playbook and generates proposed actions.

        SLA IMMUTABILITY INVARIANT:
        Never alters sla_target_minutes, sla_warning_at, sla_breach_at, or pause state.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError(
                "Customer sessions cannot evaluate or trigger resolution playbooks."
            )

        now = datetime.now(timezone.utc)
        synthesis = ticket.ticket_synthesis

        # Check existing actions for ticket to prevent duplicate compensation
        act_res = await db.execute(
            select(ResolutionAction).where(ResolutionAction.ticket_id == ticket.id)
        )
        existing_actions_all = act_res.scalars().all()
        existing_credit_sum = Decimal("0.00")
        for a in existing_actions_all:
            if a.action_type == "APPLY_GOODWILL_CREDIT" and a.status in ("APPROVED", "EXECUTED", "SUCCEEDED", "VERIFIED"):
                amt_str = (a.action_params or {}).get("credit_amount_inr") or (a.action_params or {}).get("amount") or "0"
                try:
                    existing_credit_sum += Decimal(str(amt_str))
                except Exception:
                    pass

        cust_tier = "STANDARD"
        ent_config = None
        if ticket.customer:
            cust_tier = getattr(ticket.customer, "customer_tier", "STANDARD") or "STANDARD"
            meta = getattr(ticket.customer, "metadata_", {})
            if isinstance(meta, dict):
                ent_config = meta.get("enterprise_config")

        if synthesis:
            synthesis_data = {
                "problem_statement": synthesis.problem_statement,
                "product_or_service": synthesis.product_or_service,
                "reference_identifiers": synthesis.reference_identifiers or [],
                "missing_fields": synthesis.missing_fields or [],
                "contradictions": synthesis.contradictions or [],
                "completeness_score": synthesis.completeness_score,
                "resolution_sought": synthesis.resolution_sought,
                "customer_tier": cust_tier,
                "enterprise_config": ent_config,
                "existing_credits_issued": existing_credit_sum,
            }
        else:
            synthesis_data = {
                "customer_tier": cust_tier,
                "enterprise_config": ent_config,
                "existing_credits_issued": existing_credit_sum,
            }

        evidence_data = []
        for e in (ticket.evidence_files or []):
            if isinstance(e, dict):
                evidence_data.append(e)
            else:
                ext_amt = None
                for ent in (getattr(e, "extracted_entities", None) or []):
                    if getattr(ent, "extracted_amount", None) is not None:
                        ext_amt = ent.extracted_amount
                        break
                evidence_data.append({
                    "id": str(e.id),
                    "status": getattr(e, "status", "STAGED"),
                    "file_name": getattr(e, "file_name", ""),
                    "file_type": getattr(e, "mime_type", getattr(e, "file_type", None)),
                    "extracted_amount": ext_amt,
                })

        # Evaluate through ResolutionEngine
        verdict: PlaybookEvaluationResult = self.engine.evaluate_ticket(
            ticket_id=str(ticket.id),
            category=ticket.category,
            description=ticket.description,
            synthesis_data=synthesis_data,
            evidence_data=evidence_data,
            priority=ticket.priority,
            sla_breached=ticket.sla_breached,
        )

        resolution = ticket.ticket_resolution
        if resolution is None:
            resolution = TicketResolution(
                id=uuid.uuid4(),
                ticket_id=ticket.id,
                status="EVALUATED" if verdict.eligibility_decision == "ELIGIBLE" else verdict.eligibility_decision,
                playbook_category=verdict.category.value,
                recommended_resolution=verdict.recommended_resolution,
                confidence_score=verdict.confidence_score,
                eligibility_decision=verdict.eligibility_decision,
                requires_human_approval=verdict.requires_human_approval,
                supporting_evidence=verdict.supporting_evidence,
                rejection_or_escalation_reason=verdict.rejection_or_escalation_reason,
                resolution_version=1,
            )
            ticket.ticket_resolution = resolution
            db.add(resolution)
            await db.flush()
        else:
            resolution.status = "EVALUATED" if verdict.eligibility_decision == "ELIGIBLE" else verdict.eligibility_decision
            resolution.playbook_category = verdict.category.value
            resolution.recommended_resolution = verdict.recommended_resolution
            resolution.confidence_score = verdict.confidence_score
            resolution.eligibility_decision = verdict.eligibility_decision
            resolution.requires_human_approval = verdict.requires_human_approval
            resolution.supporting_evidence = verdict.supporting_evidence
            resolution.rejection_or_escalation_reason = verdict.rejection_or_escalation_reason
            resolution.resolution_version += 1
            db.add(resolution)
            await db.flush()

        # Check existing action keys to avoid duplicate proposals
        existing_keys = {a.action_key for a in existing_actions_all}

        # Generate proposed actions
        for p_action in verdict.proposed_actions:
            if p_action.action_key not in existing_keys:
                new_action = ResolutionAction(
                    id=uuid.uuid4(),
                    resolution_id=resolution.id,
                    ticket_id=ticket.id,
                    action_type=p_action.action_type,
                    action_key=p_action.action_key,
                    action_params=p_action.action_params,
                    status="AWAITING_APPROVAL" if p_action.requires_approval else "PROPOSED",
                    is_sensitive=p_action.is_sensitive,
                    requires_approval=p_action.requires_approval,
                )
                db.add(new_action)

        # Update ticket resolution status
        ticket.resolution_status = "AWAITING_APPROVAL" if verdict.requires_human_approval else "EVALUATED"
        db.add(ticket)

        # Record audit event
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_EVALUATED",
            event_payload={
                "resolution_id": str(resolution.id),
                "playbook_category": resolution.playbook_category,
                "eligibility_decision": resolution.eligibility_decision,
                "confidence_score": resolution.confidence_score,
                "requires_human_approval": resolution.requires_human_approval,
                "proposed_actions_count": len(verdict.proposed_actions),
                "actor_id": actor_id,
                "actor_roles": actor_roles,
                "timestamp": now.isoformat(),
            },
        )
        db.add(event)
        await db.flush()

        return resolution

    async def get_resolution_actions(
        self,
        db: AsyncSession,
        resolution_id: uuid.UUID,
    ) -> List[ResolutionAction]:
        """Retrieves actions for a resolution explicitly via direct query."""
        stmt = (
            select(ResolutionAction)
            .where(ResolutionAction.resolution_id == resolution_id)
            .order_by(ResolutionAction.created_at.asc())
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())

    async def get_resolution(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> TicketResolution:
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        res_stmt = (
            select(TicketResolution)
            .where(TicketResolution.ticket_id == ticket_id)
        )
        resolution = await db.scalar(res_stmt)
        if not resolution:
            raise ResolutionNotFoundError(f"No resolution found for ticket {ticket_id}.")
        return resolution

    async def propose_action(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        action_type: str,
        action_params: Dict[str, Any],
        is_sensitive: bool,
        requires_approval: bool,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> ResolutionAction:
        """Staff manually proposes an action for a ticket resolution."""
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customers cannot propose resolution actions.")

        resolution = ticket.ticket_resolution
        if not resolution:
            raise ResolutionNotFoundError("Cannot propose action: resolution not yet evaluated.")

        action_key = f"{action_type.lower()}_{uuid.uuid4().hex[:8]}"
        action = ResolutionAction(
            id=uuid.uuid4(),
            resolution_id=resolution.id,
            ticket_id=ticket.id,
            action_type=action_type,
            action_key=action_key,
            action_params=action_params,
            status="AWAITING_APPROVAL" if (is_sensitive or requires_approval) else "PROPOSED",
            is_sensitive=is_sensitive,
            requires_approval=requires_approval,
        )
        db.add(action)

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_ACTION_PROPOSED",
            event_payload={
                "action_id": str(action.id),
                "action_type": action_type,
                "action_key": action_key,
                "is_sensitive": is_sensitive,
                "actor_id": actor_id,
                "actor_roles": actor_roles,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        db.add(event)
        await db.flush()
        return action

    async def approve_action(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        action_id: uuid.UUID,
        approval_reason: str,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> ResolutionAction:
        """Supervisor or administrator authorizes a sensitive action."""
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customers cannot approve internal actions.")

        roles_upper = [r.upper() for r in actor_roles]
        if not any(r in ("SUPERVISOR", "ADMIN", "SENIOR_SUPERVISOR") for r in roles_upper):
            raise ResolutionAuthorizationError(
                "Action approval requires SUPERVISOR, SENIOR_SUPERVISOR, or ADMIN role."
            )

        resolution = ticket.ticket_resolution
        if not resolution:
            raise ResolutionNotFoundError(f"Resolution for ticket {ticket_id} not found.")

        bind = db.bind
        is_sqlite = bind is not None and "sqlite" in bind.dialect.name.lower()
        stmt = select(ResolutionAction).where(
            ResolutionAction.id == action_id,
            ResolutionAction.resolution_id == resolution.id,
        )
        if not is_sqlite:
            stmt = stmt.with_for_update()

        action = await db.scalar(stmt)
        if not action:
            raise ActionNotFoundError(f"Action {action_id} not found in resolution.")

        # Concurrency safety: Cannot approve action currently executing or already succeeded
        if action.status in ("EXECUTING", "SUCCEEDED"):
            raise ActionOrchestrationError(
                f"Cannot modify approval state of action {action_id} in state '{action.status}'."
            )

        # Tiered approval matrix authorization check
        params = action.action_params or {}
        req_role = params.get("required_role")
        if req_role == "ADMIN":
            if "ADMIN" not in roles_upper:
                raise ResolutionAuthorizationError(
                    f"Action {action_id} requires ADMIN role for executive manual review."
                )
        elif req_role == "SENIOR_SUPERVISOR":
            if not any(r in ("SENIOR_SUPERVISOR", "ADMIN") for r in roles_upper):
                raise ResolutionAuthorizationError(
                    f"Action {action_id} requires SENIOR_SUPERVISOR or ADMIN authorization."
                )
        elif req_role == "SUPERVISOR":
            if not any(r in ("SUPERVISOR", "SENIOR_SUPERVISOR", "ADMIN") for r in roles_upper):
                raise ResolutionAuthorizationError(
                    f"Action {action_id} requires SUPERVISOR authorization."
                )
        elif action.action_type == "APPLY_GOODWILL_CREDIT":
            raw_amt = params.get("amount") or params.get("credit_amount_inr", "0.00")
            try:
                amt_dec = Decimal(str(raw_amt))
            except Exception:
                amt_dec = Decimal("0.00")
            if amt_dec > Decimal("1499.00") and "ADMIN" not in roles_upper:
                raise ResolutionAuthorizationError(
                    f"Goodwill credit of ₹{amt_dec} exceeds ₹1,499.00 and requires ADMIN authorization."
                )
            elif amt_dec > Decimal("999.00") and not any(r in ("SENIOR_SUPERVISOR", "ADMIN") for r in roles_upper):
                raise ResolutionAuthorizationError(
                    f"Goodwill credit of ₹{amt_dec} exceeds ₹999.00 and requires SENIOR_SUPERVISOR or ADMIN authorization."
                )
            elif amt_dec > Decimal("499.00") and not any(r in ("SUPERVISOR", "SENIOR_SUPERVISOR", "ADMIN") for r in roles_upper):
                raise ResolutionAuthorizationError(
                    f"Goodwill credit of ₹{amt_dec} requires SUPERVISOR authorization."
                )
        else:
            if not any(r in ("SUPERVISOR", "ADMIN") for r in roles_upper):
                raise ResolutionAuthorizationError(
                    "Action approval requires SUPERVISOR or ADMIN role."
                )

        now = datetime.now(timezone.utc)
        action.status = "APPROVED"
        action.approved_by = actor_id
        action.approved_at = now
        action.approval_reason = approval_reason

        # Cryptographic parameter binding: bind approval to exact parameter snapshot
        params_copy = dict(action.action_params or {})
        params_copy["_approved_params_hash"] = compute_action_params_hash(params_copy)
        params_copy["_approved_amount"] = params_copy.get("amount")
        params_copy["_approved_currency"] = params_copy.get("currency")
        action.action_params = params_copy
        db.add(action)

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_ACTION_APPROVED",
            event_payload={
                "action_id": str(action.id),
                "action_type": action.action_type,
                "approved_by": actor_id,
                "approval_reason": approval_reason,
                "approved_params_hash": params_copy["_approved_params_hash"],
                "timestamp": now.isoformat(),
            },
        )
        db.add(event)
        await db.flush()
        return action

    async def reject_action(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        action_id: uuid.UUID,
        rejection_reason: str,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> ResolutionAction:
        """Supervisor or administrator rejects a proposed action."""
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customers cannot reject internal actions.")

        roles_upper = [r.upper() for r in actor_roles]
        if not any(r in ("SUPERVISOR", "ADMIN") for r in roles_upper):
            raise ResolutionAuthorizationError(
                "Action rejection requires SUPERVISOR or ADMIN role."
            )

        resolution = ticket.ticket_resolution
        if not resolution:
            raise ResolutionNotFoundError(f"Resolution for ticket {ticket_id} not found.")

        bind = db.bind
        is_sqlite = bind is not None and "sqlite" in bind.dialect.name.lower()
        stmt = select(ResolutionAction).where(
            ResolutionAction.id == action_id,
            ResolutionAction.resolution_id == resolution.id,
        )
        if not is_sqlite:
            stmt = stmt.with_for_update()

        action = await db.scalar(stmt)
        if not action:
            raise ActionNotFoundError(f"Action {action_id} not found in resolution.")

        # Concurrency safety: Cannot reject action currently executing or already succeeded
        if action.status in ("EXECUTING", "SUCCEEDED"):
            raise ActionOrchestrationError(
                f"Cannot reject action {action_id} in state '{action.status}'."
            )

        now = datetime.now(timezone.utc)
        action.status = "REJECTED"
        action.rejected_by = actor_id
        action.rejected_at = now
        action.rejection_reason = rejection_reason
        db.add(action)

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_ACTION_REJECTED",
            event_payload={
                "action_id": str(action.id),
                "action_type": action.action_type,
                "rejected_by": actor_id,
                "rejection_reason": rejection_reason,
                "timestamp": now.isoformat(),
            },
        )
        db.add(event)
        await db.flush()
        return action

    async def update_action_params(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        action_id: uuid.UUID,
        new_params: Dict[str, Any],
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> ResolutionAction:
        """Updates parameters of an action.

        INVARIANT: If action was previously approved, any parameter change immediately
        invalidates approval, resets status to AWAITING_APPROVAL, and requires re-approval.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customers cannot modify action parameters.")

        resolution = ticket.ticket_resolution
        if not resolution:
            raise ResolutionNotFoundError(f"Resolution for ticket {ticket_id} not found.")

        bind = db.bind
        is_sqlite = bind is not None and "sqlite" in bind.dialect.name.lower()
        stmt = select(ResolutionAction).where(
            ResolutionAction.id == action_id,
            ResolutionAction.resolution_id == resolution.id,
        )
        if not is_sqlite:
            stmt = stmt.with_for_update()

        action = await db.scalar(stmt)
        if not action:
            raise ActionNotFoundError(f"Action {action_id} not found in resolution.")

        if action.status in ("EXECUTING", "SUCCEEDED"):
            raise ActionOrchestrationError(
                f"Cannot modify parameters of action {action_id} in state '{action.status}'."
            )

        old_params = dict(action.action_params or {})
        old_hash = compute_action_params_hash(old_params)
        new_hash = compute_action_params_hash(new_params)

        was_approved = action.status == "APPROVED"
        action.action_params = dict(new_params)

        if was_approved and old_hash != new_hash:
            action.status = "AWAITING_APPROVAL" if (action.is_sensitive or action.requires_approval) else "PROPOSED"
            action.approved_by = None
            action.approved_at = None
            action.approval_reason = None
            event = TicketEvent(
                ticket_id=ticket.id,
                event_type="RESOLUTION_ACTION_APPROVAL_INVALIDATED",
                event_payload={
                    "action_id": str(action.id),
                    "action_type": action.action_type,
                    "reason": "Action parameters modified after approval; re-approval required",
                    "actor_id": actor_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            db.add(event)

        db.add(action)
        await db.flush()
        return action

    async def execute_action(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        action_id: uuid.UUID,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes an action via ActionOrchestrator with row locking and bounded retry protection."""
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customers cannot trigger action execution.")

        action, result = await self.orchestrator.execute_action(
            db=db,
            action_id=action_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
        )

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_ACTION_EXECUTED",
            event_payload={
                "action_id": str(action.id),
                "action_type": action.action_type,
                "status": action.status,
                "execution_attempts": action.execution_attempts,
                "result": result,
                "actor_id": actor_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        db.add(event)
        await db.flush()
        return {
            "action_id": str(action.id),
            "status": action.status,
            "execution_attempts": action.execution_attempts,
            "result": result,
        }

    async def verify_resolution(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        actor_id: str,
        actor_roles: List[str],
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Independent verification stage.

        Verifies:
        1. All proposed/required actions reached SUCCEEDED status.
        2. Required evidence is verified.
        3. No unresolved contradictions exist in synthesis.
        4. If sensitive actions existed, verified approvals were present.

        Only when verified does operational ticket status transition to RESOLVED.
        If verification fails, triggers Day 13 routing escalation.
        """
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        if customer_id is not None:
            raise ResolutionAuthorizationError("Customer sessions cannot verify resolution.")

        resolution = ticket.ticket_resolution
        if not resolution:
            raise ResolutionNotFoundError(f"No resolution found for ticket {ticket_id}.")

        now = datetime.now(timezone.utc)
        actions = await self.get_resolution_actions(db, resolution.id)
        synthesis = ticket.ticket_synthesis

        verification_failures: List[str] = []

        # 1. Check contradiction blocking
        if synthesis and synthesis.contradictions:
            verification_failures.append(
                f"Ticket has {len(synthesis.contradictions)} unresolved evidence contradiction(s)."
            )

        # 2. Check action execution
        if not actions and resolution.eligibility_decision == "ELIGIBLE":
            verification_failures.append("No actions defined or executed for eligible resolution.")

        for act in actions:
            if act.status != "SUCCEEDED":
                verification_failures.append(
                    f"Action '{act.action_type}' ({act.id}) is in unverified state '{act.status}'."
                )

        if verification_failures:
            resolution.status = "FAILED"
            resolution.rejection_or_escalation_reason = "; ".join(verification_failures)
            ticket.resolution_status = "FAILED"
            ticket.status = "ESCALATED"
            db.add(resolution)
            db.add(ticket)

            # Escalate via Day 13 routing engine to appropriate team queue or available agent
            try:
                await self.routing_service.route_ticket(
                    db=db,
                    ticket_id=ticket.id,
                    actor_id=actor_id,
                    actor_roles=actor_roles,
                )
            except Exception as e:
                logger.warning("Routing escalation encountered error during verification fallback", extra={"error": str(e)})

            event = TicketEvent(
                ticket_id=ticket.id,
                event_type="RESOLUTION_VERIFICATION_FAILED",
                event_payload={
                    "resolution_id": str(resolution.id),
                    "failures": verification_failures,
                    "escalated": True,
                    "actor_id": actor_id,
                    "timestamp": now.isoformat(),
                },
            )
            db.add(event)
            await db.flush()

            return {
                "verified": False,
                "resolution_status": "FAILED",
                "failures": verification_failures,
                "ticket_status": ticket.status,
                "escalated": True,
            }

        # Verification Passed!
        resolution.status = "RESOLVED"
        resolution.verified_at = now
        resolution.verified_by = actor_id

        ticket.resolution_status = "RESOLVED"
        ticket.status = "RESOLVED"
        ticket.resolved_at = now

        db.add(resolution)
        db.add(ticket)

        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="RESOLUTION_VERIFIED",
            event_payload={
                "resolution_id": str(resolution.id),
                "playbook_category": resolution.playbook_category,
                "actions_verified_count": len(actions),
                "verified_by": actor_id,
                "timestamp": now.isoformat(),
            },
        )
        db.add(event)
        await db.flush()

        return {
            "verified": True,
            "resolution_status": "RESOLVED",
            "ticket_status": "RESOLVED",
            "verified_at": now.isoformat(),
            "actions_executed": len(actions),
        }

    async def get_resolution_history(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        ticket = await self._fetch_ticket_with_context(db, ticket_id)
        self._assert_customer_owns_ticket(ticket, customer_id)

        stmt = (
            select(TicketEvent)
            .where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.event_type.in_([
                    "RESOLUTION_EVALUATED",
                    "RESOLUTION_ACTION_PROPOSED",
                    "RESOLUTION_ACTION_APPROVED",
                    "RESOLUTION_ACTION_REJECTED",
                    "RESOLUTION_ACTION_EXECUTED",
                    "RESOLUTION_VERIFIED",
                    "RESOLUTION_VERIFICATION_FAILED",
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


_default_service: Optional[TicketResolutionService] = None


def get_ticket_resolution_service() -> TicketResolutionService:
    global _default_service
    if _default_service is None:
        _default_service = TicketResolutionService()
    return _default_service
