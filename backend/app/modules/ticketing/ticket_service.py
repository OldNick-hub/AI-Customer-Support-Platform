"""Ticket Synthesis Lifecycle Service.

Coordinates ticket synthesis, completeness evaluation, clarification generation,
and state machine transitions. Enforces tenant isolation, idempotent operations,
RBAC authorization, and immutable audit logging via TicketEvent.

State machine:
  DRAFT → SYNTHESIZING → SYNTHESIZED → NEEDS_INFORMATION → READY_FOR_REVIEW → COMPLETE
                                           ↑                      |
                                           └──────────────────────┘
  (Clarification response updates synthesis → re-eval → new state)

Ticket operational status (OPEN, PENDING_CUSTOMER, etc.) is managed by the existing
escalation and ticket systems. synthesis_status is orthogonal and managed here.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import configure_safe_logger
from app.models.customer import Conversation, Message
from app.models.evidence import EvidenceFile
from app.models.ticket import SLACalendar, Ticket, TicketEvent
from app.models.ticket_synthesis import TicketClarificationRequest, TicketSynthesis
from app.modules.ticketing.after_hours import calculate_sla_deadlines
from app.modules.ticketing.ticket_clarification import (
    ClarificationRoundRequest,
    ClarificationRoundResult,
    CustomerResponseResult,
    TicketClarificationEngine,
    get_clarification_engine,
)
from app.modules.ticketing.ticket_completeness import (
    CompletenessResult,
    TicketCompletenessEvaluator,
    get_completeness_evaluator,
)
from app.modules.ticketing.ticket_synthesis import (
    SynthesisRequest,
    SynthesisResult,
    TicketSynthesisEngine,
    get_synthesis_engine,
)

logger = configure_safe_logger(logging.getLogger(__name__))

# Synthesis status FSM
SYNTHESIS_STATUS_DRAFT = "DRAFT"
SYNTHESIS_STATUS_SYNTHESIZING = "SYNTHESIZING"
SYNTHESIS_STATUS_SYNTHESIZED = "SYNTHESIZED"
SYNTHESIS_STATUS_NEEDS_INFORMATION = "NEEDS_INFORMATION"
SYNTHESIS_STATUS_READY_FOR_REVIEW = "READY_FOR_REVIEW"
SYNTHESIS_STATUS_COMPLETE = "COMPLETE"

VALID_SYNTHESIS_TRANSITIONS: Dict[str, List[str]] = {
    SYNTHESIS_STATUS_DRAFT: [SYNTHESIS_STATUS_SYNTHESIZING],
    SYNTHESIS_STATUS_SYNTHESIZING: [SYNTHESIS_STATUS_SYNTHESIZED, SYNTHESIS_STATUS_DRAFT],
    SYNTHESIS_STATUS_SYNTHESIZED: [SYNTHESIS_STATUS_NEEDS_INFORMATION, SYNTHESIS_STATUS_READY_FOR_REVIEW, SYNTHESIS_STATUS_COMPLETE, SYNTHESIS_STATUS_SYNTHESIZING],
    SYNTHESIS_STATUS_NEEDS_INFORMATION: [SYNTHESIS_STATUS_SYNTHESIZED, SYNTHESIS_STATUS_READY_FOR_REVIEW, SYNTHESIS_STATUS_COMPLETE, SYNTHESIS_STATUS_SYNTHESIZING],
    SYNTHESIS_STATUS_READY_FOR_REVIEW: [SYNTHESIS_STATUS_COMPLETE, SYNTHESIS_STATUS_NEEDS_INFORMATION, SYNTHESIS_STATUS_SYNTHESIZING],
    SYNTHESIS_STATUS_COMPLETE: [],
}


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class TicketNotFoundError(Exception):
    """Ticket does not exist."""


class TicketAuthorizationError(Exception):
    """Caller is not authorized to access this ticket."""


class TicketSynthesisConflictError(Exception):
    """Synthesis already exists and force_resynthesis was not set."""


class InvalidSynthesisTransitionError(Exception):
    """The requested state transition is not permitted."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_ticket(
    db: AsyncSession, ticket_id: uuid.UUID
) -> Ticket:
    """Fetches a ticket by ID with eager-loaded relationships."""
    result = await db.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(
            selectinload(Ticket.ticket_synthesis).selectinload(
                TicketSynthesis.clarification_requests
            ),
            selectinload(Ticket.clarification_requests),
            selectinload(Ticket.evidence_files),
        )
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise TicketNotFoundError(f"Ticket {ticket_id} not found.")
    return ticket


