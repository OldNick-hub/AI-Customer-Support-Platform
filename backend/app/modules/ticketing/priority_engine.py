"""Deterministic and Explainable Ticket Priority Engine.

Evaluates support ticket priority using evidence-backed business rules, customer tier
impact, issue category criticality, SLA proximity, and security/outage indicators.

Core Invariants:
1. Category alone (e.g. payment_issue) never establishes a verified financial loss.
2. Financial loss severity requires DOCUMENT_CORROBORATED evidence with verified variance.
3. Untrusted/contradictory evidence suppresses financial severity elevation.
4. Customer tier (e.g. ENTERPRISE) does not automatically imply a critical incident.
5. All calculations are deterministic and output structured, verifiable supporting facts.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger
from app.modules.evidence.schemas import FinancialVerificationLevel

logger = configure_safe_logger(logging.getLogger(__name__))


class PriorityLevel(str, Enum):
    """Authoritative ticket priority levels matching existing Ticket model."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    URGENT = "URGENT"


PRIORITY_ORDER = {
    PriorityLevel.LOW: 1,
    PriorityLevel.MEDIUM: 2,
    PriorityLevel.HIGH: 3,
    PriorityLevel.CRITICAL: 4,
    PriorityLevel.URGENT: 5,
}


@dataclass(frozen=True)
class PriorityEvaluationRequest:
    """Input payload for deterministic ticket priority evaluation."""
    ticket_id: str
    issue_category: str
    customer_tier: str = "STANDARD"  # STANDARD, VIP, ENTERPRISE
    problem_statement: str = ""
    resolution_sought: Optional[str] = None
    has_contradictions: bool = False
    evidence_verification_level: Optional[FinancialVerificationLevel] = None
    has_verified_financial_variance: bool = False
    verified_variance_amount: Optional[float] = None
    verified_currency: Optional[str] = None
    is_account_compromised: bool = False
    is_operational_outage: bool = False
    sla_breached: bool = False
    sla_warning_sent: bool = False
    minutes_to_sla_breach: Optional[float] = None
    customer_sentiment_score: Optional[float] = None  # -1.0 to 1.0


class PriorityEvaluationResult(BaseModel):
    """Explainable priority verdict with verifiable supporting facts."""
    priority: PriorityLevel
    priority_score: float = Field(..., ge=1.0, le=5.0)
    priority_floor: PriorityLevel
    reason: str
    supporting_facts: List[str] = Field(default_factory=list)


