"""Intelligent Ticket Clarification Engine.

Generates targeted, non-redundant clarification questions when mandatory
ticket information is missing. Tracks round history to avoid repeating
questions the customer already answered.

Design principles:
- Only asks for genuinely missing information.
- Never asks for information already present in earlier messages or evidence.
- Deduplicates questions across rounds: re-reads prior responses before generating new questions.
- Prioritizes the most critical missing field first (by mandatory status and field order).
- Supports multilingual responses by providing culturally neutral, clear English questions
  (the LLM translation layer handles language adaptation if needed).
- After each customer response, re-evaluates completeness to decide whether to stop or continue.
- Returns at most MAX_QUESTIONS_PER_ROUND questions per round to avoid overwhelming customers.
- Handles ambiguous or non-responsive answers by keeping the round open (PENDING_CUSTOMER).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger
from app.modules.ticketing.ticket_completeness import (
    CompletenessResult,
    CompletenessViolation,
    TicketCompletenessEvaluator,
    get_completeness_evaluator,
)
from app.modules.ticketing.ticket_synthesis import (
    SynthesisResult,
    TicketSynthesisEngine,
    _heuristic_extract,
    get_synthesis_engine,
)

logger = configure_safe_logger(logging.getLogger(__name__))

MAX_QUESTIONS_PER_ROUND = 3
MAX_CLARIFICATION_ROUNDS = 5


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ClarificationQuestion(BaseModel):
    """A single targeted clarification question for a missing field."""
    field_name: str
    question: str
    priority: int  # 1 = highest priority
    is_mandatory: bool


class ClarificationRoundRequest(BaseModel):
    """Input to generate a new clarification round."""
    ticket_id: str
    synthesis_data: Dict[str, Any]
    completeness_result: Dict[str, Any]  # CompletenessResult dict
    evidence_ids: List[str] = Field(default_factory=list)
    prior_rounds: List[Dict[str, Any]] = Field(default_factory=list)
    current_round_number: int = 1


class ClarificationRoundResult(BaseModel):
    """Output from generating a clarification round."""
    should_ask: bool  # False if sufficient info is already available
    questions: List[ClarificationQuestion] = Field(default_factory=list)
    targeted_missing_fields: List[str] = Field(default_factory=list)
    stop_reason: Optional[str] = None  # Why we stopped asking (if should_ask=False)


class CustomerResponseResult(BaseModel):
    """Result of processing a customer's clarification response."""
    response_absorbed: bool
    updated_synthesis_data: Dict[str, Any]
    new_completeness: Dict[str, Any]
    should_continue_clarification: bool
    next_round_number: int
    ambiguous_response: bool = False


# ---------------------------------------------------------------------------
# Field-to-question mapping
# ---------------------------------------------------------------------------

FIELD_QUESTIONS: Dict[str, str] = {
    "order_id_or_tracking": (
        "Could you please provide your order number or tracking number? "
        "This is usually in your order confirmation email (e.g., ORD-12345 or TRK-98765)."
    ),
    "order_id_or_invoice": (
        "Could you please share your order number or invoice number? "
        "You can usually find this in your purchase confirmation or receipt."
    ),
    "product_or_service": (
        "Which product or service does your issue relate to? "
        "Please include the product name or model number if available."
    ),
    "resolution_sought": (
        "What outcome are you hoping for? For example: a refund, a replacement, "
        "an explanation, or something else?"
    ),
    "actions_already_attempted": (
        "Have you already tried any steps to resolve this issue? "
        "For example, contacting the courier, requesting a return, or restarting the app?"
    ),
    "evidence": (
        "Do you have any supporting documents such as a receipt, invoice, or photo "
        "that you can share? This will help us resolve your case faster."
    ),
    "problem_statement": (
        "Could you describe your issue in a bit more detail? "
        "What exactly happened, and when did it occur?"
    ),
}

FIELD_PRIORITIES: Dict[str, int] = {
    "problem_statement": 1,
    "order_id_or_invoice": 2,
    "order_id_or_tracking": 2,
    "product_or_service": 3,
    "resolution_sought": 4,
    "actions_already_attempted": 5,
    "evidence": 6,
}


def _already_asked_in_prior_rounds(
    field_name: str, prior_rounds: List[Dict[str, Any]]
) -> bool:
    """Returns True if this field was already asked in a prior clarification round."""
    for round_data in prior_rounds:
        questions = round_data.get("questions") or []
        for q in questions:
            if q.get("field_name") == field_name or q.get("field") == field_name:
                return True
    return False