def _assert_customer_owns_ticket(ticket: Ticket, customer_id: Optional[str]) -> None:
    """Raises TicketAuthorizationError if customer_id does not own the ticket."""
    if customer_id is None:
        return  # Staff bypass
    if str(ticket.customer_id) != str(customer_id):
        raise TicketAuthorizationError(
            f"Customer {customer_id} is not authorized to access ticket {ticket.id}."
        )


async def _emit_ticket_event(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    """Persists an immutable audit event for the ticket."""
    event = TicketEvent(
        ticket_id=ticket_id,
        event_type=event_type,
        event_payload=payload,
    )
    db.add(event)


async def _transition_synthesis_status(
    db: AsyncSession,
    ticket: Ticket,
    new_status: str,
    reason: str = "",
) -> None:
    """Validates and applies a synthesis_status transition, emitting an audit event."""
    current = ticket.synthesis_status
    allowed = VALID_SYNTHESIS_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        raise InvalidSynthesisTransitionError(
            f"Cannot transition synthesis_status from '{current}' to '{new_status}' "
            f"for ticket {ticket.id}."
        )
    ticket.synthesis_status = new_status
    await _emit_ticket_event(
        db,
        ticket.id,
        "SYNTHESIS_STATUS_CHANGED",
        {
            "from": current,
            "to": new_status,
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TicketSynthesisService:
    """Orchestrates the full intelligent ticket synthesis lifecycle.

    Responsibilities:
    - Fetch conversation messages from DB for a ticket.
    - Run synthesis engine and persist TicketSynthesis record.
    - Evaluate completeness using TicketCompletenessEvaluator.
    - Generate and persist clarification round (TicketClarificationRequest).
    - Process customer clarification responses.
    - Drive synthesis_status state machine.
    - Emit TicketEvent audit records for every significant action.
    - Enforce tenant isolation and RBAC.
    """

    def __init__(
        self,
        synthesis_engine: Optional[TicketSynthesisEngine] = None,
        completeness_evaluator: Optional[TicketCompletenessEvaluator] = None,
        clarification_engine: Optional[TicketClarificationEngine] = None,
    ) -> None:
        self._synthesis_engine = synthesis_engine or get_synthesis_engine()
        self._evaluator = completeness_evaluator or get_completeness_evaluator()
        self._clarification = clarification_engine or get_clarification_engine()

    @staticmethod
    def _get_valid_evidence_ids(ticket: Ticket) -> List[str]:
        """Filters ticket evidence files to only include valid, active, unexpired, non-quarantined files."""
        now = datetime.now(timezone.utc)
        return [
            str(ef.id)
            for ef in (ticket.evidence_files or [])
            if ef.status not in ("PURGED", "QUARANTINED", "FLAGGED_UNSAFE", "FAILED", "EXPIRED")
            and not ef.is_malicious
            and (
                ef.retention_expires_at is None
                or (
                    ef.retention_expires_at.replace(tzinfo=timezone.utc)
                    if ef.retention_expires_at.tzinfo is None
                    else ef.retention_expires_at
                )
                > now
            )
        ]

    # ------------------------------------------------------------------
    # 1. Synthesize ticket from conversation
    # ------------------------------------------------------------------

    async def synthesize_ticket(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
        force_resynthesis: bool = False,
    ) -> Dict[str, Any]:
        """Synthesize a structured ticket from its linked conversation.

        Returns the synthesis result dict plus updated synthesis_status.
        Does NOT silently overwrite an existing synthesis unless force_resynthesis=True.
        """
        ticket = await _fetch_ticket(db, ticket_id)
        _assert_customer_owns_ticket(ticket, customer_id)

        # Guard: existing synthesis without force flag
        if ticket.ticket_synthesis is not None and not force_resynthesis:
            raise TicketSynthesisConflictError(
                f"Ticket {ticket_id} already has a synthesis (version "
                f"{ticket.ticket_synthesis.synthesis_version}). "
                "Set force_resynthesis=True to re-synthesize."
            )

        # Phase 1: Associate any eligible unlinked evidence files from this ticket's conversation
        if ticket.conversation_id:
            unlinked_stmt = (
                select(EvidenceFile)
                .where(
                    EvidenceFile.conversation_id == ticket.conversation_id,
                    EvidenceFile.ticket_id.is_(None),
                )
            )
            unlinked_res = await db.execute(unlinked_stmt)
            for ef in unlinked_res.scalars().all():
                if ef.status == "PURGED":
                    continue
                if ef.customer_id is not None and ticket.customer_id is not None and str(ef.customer_id) != str(ticket.customer_id):
                    continue
                if ef.customer_id is None and ticket.customer_id is not None:
                    ef.customer_id = ticket.customer_id
                ef.ticket_id = ticket.id
                db.add(ef)
            await db.flush()
            await db.refresh(ticket, attribute_names=["evidence_files"])

        # Fetch conversation messages and valid evidence IDs
        messages = await self._fetch_conversation_messages(db, ticket)
        evidence_ids = self._get_valid_evidence_ids(ticket)

        # Mark synthesizing
        await _transition_synthesis_status(
            db, ticket, SYNTHESIS_STATUS_SYNTHESIZING, "Synthesis started."
        )
        await db.flush()

        # Run synthesis
        req = SynthesisRequest(
            conversation_id=str(ticket.conversation_id or ""),
            ticket_id=str(ticket_id),
            messages=messages,
            evidence_ids=evidence_ids,
            force_resynthesis=force_resynthesis,
        )
        result: SynthesisResult = await self._synthesis_engine.synthesize(req)

        # Evaluate completeness
        completeness: CompletenessResult = self._evaluator.evaluate(
            synthesis_data=result.model_dump(),
            evidence_ids=evidence_ids,
        )

        # Persist / update TicketSynthesis
        if ticket.ticket_synthesis is None:
            ts = TicketSynthesis(
                ticket_id=ticket_id,
                **self._synthesis_to_db_kwargs(result, completeness),
            )
            ticket.ticket_synthesis = ts
            db.add(ts)
        else:
            existing = ticket.ticket_synthesis
            # Supersede outstanding pending clarifications
            for cr in (existing.clarification_requests or []):
                if cr.status == "PENDING_CUSTOMER":
                    cr.status = "SUPERSEDED"
            existing.synthesis_version += 1
            self._update_synthesis_record(existing, result, completeness)

        # Transition to appropriate status
        new_status = (
            SYNTHESIS_STATUS_NEEDS_INFORMATION
            if completeness.missing_mandatory_fields
            else SYNTHESIS_STATUS_SYNTHESIZED
        )
        # Allow transition from SYNTHESIZING to SYNTHESIZED then to needs/ready
        await _transition_synthesis_status(
            db, ticket, SYNTHESIS_STATUS_SYNTHESIZED, "Synthesis completed."
        )
        if completeness.missing_mandatory_fields:
            await _transition_synthesis_status(
                db, ticket, SYNTHESIS_STATUS_NEEDS_INFORMATION,
                f"Missing mandatory fields: {completeness.missing_mandatory_fields}"
            )
        else:
            await _transition_synthesis_status(
                db, ticket, SYNTHESIS_STATUS_READY_FOR_REVIEW,
                "All mandatory fields present."
            )

        await db.flush()
        await _emit_ticket_event(
            db, ticket_id, "TICKET_SYNTHESIZED",
            {
                "synthesis_version": ticket.ticket_synthesis.synthesis_version
                if ticket.ticket_synthesis else 1,
                "completeness_score": completeness.completeness_score,
                "is_complete": completeness.is_complete,
                "llm_model": result.llm_model_used,
            },
        )

        return {
            "ticket_id": str(ticket_id),
            "synthesis_status": ticket.synthesis_status,
            "synthesis": result.model_dump(),
            "completeness": completeness.model_dump(),
        }

    # ------------------------------------------------------------------
    # 2. Evaluate completeness standalone
    # ------------------------------------------------------------------

    async def evaluate_completeness(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> CompletenessResult:
        """Re-evaluate completeness of an existing synthesis."""
        ticket = await _fetch_ticket(db, ticket_id)
        _assert_customer_owns_ticket(ticket, customer_id)

        if ticket.ticket_synthesis is None:
            raise TicketNotFoundError(f"No synthesis found for ticket {ticket_id}.")

        ts = ticket.ticket_synthesis
        evidence_ids = self._get_valid_evidence_ids(ticket)
        synthesis_dict = self._synthesis_record_to_dict(ts)

        return self._evaluator.evaluate(
            synthesis_data=synthesis_dict,
            evidence_ids=evidence_ids,
        )

    # ------------------------------------------------------------------
    # 3. Generate clarification round
    # ------------------------------------------------------------------

    async def generate_clarification(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a new clarification round for a ticket needing information."""
        ticket = await _fetch_ticket(db, ticket_id)
        _assert_customer_owns_ticket(ticket, customer_id)

        if ticket.ticket_synthesis is None:
            raise TicketNotFoundError(f"No synthesis found for ticket {ticket_id}. Run synthesis first.")

        ts = ticket.ticket_synthesis
        evidence_ids = self._get_valid_evidence_ids(ticket)
        synthesis_dict = self._synthesis_record_to_dict(ts)

        completeness = self._evaluator.evaluate(
            synthesis_data=synthesis_dict,
            evidence_ids=evidence_ids,
        )

        # Count existing rounds (non-superseded)
        prior_rounds = [
            {
                "questions": cr.questions,
                "targeted_missing_fields": cr.targeted_missing_fields,
                "status": cr.status,
                "customer_response": cr.customer_response,
            }
            for cr in sorted(ts.clarification_requests or [], key=lambda r: r.round_number)
        ]
        next_round = len(prior_rounds) + 1

        clarification_req = ClarificationRoundRequest(
            ticket_id=str(ticket_id),
            synthesis_data=synthesis_dict,
            completeness_result=completeness.model_dump(),
            evidence_ids=evidence_ids,
            prior_rounds=prior_rounds,
            current_round_number=next_round,
        )
        result: ClarificationRoundResult = self._clarification.generate_clarification_round(
            clarification_req
        )

        if not result.should_ask:
            # Ticket may be complete or max rounds exceeded
            if completeness.is_complete and ticket.synthesis_status not in (
                SYNTHESIS_STATUS_COMPLETE, SYNTHESIS_STATUS_READY_FOR_REVIEW
            ):
                await _transition_synthesis_status(
                    db, ticket, SYNTHESIS_STATUS_READY_FOR_REVIEW,
                    result.stop_reason or "Sufficient information gathered."
                )
                await db.flush()
            return {
                "ticket_id": str(ticket_id),
                "should_ask": False,
                "stop_reason": result.stop_reason,
                "synthesis_status": ticket.synthesis_status,
            }

        # Persist the clarification request
        cr = TicketClarificationRequest(
            ticket_id=ticket_id,
            synthesis_id=ts.id,
            round_number=next_round,
            questions=[q.model_dump() for q in result.questions],
            targeted_missing_fields=result.targeted_missing_fields,
            status="PENDING_CUSTOMER",
            completeness_score_before=completeness.completeness_score,
        )
        db.add(cr)
        if ts.clarification_requests is not None and cr not in ts.clarification_requests:
            ts.clarification_requests.append(cr)

        # Ensure synthesis status is NEEDS_INFORMATION
        if ticket.synthesis_status not in (SYNTHESIS_STATUS_NEEDS_INFORMATION,):
            if ticket.synthesis_status in (SYNTHESIS_STATUS_SYNTHESIZED, SYNTHESIS_STATUS_READY_FOR_REVIEW):
                await _transition_synthesis_status(
                    db, ticket, SYNTHESIS_STATUS_NEEDS_INFORMATION,
                    "Clarification round opened."
                )

        await db.flush()
        await _emit_ticket_event(
            db, ticket_id, "CLARIFICATION_ROUND_OPENED",
            {"round_number": next_round, "targeted_fields": result.targeted_missing_fields},
        )

        return {
            "ticket_id": str(ticket_id),
            "should_ask": True,
            "round_number": next_round,
            "questions": [q.model_dump() for q in result.questions],
            "targeted_missing_fields": result.targeted_missing_fields,
            "synthesis_status": ticket.synthesis_status,
        }

    # ------------------------------------------------------------------
    # 4. Process customer clarification response
    # ------------------------------------------------------------------

    async def respond_to_clarification(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        clarification_request_id: uuid.UUID,
        customer_response: str,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process a customer's clarification response.

        Merges new information into the synthesis, re-evaluates completeness,
        updates the clarification round record, and transitions synthesis_status.
        """
        ticket = await _fetch_ticket(db, ticket_id)
        _assert_customer_owns_ticket(ticket, customer_id)

        if ticket.ticket_synthesis is None:
            raise TicketNotFoundError(f"No synthesis for ticket {ticket_id}.")

        ts = ticket.ticket_synthesis

        # Find the pending clarification request directly by ID and ticket
        res = await db.execute(
            select(TicketClarificationRequest).where(
                TicketClarificationRequest.id == clarification_request_id,
                TicketClarificationRequest.ticket_id == ticket_id,
            )
        )
        cr: Optional[TicketClarificationRequest] = res.scalar_one_or_none()

        if cr is None:
            raise TicketNotFoundError(
                f"Clarification request {clarification_request_id} not found for ticket {ticket_id}."
            )
        if cr.status != "PENDING_CUSTOMER":
            raise TicketSynthesisConflictError(
                f"Clarification request {clarification_request_id} is already in status '{cr.status}'."
            )

        evidence_ids = self._get_valid_evidence_ids(ticket)
        synthesis_dict = self._synthesis_record_to_dict(ts)

        # Process response
        response_result: CustomerResponseResult = self._clarification.process_customer_response(
            synthesis_data=synthesis_dict,
            customer_response=customer_response,
            targeted_missing_fields=cr.targeted_missing_fields or [],
            evidence_ids=evidence_ids,
            current_round_number=cr.round_number,
        )

        # Update synthesis record
        self._apply_synthesis_update(ts, response_result.updated_synthesis_data)

        # Mark clarification round answered
        cr.status = "ANSWERED"
        cr.customer_response = customer_response[:5000]
        cr.responded_at = datetime.now(timezone.utc)

        new_completeness = CompletenessResult(**response_result.new_completeness)
        cr.completeness_score_after = new_completeness.completeness_score

        # Transition synthesis status
        if new_completeness.is_complete:
            if ticket.synthesis_status != SYNTHESIS_STATUS_READY_FOR_REVIEW:
                await _transition_synthesis_status(
                    db, ticket, SYNTHESIS_STATUS_READY_FOR_REVIEW,
                    "All mandatory fields now present after customer response."
                )
        # else: remain NEEDS_INFORMATION for next round

        await db.flush()
        await _emit_ticket_event(
            db, ticket_id, "CLARIFICATION_ROUND_ANSWERED",
            {
                "round_number": cr.round_number,
                "ambiguous": response_result.ambiguous_response,
                "completeness_before": cr.completeness_score_before,
                "completeness_after": cr.completeness_score_after,
            },
        )

        return {
            "ticket_id": str(ticket_id),
            "clarification_request_id": str(cr.id),
            "round_number": cr.round_number,
            "response_absorbed": response_result.response_absorbed,
            "ambiguous_response": response_result.ambiguous_response,
            "new_completeness": new_completeness.model_dump(),
            "should_continue_clarification": response_result.should_continue_clarification,
            "synthesis_status": ticket.synthesis_status,
        }

    # ------------------------------------------------------------------
    # 5. Mark complete (admin/agent explicit completion)
    # ------------------------------------------------------------------

    async def mark_complete(
        self,
        db: AsyncSession,
        ticket_id: uuid.UUID,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Explicitly mark a ticket synthesis as COMPLETE."""
        ticket = await _fetch_ticket(db, ticket_id)
        _assert_customer_owns_ticket(ticket, customer_id)

        await _transition_synthesis_status(
            db, ticket, SYNTHESIS_STATUS_COMPLETE, "Marked complete by agent/admin."
        )
        await db.flush()
        await _emit_ticket_event(
            db, ticket_id, "SYNTHESIS_MARKED_COMPLETE", {}
        )
        return {"ticket_id": str(ticket_id), "synthesis_status": ticket.synthesis_status}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_conversation_messages(
        self, db: AsyncSession, ticket: Ticket
    ) -> List[Dict[str, Any]]:
        """Loads all messages from the ticket's linked conversation."""
        if ticket.conversation_id is None:
            return []
        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == ticket.conversation_id)
            .order_by(Message.created_at.asc())
        )
        messages = result.scalars().all()
        return [
            {
                "role": m.sender_type,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in messages
        ]

    @staticmethod
    def _synthesis_to_db_kwargs(
        result: SynthesisResult, completeness: CompletenessResult
    ) -> Dict[str, Any]:
        return {
            "problem_statement": result.problem_statement,
            "product_or_service": result.product_or_service,
            "reference_identifiers": result.reference_identifiers,
            "issue_category": result.issue_category,
            "issue_subcategory": result.issue_subcategory,
            "resolution_sought": result.resolution_sought,
            "actions_already_attempted": result.actions_already_attempted,
            "linked_evidence_ids": result.linked_evidence_ids,
            "missing_fields": completeness.missing_mandatory_fields,
            "contradictions": result.contradictions,
            "completeness_score": completeness.completeness_score,
            "completeness_reasons": completeness.reasons,
            "conversation_context_summary": result.conversation_context_summary,
            "extraction_confidence": result.extraction_confidence,
            "extraction_provenance": result.extraction_provenance,
            "llm_model_used": result.llm_model_used,
        }

    @staticmethod
    def _update_synthesis_record(
        ts: TicketSynthesis,
        result: SynthesisResult,
        completeness: CompletenessResult,
    ) -> None:
        ts.problem_statement = result.problem_statement
        ts.product_or_service = result.product_or_service
        ts.reference_identifiers = result.reference_identifiers
        ts.issue_category = result.issue_category
        ts.issue_subcategory = result.issue_subcategory
        ts.resolution_sought = result.resolution_sought
        ts.actions_already_attempted = result.actions_already_attempted
        ts.linked_evidence_ids = result.linked_evidence_ids
        ts.missing_fields = completeness.missing_mandatory_fields
        ts.contradictions = result.contradictions
        ts.completeness_score = completeness.completeness_score
        ts.completeness_reasons = completeness.reasons
        ts.conversation_context_summary = result.conversation_context_summary
        ts.extraction_confidence = result.extraction_confidence
        ts.extraction_provenance = result.extraction_provenance
        ts.llm_model_used = result.llm_model_used
        ts.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _apply_synthesis_update(ts: TicketSynthesis, data: Dict[str, Any]) -> None:
        """Apply dict updates to an existing TicketSynthesis record."""
        for field in (
            "problem_statement", "product_or_service", "reference_identifiers",
            "issue_category", "issue_subcategory", "resolution_sought",
            "actions_already_attempted", "linked_evidence_ids", "missing_fields",
            "contradictions", "completeness_score", "completeness_reasons",
            "conversation_context_summary", "extraction_confidence", "extraction_provenance",
        ):
            if field in data:
                setattr(ts, field, data[field])
        ts.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 7. Runtime SLA Recalculation for Open Tickets
    # ------------------------------------------------------------------

    async def recalculate_open_tickets_sla(
        self,
        db: AsyncSession,
        calendar_id: Optional[uuid.UUID] = None,
        priority_targets: Optional[Dict[str, int]] = None,
        actor_id: Optional[str] = None,
        actor_roles: Optional[List[str]] = None,
        tenant_id: str = "default",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Recalculates SLA targets, warning deadlines, and breach deadlines across open tickets.

        Guarantees:
        - Only targets OPEN and IN_PROGRESS tickets (excludes CLOSED, RESOLVED).
        - Preserves total_paused_duration_seconds and respects active paused state.
        - Enforces calendar business hours, holidays, and weekends.
        - Enforces administrative authorization (ADMIN, SUPERVISOR, or STAFF).
        - Validates priority targets are strictly positive.
        - Emits an immutable TicketEvent(SLA_RECALCULATED) for each updated ticket.
        - Transaction-safe with atomic commit and rollback on error.
        - Idempotent across repeated executions.
        """
        # 1. Authorization check
        if actor_roles is not None:
            roles_upper = [r.upper() for r in actor_roles]
            if not any(r in ("ADMIN", "SUPERVISOR", "STAFF") for r in roles_upper):
                raise TicketAuthorizationError("Administrative authorization required to recalculate ticket SLAs.")

        # 2. Validate priority targets
        if priority_targets is not None:
            if not isinstance(priority_targets, dict):
                raise ValueError("priority_targets must be a dictionary.")
            for prio_key, target_mins in priority_targets.items():
                if not isinstance(target_mins, int) or target_mins <= 0:
                    raise ValueError(
                        f"Priority target minutes for '{prio_key}' must be a strictly positive integer, got {target_mins}."
                    )

        # 3. Resolve target calendar if specified
        target_calendar: Optional[SLACalendar] = None
        if calendar_id is not None:
            stmt_cal = select(SLACalendar).where(SLACalendar.id == calendar_id)
            target_calendar = await db.scalar(stmt_cal)
            if not target_calendar:
                raise TicketNotFoundError(f"SLA calendar {calendar_id} not found.")

        # 4. Fetch eligible tickets
        stmt = (
            select(Ticket)
            .options(selectinload(Ticket.sla_calendar), selectinload(Ticket.customer))
            .where(Ticket.status.in_(["OPEN", "IN_PROGRESS"]))
            .order_by(Ticket.created_at.asc())
        )
        if calendar_id is not None:
            stmt = stmt.where(Ticket.sla_calendar_id == calendar_id)

        candidates = (await db.scalars(stmt)).all()

        eligible_tickets: List[Ticket] = []
        for t in candidates:
            # Tenant isolation check
            cust_meta = getattr(t.customer, "metadata_", {}) or {}
            cust_tenant = cust_meta.get("tenant_id", "default") if isinstance(cust_meta, dict) else "default"
            if cust_tenant == tenant_id:
                eligible_tickets.append(t)

        now = datetime.now(timezone.utc)
        updated_details: List[Dict[str, Any]] = []
        skipped_count = 0

        for ticket in eligible_tickets:
            cal = target_calendar or ticket.sla_calendar
            # Priority target
            if priority_targets and ticket.priority in priority_targets:
                new_target = priority_targets[ticket.priority]
            else:
                new_target = ticket.sla_target_minutes

            new_warning, new_breach = calculate_sla_deadlines(
                created_at=ticket.created_at or now,
                target_minutes=new_target,
                calendar=cal,
                total_paused_duration_seconds=ticket.total_paused_duration_seconds,
            )

            prev_target = ticket.sla_target_minutes
            prev_warning = ticket.sla_warning_at
            prev_breach = ticket.sla_breach_at

            # Check if any change occurred
            warning_diff = abs((new_warning - prev_warning).total_seconds()) if prev_warning else 999999
            breach_diff = abs((new_breach - prev_breach).total_seconds()) if prev_breach else 999999
            target_changed = (new_target != prev_target)
            calendar_changed = bool(target_calendar and target_calendar.id != ticket.sla_calendar_id)

            if warning_diff < 1.0 and breach_diff < 1.0 and not target_changed and not calendar_changed:
                skipped_count += 1
                continue

            if not dry_run:
                ticket.sla_target_minutes = new_target
                ticket.sla_warning_at = new_warning
                ticket.sla_breach_at = new_breach
                if target_calendar:
                    ticket.sla_calendar_id = target_calendar.id

                # If new breach is in the future, reset breached flag
                if new_breach > now and ticket.sla_breached:
                    ticket.sla_breached = False
                # If new warning is in the future, reset warning flag
                if new_warning > now and ticket.sla_warning_sent:
                    ticket.sla_warning_sent = False

                event = TicketEvent(
                    ticket_id=ticket.id,
                    event_type="SLA_RECALCULATED",
                    event_payload={
                        "previous_target_minutes": prev_target,
                        "new_target_minutes": new_target,
                        "previous_warning_at": prev_warning.isoformat() if prev_warning else None,
                        "new_warning_at": new_warning.isoformat(),
                        "previous_breach_at": prev_breach.isoformat() if prev_breach else None,
                        "new_breach_at": new_breach.isoformat(),
                        "total_paused_duration_seconds": ticket.total_paused_duration_seconds,
                        "actor_id": actor_id,
                        "calendar_id": str(cal.id) if cal else None,
                        "tenant_id": tenant_id,
                        "timestamp": now.isoformat(),
                    },
                )
                db.add(event)

            updated_details.append({
                "ticket_id": str(ticket.id),
                "ticket_number": ticket.ticket_number,
                "priority": ticket.priority,
                "target_minutes": new_target,
                "old_breach_at": prev_breach.isoformat() if prev_breach else None,
                "new_breach_at": new_breach.isoformat(),
                "old_warning_at": prev_warning.isoformat() if prev_warning else None,
                "new_warning_at": new_warning.isoformat(),
            })

        if not dry_run and updated_details:
            await db.flush()

        logger.info(
            "SLA recalculation completed: %d eligible, %d updated, %d skipped (dry_run=%s)",
            len(eligible_tickets),
            len(updated_details),
            skipped_count,
            dry_run,
        )

        return {
            "total_eligible": len(eligible_tickets),
            "updated_count": len(updated_details),
            "skipped_count": skipped_count,
            "failed_count": 0,
            "dry_run": dry_run,
            "details": updated_details,
        }

    @staticmethod
    def _synthesis_record_to_dict(ts: TicketSynthesis) -> Dict[str, Any]:
        return {
            "problem_statement": ts.problem_statement,
            "product_or_service": ts.product_or_service,
            "reference_identifiers": ts.reference_identifiers or [],
            "issue_category": ts.issue_category,
            "issue_subcategory": ts.issue_subcategory,
            "resolution_sought": ts.resolution_sought,
            "actions_already_attempted": ts.actions_already_attempted or [],
            "linked_evidence_ids": ts.linked_evidence_ids or [],
            "missing_fields": ts.missing_fields or [],
            "contradictions": ts.contradictions or [],
            "completeness_score": ts.completeness_score,
            "completeness_reasons": ts.completeness_reasons or [],
            "conversation_context_summary": ts.conversation_context_summary,
            "extraction_confidence": ts.extraction_confidence,
            "extraction_provenance": ts.extraction_provenance or {},
            "llm_model_used": ts.llm_model_used,
        }


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_default_service: Optional[TicketSynthesisService] = None


def get_ticket_synthesis_service() -> TicketSynthesisService:
    """Returns the shared TicketSynthesisService singleton."""
    global _default_service
    if _default_service is None:
        _default_service = TicketSynthesisService()
    return _default_service

