"""Ticket Completeness Evaluator.

Deterministically evaluates whether a synthesized ticket contains sufficient
information to be actionable for the relevant issue category.

Design principles:
- Purely deterministic: no LLM calls, no network requests.
- Category-specific mandatory fields with clear rationale.
- Optional fields noted but never treated as blockers.
- Completeness score is 0.0–1.0, ratio of present mandatory fields.
- Contradictions in the synthesis are always surfaced regardless of score.
- A score of 1.0 means all mandatory fields are present, NOT that the ticket is resolved.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


# ---------------------------------------------------------------------------
# Field definitions per category
# ---------------------------------------------------------------------------

class FieldRequirement(BaseModel):
    """Definition of a single field requirement in the completeness schema."""
    field_name: str
    label: str
    mandatory: bool
    rationale: str


# Category-specific field requirements.
# "mandatory" fields MUST be present for a ticket to reach READY_FOR_REVIEW.
# "optional" fields improve quality but are never blockers.
CATEGORY_REQUIREMENTS: Dict[str, List[FieldRequirement]] = {
    "delivery_issue": [
        FieldRequirement(
            field_name="order_id_or_tracking",
            label="Order ID or Tracking Number",
            mandatory=True,
            rationale="Required to look up the shipment in the logistics system.",
        ),
        FieldRequirement(
            field_name="problem_statement",
            label="Problem Description",
            mandatory=True,
            rationale="Must describe what went wrong with delivery.",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product or Item",
            mandatory=False,
            rationale="Helps identify the specific shipment.",
        ),
        FieldRequirement(
            field_name="resolution_sought",
            label="Desired Resolution",
            mandatory=False,
            rationale="Helps prioritize reship, refund, or investigation.",
        ),
    ],
    "payment_issue": [
        FieldRequirement(
            field_name="order_id_or_invoice",
            label="Order ID or Invoice Number",
            mandatory=True,
            rationale="Required to locate the transaction in the payment system.",
        ),
        FieldRequirement(
            field_name="problem_statement",
            label="Problem Description",
            mandatory=True,
            rationale="Must describe the nature of the payment issue.",
        ),
        FieldRequirement(
            field_name="resolution_sought",
            label="Desired Resolution",
            mandatory=True,
            rationale="Payment issues require explicit customer instructions (refund, reversal, etc.).",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product or Service",
            mandatory=False,
            rationale="Helps confirm payment context.",
        ),
    ],
    "damaged_product": [
        FieldRequirement(
            field_name="problem_statement",
            label="Damage Description",
            mandatory=True,
            rationale="Must describe the damage (broken, cracked, missing parts, etc.).",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product Name",
            mandatory=True,
            rationale="Required to identify the item and check warranty/return policies.",
        ),
        FieldRequirement(
            field_name="order_id_or_invoice",
            label="Order ID or Invoice Number",
            mandatory=False,
            rationale="Helpful for warranty lookup but not strictly required if product is identifiable.",
        ),
        FieldRequirement(
            field_name="evidence",
            label="Supporting Evidence (Photo/Receipt)",
            mandatory=False,
            rationale="Recommended to expedite replacement, but not mandatory.",
        ),
    ],
    "account_issue": [
        FieldRequirement(
            field_name="problem_statement",
            label="Account Problem Description",
            mandatory=True,
            rationale="Must describe the account problem (locked, suspended, login failure, etc.).",
        ),
        FieldRequirement(
            field_name="resolution_sought",
            label="Desired Resolution",
            mandatory=False,
            rationale="Helps direct to account recovery, re-activation, or password reset.",
        ),
    ],
    "product_quality": [
        FieldRequirement(
            field_name="problem_statement",
            label="Quality Issue Description",
            mandatory=True,
            rationale="Must describe the quality defect or mismatch.",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product Name",
            mandatory=True,
            rationale="Required to identify the item.",
        ),
        FieldRequirement(
            field_name="order_id_or_invoice",
            label="Order ID or Invoice Number",
            mandatory=False,
            rationale="Helps look up purchase history.",
        ),
    ],
    "cancellation_return": [
        FieldRequirement(
            field_name="problem_statement",
            label="Cancellation/Return Reason",
            mandatory=True,
            rationale="Must state reason for cancellation or return.",
        ),
        FieldRequirement(
            field_name="order_id_or_invoice",
            label="Order ID or Invoice Number",
            mandatory=True,
            rationale="Required to process the cancellation or return.",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product Name",
            mandatory=False,
            rationale="Helpful for partial order returns.",
        ),
    ],
    "technical_support": [
        FieldRequirement(
            field_name="problem_statement",
            label="Technical Issue Description",
            mandatory=True,
            rationale="Must describe the technical error or malfunction.",
        ),
        FieldRequirement(
            field_name="product_or_service",
            label="Product or Platform",
            mandatory=False,
            rationale="Helps identify which product/platform is affected.",
        ),
        FieldRequirement(
            field_name="actions_already_attempted",
            label="Troubleshooting Already Attempted",
            mandatory=False,
            rationale="Avoids repeating troubleshooting steps.",
        ),
    ],
    "general": [
        FieldRequirement(
            field_name="problem_statement",
            label="Problem Description",
            mandatory=True,
            rationale="Every ticket requires a clear description of the customer's problem.",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CompletenessViolation(BaseModel):
    """A single missing or contradictory field finding."""
    field_name: str
    label: str
    violation_type: str  # 'MISSING_MANDATORY', 'MISSING_OPTIONAL', 'CONTRADICTION'
    rationale: str
    is_blocker: bool


class CompletenessResult(BaseModel):
    """Result of a completeness evaluation for a synthesized ticket."""
    issue_category: str
    completeness_score: float = Field(..., ge=0.0, le=1.0)
    is_complete: bool
    mandatory_fields_total: int
    mandatory_fields_present: int
    violations: List[CompletenessViolation] = Field(default_factory=list)
    missing_mandatory_fields: List[str] = Field(default_factory=list)
    missing_optional_fields: List[str] = Field(default_factory=list)
    contradictions_detected: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    # Fields that the customer already provided — MUST NOT be re-requested
    already_provided_fields: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helper: check field presence
# ---------------------------------------------------------------------------

def _has_identifiers_of_type(
    reference_identifiers: List[Dict[str, str]],
    allowed_types: Set[str],
) -> bool:
    """Returns True if any reference identifier has an allowed type."""
    return any(
        str(ident.get("type", "")).upper() in allowed_types
        for ident in reference_identifiers
    )


def _evaluate_field(
    field_name: str,
    synthesis_data: Dict[str, Any],
    evidence_ids: List[str],
) -> bool:
    """Returns True if the field is considered present in the synthesis."""
    reference_identifiers = synthesis_data.get("reference_identifiers") or []

    if field_name == "problem_statement":
        ps = synthesis_data.get("problem_statement", "")
        return bool(ps and len(str(ps).strip()) >= 10)

    if field_name == "product_or_service":
        return bool(synthesis_data.get("product_or_service"))

    if field_name == "order_id_or_tracking":
        return _has_identifiers_of_type(
            reference_identifiers, {"ORDER_ID", "TRACKING_NUMBER"}
        )

    if field_name == "order_id_or_invoice":
        return _has_identifiers_of_type(
            reference_identifiers, {"ORDER_ID", "INVOICE_NUMBER"}
        )

    if field_name == "resolution_sought":
        return bool(synthesis_data.get("resolution_sought"))

    if field_name == "actions_already_attempted":
        actions = synthesis_data.get("actions_already_attempted") or []
        return bool(actions)

    if field_name == "evidence":
        return bool(evidence_ids)

    return False


# ---------------------------------------------------------------------------
# Completeness Evaluator
# ---------------------------------------------------------------------------

class TicketCompletenessEvaluator:
    """Deterministic completeness evaluator for synthesized ticket data.

    No LLM calls are made. Evaluation is based on category-specific field
    requirements applied to the synthesis result dict.
    """

    def evaluate(
        self,
        synthesis_data: Dict[str, Any],
        evidence_ids: Optional[List[str]] = None,
        category_override: Optional[str] = None,
    ) -> CompletenessResult:
        """Evaluate completeness of a synthesized ticket.

        Args:
            synthesis_data: Dict representation of a SynthesisResult.
            evidence_ids: List of linked evidence file IDs.
            category_override: Override category for evaluation (e.g., from agent).

        Returns:
            CompletenessResult with score, violations, and guidance.
        """
        evidence_ids = evidence_ids or []
        category = category_override or str(
            synthesis_data.get("issue_category", "general")
        ).lower().strip()

        if category not in CATEGORY_REQUIREMENTS:
            category = "general"

        requirements = CATEGORY_REQUIREMENTS[category]
        contradictions = synthesis_data.get("contradictions") or []

        violations: List[CompletenessViolation] = []
        missing_mandatory: List[str] = []
        missing_optional: List[str] = []
        already_provided: List[str] = []
        reasons: List[str] = []

        mandatory_total = sum(1 for r in requirements if r.mandatory)
        mandatory_present = 0

        for req in requirements:
            is_present = _evaluate_field(req.field_name, synthesis_data, evidence_ids)

            if is_present:
                already_provided.append(req.field_name)
                if req.mandatory:
                    mandatory_present += 1
            else:
                if req.mandatory:
                    missing_mandatory.append(req.field_name)
                    violations.append(
                        CompletenessViolation(
                            field_name=req.field_name,
                            label=req.label,
                            violation_type="MISSING_MANDATORY",
                            rationale=req.rationale,
                            is_blocker=True,
                        )
                    )
                    reasons.append(f"Missing mandatory field: {req.label}. {req.rationale}")
                else:
                    missing_optional.append(req.field_name)
                    violations.append(
                        CompletenessViolation(
                            field_name=req.field_name,
                            label=req.label,
                            violation_type="MISSING_OPTIONAL",
                            rationale=req.rationale,
                            is_blocker=False,
                        )
                    )

        # Surface contradictions
        contradiction_descriptions: List[str] = []
        for c in contradictions:
            desc = c.get("description", str(c))
            contradiction_descriptions.append(desc)
            violations.append(
                CompletenessViolation(
                    field_name=c.get("field", "unknown"),
                    label=c.get("field", "unknown").replace("_", " ").title(),
                    violation_type="CONTRADICTION",
                    rationale=desc,
                    is_blocker=False,  # Contradictions inform but don't block ticket flow
                )
            )
            reasons.append(f"Contradiction detected: {desc}")

        # Score
        score = (mandatory_present / mandatory_total) if mandatory_total > 0 else 1.0
        is_complete = score >= 1.0 and not missing_mandatory

        return CompletenessResult(
            issue_category=category,
            completeness_score=round(score, 3),
            is_complete=is_complete,
            mandatory_fields_total=mandatory_total,
            mandatory_fields_present=mandatory_present,
            violations=violations,
            missing_mandatory_fields=missing_mandatory,
            missing_optional_fields=missing_optional,
            contradictions_detected=contradiction_descriptions,
            reasons=reasons,
            already_provided_fields=already_provided,
        )


# Default singleton
_default_evaluator: Optional[TicketCompletenessEvaluator] = None


def get_completeness_evaluator() -> TicketCompletenessEvaluator:
    """Returns the shared TicketCompletenessEvaluator singleton."""
    global _default_evaluator
    if _default_evaluator is None:
        _default_evaluator = TicketCompletenessEvaluator()
    return _default_evaluator