def _customer_answered_in_prior_rounds(
    field_name: str, prior_rounds: List[Dict[str, Any]]
) -> bool:
    """Returns True if the customer provided a non-trivial answer to a prior round
    that targeted this field — indicating the information was supplied even if
    the synthesis didn't pick it up."""
    for round_data in prior_rounds:
        targeted = round_data.get("targeted_missing_fields") or []
        response = round_data.get("customer_response") or ""
        status = round_data.get("status", "")
        # If the field was targeted and customer gave a substantial response
        if field_name in targeted and status == "ANSWERED" and len(str(response).strip()) > 5:
            return True
    return False


# ---------------------------------------------------------------------------
# Clarification Engine
# ---------------------------------------------------------------------------

class TicketClarificationEngine:
    """Generates targeted, deduplication-aware clarification questions.

    Does NOT make LLM calls for question generation — questions are deterministic,
    mapped from field names to clear natural language questions. This avoids
    LLM latency for the common case and guarantees predictable, safe output.

    The synthesis engine is used only when re-evaluating completeness after
    incorporating a customer response.
    """

    def __init__(
        self,
        completeness_evaluator: Optional[TicketCompletenessEvaluator] = None,
    ) -> None:
        self._evaluator = completeness_evaluator or get_completeness_evaluator()

    def generate_clarification_round(
        self, request: ClarificationRoundRequest
    ) -> ClarificationRoundResult:
        """Generate questions for a new clarification round.

        Skips fields that:
        1. Are already present in the synthesis.
        2. Were already asked in prior rounds.
        3. The customer already answered in prior rounds.

        Returns should_ask=False when:
        - Ticket is already complete (no missing mandatory fields).
        - Maximum rounds exceeded.
        - All missing fields were already asked and not answered (no new questions possible).
        """
        # Check round limit
        if request.current_round_number > MAX_CLARIFICATION_ROUNDS:
            return ClarificationRoundResult(
                should_ask=False,
                stop_reason=(
                    f"Maximum clarification rounds ({MAX_CLARIFICATION_ROUNDS}) reached. "
                    "Ticket will proceed with available information."
                ),
            )

        # Load completeness
        from app.modules.ticketing.ticket_completeness import CompletenessResult
        completeness = CompletenessResult(**request.completeness_result)

        # If already complete — no need to ask
        if completeness.is_complete:
            return ClarificationRoundResult(
                should_ask=False,
                stop_reason="Ticket already has sufficient information for all mandatory fields.",
            )

        missing_mandatory = completeness.missing_mandatory_fields
        if not missing_mandatory:
            return ClarificationRoundResult(
                should_ask=False,
                stop_reason="All mandatory fields are present. Ticket is ready for review.",
            )

        # Build questions only for genuinely new missing fields
        questions: List[ClarificationQuestion] = []
        targeted_fields: List[str] = []

        for field in missing_mandatory:
            # Skip if already asked
            if _already_asked_in_prior_rounds(field, request.prior_rounds):
                # Skip also if customer already answered (absorbed into synthesis)
                continue

            # Skip if customer already answered (even if synthesis didn't capture it)
            if _customer_answered_in_prior_rounds(field, request.prior_rounds):
                continue

            question_text = FIELD_QUESTIONS.get(
                field,
                f"Could you please provide more information about '{field.replace('_', ' ')}'?"
            )
            priority = FIELD_PRIORITIES.get(field, 99)
            is_mandatory = field in missing_mandatory

            questions.append(
                ClarificationQuestion(
                    field_name=field,
                    question=question_text,
                    priority=priority,
                    is_mandatory=is_mandatory,
                )
            )
            targeted_fields.append(field)

        # Sort by priority
        questions.sort(key=lambda q: q.priority)

        # Limit per round
        questions = questions[:MAX_QUESTIONS_PER_ROUND]
        targeted_fields = [q.field_name for q in questions]

        if not questions:
            return ClarificationRoundResult(
                should_ask=False,
                stop_reason=(
                    "All missing mandatory fields were already asked in prior rounds. "
                    "Ticket will be reviewed with available information."
                ),
            )

        return ClarificationRoundResult(
            should_ask=True,
            questions=questions,
            targeted_missing_fields=targeted_fields,
        )

    def process_customer_response(
        self,
        synthesis_data: Dict[str, Any],
        customer_response: str,
        targeted_missing_fields: List[str],
        evidence_ids: Optional[List[str]] = None,
        current_round_number: int = 1,
    ) -> CustomerResponseResult:
        """Incorporate a customer's clarification response into the synthesis.

        Uses heuristic extraction to merge new information from the response
        into the existing synthesis data, then re-evaluates completeness.
        """
        evidence_ids = evidence_ids or []
        response_stripped = customer_response.strip()

        # Detect ambiguous response (very short / non-informative)
        ambiguous = len(response_stripped) < 5 or response_stripped.lower() in {
            ".", "n/a", "na", "none", "no", "idk", "i don't know", "unknown",
        }

        if ambiguous:
            logger.info(
                "Customer clarification response appears ambiguous (len=%d): proceeding without update.",
                len(response_stripped),
            )
            # Re-evaluate as-is
            completeness = self._evaluator.evaluate(
                synthesis_data=synthesis_data,
                evidence_ids=evidence_ids,
            )
            return CustomerResponseResult(
                response_absorbed=False,
                updated_synthesis_data=synthesis_data,
                new_completeness=completeness.model_dump(),
                should_continue_clarification=not completeness.is_complete,
                next_round_number=current_round_number + 1,
                ambiguous_response=True,
            )

        # Merge response into synthesis: run heuristic extraction on the response text
        # and merge extracted identifiers, product, etc. into synthesis_data
        response_messages = [{"role": "CUSTOMER", "content": response_stripped}]
        extracted = _heuristic_extract(response_messages, evidence_ids=[])

        updated = dict(synthesis_data)

        # Merge reference identifiers (avoid duplicates)
        existing_ids: List[Dict[str, str]] = updated.get("reference_identifiers") or []
        existing_values = {f"{i.get('type')}:{i.get('value')}" for i in existing_ids}
        for ident in extracted.reference_identifiers:
            key = f"{ident.get('type')}:{ident.get('value')}"
            if key not in existing_values:
                existing_ids.append(ident)
                existing_values.add(key)
        updated["reference_identifiers"] = existing_ids

        # Merge product if missing
        if not updated.get("product_or_service") and extracted.product_or_service:
            updated["product_or_service"] = extracted.product_or_service
            prov = dict(updated.get("extraction_provenance") or {})
            prov["product_or_service"] = "explicit"
            updated["extraction_provenance"] = prov

        # If resolution_sought was targeted and response is substantive, absorb it
        if "resolution_sought" in targeted_missing_fields and not updated.get("resolution_sought"):
            # Simple heuristic: use the customer's response as the resolution context
            updated["resolution_sought"] = response_stripped[:500]
            prov = dict(updated.get("extraction_provenance") or {})
            prov["resolution_sought"] = "explicit"
            updated["extraction_provenance"] = prov

        # Remove absorbed fields from missing_fields
        new_missing = []
        for field in updated.get("missing_fields") or []:
            still_missing = not _evaluate_field_simple(field, updated, evidence_ids)
            if still_missing:
                new_missing.append(field)
        updated["missing_fields"] = new_missing

        # Re-evaluate completeness
        completeness = self._evaluator.evaluate(
            synthesis_data=updated,
            evidence_ids=evidence_ids,
        )
        updated["completeness_score"] = completeness.completeness_score
        updated["missing_fields"] = completeness.missing_mandatory_fields
        updated["completeness_reasons"] = completeness.reasons

        should_continue = not completeness.is_complete and current_round_number < MAX_CLARIFICATION_ROUNDS

        return CustomerResponseResult(
            response_absorbed=True,
            updated_synthesis_data=updated,
            new_completeness=completeness.model_dump(),
            should_continue_clarification=should_continue,
            next_round_number=current_round_number + 1,
            ambiguous_response=False,
        )


def _evaluate_field_simple(
    field_name: str, synthesis_data: Dict[str, Any], evidence_ids: List[str]
) -> bool:
    """Simple field presence check (mirrors ticket_completeness._evaluate_field)."""
    from app.modules.ticketing.ticket_completeness import _evaluate_field
    return _evaluate_field(field_name, synthesis_data, evidence_ids)


# Default singleton
_default_clarification_engine: Optional[TicketClarificationEngine] = None


def get_clarification_engine() -> TicketClarificationEngine:
    """Returns the shared TicketClarificationEngine singleton."""
    global _default_clarification_engine
    if _default_clarification_engine is None:
        _default_clarification_engine = TicketClarificationEngine()
    return _default_clarification_engine