class PriorityEngine:
    """Evaluates ticket priority deterministically without hallucinations or arbitrary USD floors."""

    def evaluate(self, req: PriorityEvaluationRequest) -> PriorityEvaluationResult:
        score = 2.0
        floor = PriorityLevel.LOW
        facts: List[str] = []

        category_clean = (req.issue_category or "general").lower().strip()

        # ----------------------------------------------------------------------
        # 1. Base Category Criticality
        # ----------------------------------------------------------------------
        if category_clean == "general":
            score = 1.5
            floor = PriorityLevel.LOW
            facts.append("Category 'general': Base score 1.5 (floor LOW)")
        elif category_clean in ("delivery_issue", "cancellation_return"):
            score = 2.0
            floor = PriorityLevel.LOW
            facts.append(f"Category '{category_clean}': Base score 2.0 (floor LOW)")
        elif category_clean in ("damaged_product", "product_quality"):
            score = 2.5
            floor = PriorityLevel.MEDIUM
            facts.append(f"Category '{category_clean}': Base score 2.5 (floor MEDIUM)")
        elif category_clean in ("payment_issue", "account_issue", "technical_support"):
            score = 2.5
            floor = PriorityLevel.MEDIUM
            facts.append(f"Category '{category_clean}': Base score 2.5 (floor MEDIUM)")
        else:
            score = 2.0
            floor = PriorityLevel.LOW
            facts.append(f"Category '{category_clean}': Default base score 2.0 (floor LOW)")

        # ----------------------------------------------------------------------
        # 2. Customer Tier Impact (Business Impact)
        # ----------------------------------------------------------------------
        tier = (req.customer_tier or "STANDARD").upper().strip()
        if tier == "VIP":
            score += 0.5
            if PRIORITY_ORDER[PriorityLevel.MEDIUM] > PRIORITY_ORDER[floor]:
                floor = PriorityLevel.MEDIUM
            facts.append("Customer Tier 'VIP': +0.5 score boost (floor MEDIUM)")
        elif tier == "ENTERPRISE":
            score += 0.8
            if PRIORITY_ORDER[PriorityLevel.MEDIUM] > PRIORITY_ORDER[floor]:
                floor = PriorityLevel.MEDIUM
            facts.append("Customer Tier 'ENTERPRISE': +0.8 score boost (floor MEDIUM)")
        else:
            facts.append("Customer Tier 'STANDARD': +0.0 score boost")

        # ----------------------------------------------------------------------
        # 3. Financial Evidence & Verification Hierarchy
        # ----------------------------------------------------------------------
        # Safeguard: Never infer financial loss from customer claim text alone
        if req.has_contradictions:
            facts.append("Contradictions Detected in Claims/Evidence: Financial severity elevation suppressed (+0.0)")
        elif (
            req.evidence_verification_level == FinancialVerificationLevel.DOCUMENT_CORROBORATED
            and req.has_verified_financial_variance
        ):
            # Evidence corroborates non-zero financial discrepancy (moderate +0.5 boost, does not independently elevate to CRITICAL)
            score += 0.5
            curr_str = req.verified_currency or "currency"
            amt_str = f"{req.verified_variance_amount:.2f}" if req.verified_variance_amount is not None else "non-zero"
            facts.append(
                f"Financial Evidence Corroborated: Verified discrepancy of {amt_str} {curr_str} (+0.5 score boost)"
            )
        elif category_clean == "payment_issue":
            facts.append("Payment Issue Claim: No authoritative corroboration (+0.0 score, financial elevation withheld)")

        # ----------------------------------------------------------------------
        # 4. Verified Security Incidents & Operational Outages
        # ----------------------------------------------------------------------
        if req.is_account_compromised and category_clean == "account_issue":
            score += 1.5
            if PRIORITY_ORDER[PriorityLevel.CRITICAL] > PRIORITY_ORDER[floor]:
                floor = PriorityLevel.CRITICAL
            facts.append("Verified Security Indicator: Account lockout/compromise detected (+1.5 score boost, floor CRITICAL)")

        if req.is_operational_outage and category_clean == "technical_support":
            score += 1.5
            if PRIORITY_ORDER[PriorityLevel.CRITICAL] > PRIORITY_ORDER[floor]:
                floor = PriorityLevel.CRITICAL
            facts.append("Verified Operational Outage: Incident impacts production service (+1.5 score boost, floor CRITICAL)")

        # ----------------------------------------------------------------------
        # 5. SLA Proximity & Breach State (Time Sensitivity)
        # ----------------------------------------------------------------------
        if req.sla_breached:
            score += 2.0
            floor = PriorityLevel.URGENT
            facts.append("SLA State: SLA already breached (+2.0 score boost, floor URGENT)")
        elif req.minutes_to_sla_breach is not None and req.minutes_to_sla_breach <= 30.0:
            score += 0.8
            facts.append(f"SLA Proximity: <= 30m to breach ({req.minutes_to_sla_breach:.1f}m left) (+0.8 score boost)")
        elif req.sla_warning_sent:
            score += 0.5
            facts.append("SLA Warning: SLA warning threshold reached (+0.5 score boost)")

        # ----------------------------------------------------------------------
        # 6. Customer Sentiment Trajectory
        # ----------------------------------------------------------------------
        if req.customer_sentiment_score is not None and req.customer_sentiment_score <= -0.5:
            score += 0.4
            facts.append(f"Customer Sentiment: High frustration score {req.customer_sentiment_score:.2f} (+0.4 score boost)")

        # ----------------------------------------------------------------------
        # 7. Clamp Score & Map to Priority Level
        # ----------------------------------------------------------------------
        final_score = max(1.0, min(5.0, round(score, 2)))

        if final_score >= 4.5:
            computed_level = PriorityLevel.URGENT
        elif final_score >= 3.8:
            computed_level = PriorityLevel.CRITICAL
        elif final_score >= 2.8:
            computed_level = PriorityLevel.HIGH
        elif final_score >= 1.8:
            computed_level = PriorityLevel.MEDIUM
        else:
            computed_level = PriorityLevel.LOW

        # Enforce priority floor
        if PRIORITY_ORDER[floor] > PRIORITY_ORDER[computed_level]:
            final_priority = floor
            reason = f"Evaluated as {final_priority.value} (elevated from computed score {final_score:.2f} by floor {floor.value})."
        else:
            final_priority = computed_level
            reason = f"Evaluated as {final_priority.value} with priority score {final_score:.2f}."

        return PriorityEvaluationResult(
            priority=final_priority,
            priority_score=final_score,
            priority_floor=floor,
            reason=reason,
            supporting_facts=facts,
        )


_default_priority_engine: Optional[PriorityEngine] = None


def get_priority_engine() -> PriorityEngine:
    global _default_priority_engine
    if _default_priority_engine is None:
        _default_priority_engine = PriorityEngine()
    return _default_priority_engine

