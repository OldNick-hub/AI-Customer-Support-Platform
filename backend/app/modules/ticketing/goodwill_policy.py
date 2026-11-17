"""Smart INR Goodwill Credit Policy Engine.

India-first financial compensation policy for customer support resolutions.
Calculates tier-based, order-value-capped goodwill credits with strict financial
safeguards, Decimal precision, anti-duplication enforcement, and a 4-tier approval matrix.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import logging
from typing import Any, Dict, List, Optional, Union

from app.core.config import settings
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))

# Precision constant
CENT = Decimal("0.01")


@dataclass(frozen=True)
class GoodwillCreditCalculationResult:
    """Immutable result of a goodwill credit policy evaluation."""

    eligible: bool
    amount: Decimal
    currency: str = "INR"
    customer_tier: str = "STANDARD"
    verified_order_value: Optional[Decimal] = None
    order_value_cap: Optional[Decimal] = None
    tier_max: Decimal = Decimal("0.00")
    tier_default: Decimal = Decimal("0.00")
    existing_credits_deducted: Decimal = Decimal("0.00")
    requires_approval: bool = False
    required_role: str = "AGENT"
    approval_tier: str = "AUTOMATED_OR_AGENT"
    reason: str = ""
    policy_rules_applied: List[str] = field(default_factory=list)

    def to_action_params(self) -> Dict[str, Any]:
        """Converts result into structured action parameters with Decimal amounts stored as exact strings."""
        return {
            "credit_amount_inr": str(self.amount.quantize(CENT, rounding=ROUND_HALF_UP)),
            "amount": str(self.amount.quantize(CENT, rounding=ROUND_HALF_UP)),
            "currency": self.currency,
            "customer_tier": self.customer_tier,
            "verified_order_value": str(self.verified_order_value.quantize(CENT, rounding=ROUND_HALF_UP)) if self.verified_order_value is not None else None,
            "order_value_cap": str(self.order_value_cap.quantize(CENT, rounding=ROUND_HALF_UP)) if self.order_value_cap is not None else None,
            "tier_max": str(self.tier_max.quantize(CENT, rounding=ROUND_HALF_UP)),
            "tier_default": str(self.tier_default.quantize(CENT, rounding=ROUND_HALF_UP)),
            "existing_credits_deducted": str(self.existing_credits_deducted.quantize(CENT, rounding=ROUND_HALF_UP)),
            "requires_approval": self.requires_approval,
            "required_role": self.required_role,
            "approval_tier": self.approval_tier,
            "reason": self.reason,
            "policy_rules_applied": list(self.policy_rules_applied),
        }


class GoodwillCreditPolicy:
    """India-first Smart Goodwill Credit Policy Evaluator."""

    def __init__(self) -> None:
        self.currency: str = "INR"

    def _to_decimal(self, val: Any) -> Optional[Decimal]:
        if val is None:
            return None
        if isinstance(val, Decimal):
            return val
        try:
            return Decimal(str(val).strip())
        except (InvalidOperation, ValueError, TypeError):
            return None

    def calculate_credit(
        self,
        customer_tier: str,
        verified_order_value: Optional[Union[Decimal, float, str]],
        delay_severity: str = "MODERATE",
        sla_breached: bool = False,
        existing_credits_issued: Optional[Union[Decimal, float, str]] = None,
        enterprise_config: Optional[Dict[str, Any]] = None,
        allow_top_up: bool = False,
    ) -> GoodwillCreditCalculationResult:
        """Calculates tier-based, order-value-capped goodwill credit in INR.

        Policy Invariants:
        1. Store amounts using Decimal, never float.
        2. Strict ISO-4217 INR currency.
        3. Compensation must not exceed 10% of verified affected order value.
        4. Do not calculate if affected order value is unknown or unverified.
        5. The customer's tier maximum remains an absolute cap.
        6. Enterprise tier requires explicit configuration; never assume unlimited compensation.
        7. Prevent duplicate compensation for the same incident/order.
        8. 4-tier configurable approval matrix:
           - ₹0–₹499: Automated recommendation eligible (Agent role)
           - ₹500–₹999: Supervisor approval required
           - ₹1,000–₹1,499: Senior supervisor approval required
           - Above ₹1,499: Manual review required (Admin role, no automatic execution)
        """
        rules: List[str] = []
        tier_norm = (customer_tier or "STANDARD").upper().strip()

        # 1. Affected Order Value Verification
        order_val_dec = self._to_decimal(verified_order_value)
        if order_val_dec is None or order_val_dec <= Decimal("0.00"):
            return GoodwillCreditCalculationResult(
                eligible=False,
                amount=Decimal("0.00"),
                currency=self.currency,
                customer_tier=tier_norm,
                verified_order_value=None,
                reason="Affected order value is unknown or unverified. Goodwill compensation cannot be calculated.",
                policy_rules_applied=["ORDER_VALUE_UNVERIFIED_BLOCK"],
            )

        order_val = order_val_dec.quantize(CENT, rounding=ROUND_HALF_UP)
        rules.append(f"Verified affected order value: ₹{order_val}")

        # 2. Existing Compensation & Duplicate Incident Check
        prev_credits = self._to_decimal(existing_credits_issued) or Decimal("0.00")
        prev_credits = prev_credits.quantize(CENT, rounding=ROUND_HALF_UP)
        if prev_credits > Decimal("0.00") and not allow_top_up:
            return GoodwillCreditCalculationResult(
                eligible=False,
                amount=Decimal("0.00"),
                currency=self.currency,
                customer_tier=tier_norm,
                verified_order_value=order_val,
                existing_credits_deducted=prev_credits,
                reason=f"Previous goodwill compensation of ₹{prev_credits} already issued for this incident. Duplicate compensation prohibited.",
                policy_rules_applied=["DUPLICATE_COMPENSATION_PROHIBITED"],
            )

        # 3. Tier Policy Parameters
        if tier_norm == "STANDARD":
            tier_default = Decimal(str(getattr(settings, "GOODWILL_CREDIT_STANDARD_DEFAULT", "499.00")))
            tier_max = Decimal(str(getattr(settings, "GOODWILL_CREDIT_STANDARD_MAX", "999.00")))
            rules.append(f"STANDARD tier applied: default ₹{tier_default}, max cap ₹{tier_max}")
        elif tier_norm == "VIP":
            tier_default = Decimal(str(getattr(settings, "GOODWILL_CREDIT_VIP_DEFAULT", "799.00")))
            tier_max = Decimal(str(getattr(settings, "GOODWILL_CREDIT_VIP_MAX", "1499.00")))
            rules.append(f"VIP tier applied: default ₹{tier_default}, max cap ₹{tier_max}")
        elif tier_norm == "ENTERPRISE":
            # Configurable organization-specific policy; never assume unlimited compensation
            ent_conf = enterprise_config or {}
            raw_ent_default = ent_conf.get("default_credit") or getattr(settings, "GOODWILL_CREDIT_ENTERPRISE_DEFAULT", "999.00")
            raw_ent_max = ent_conf.get("max_credit") or getattr(settings, "GOODWILL_CREDIT_ENTERPRISE_MAX", "2499.00")
            tier_default = Decimal(str(raw_ent_default)).quantize(CENT, rounding=ROUND_HALF_UP)
            tier_max = Decimal(str(raw_ent_max)).quantize(CENT, rounding=ROUND_HALF_UP)
            # Enterprise safety invariant: never unlimited
            if tier_max <= Decimal("0.00") or tier_max > Decimal("10000.00"):
                tier_max = Decimal("2499.00")
            rules.append(f"ENTERPRISE organization policy applied: default ₹{tier_default}, strict max cap ₹{tier_max}")
        else:
            tier_norm = "STANDARD"
            tier_default = Decimal(str(getattr(settings, "GOODWILL_CREDIT_STANDARD_DEFAULT", "499.00")))
            tier_max = Decimal(str(getattr(settings, "GOODWILL_CREDIT_STANDARD_MAX", "999.00")))
            rules.append(f"Default STANDARD tier applied: default ₹{tier_default}, max cap ₹{tier_max}")

        tier_default = tier_default.quantize(CENT, rounding=ROUND_HALF_UP)
        tier_max = tier_max.quantize(CENT, rounding=ROUND_HALF_UP)

        # 4. Severity & SLA Adjustment
        is_severe = delay_severity.upper() in ("SEVERE", "CRITICAL") or sla_breached
        if is_severe:
            # Severe delay scales towards tier maximum
            proposed_base = min(tier_max, (tier_default * Decimal("1.50")).quantize(CENT, rounding=ROUND_HALF_UP))
            rules.append(f"Severe delay/SLA breach detected; scaled base to ₹{proposed_base}")
        else:
            proposed_base = tier_default
            rules.append(f"Standard delay severity; baseline credit ₹{proposed_base}")

        # 5. Order Value Cap (Max 10% of verified order value)
        cap_percent = Decimal(str(getattr(settings, "GOODWILL_CREDIT_ORDER_VALUE_CAP_PERCENT", "10.00"))) / Decimal("100.00")
        order_cap = (order_val * cap_percent).quantize(CENT, rounding=ROUND_HALF_UP)
        rules.append(f"10% order-value ceiling: ₹{order_cap} (10% of ₹{order_val})")

        # 6. Apply Compound Caps
        capped_amount = min(proposed_base, order_cap, tier_max)
        rules.append(f"Applied minimum of proposed (₹{proposed_base}), order cap (₹{order_cap}), tier max (₹{tier_max}) -> ₹{capped_amount}")

        # Deduct any previous credits if top-up mode enabled
        final_amount = max(Decimal("0.00"), capped_amount - prev_credits)
        if prev_credits > Decimal("0.00"):
            rules.append(f"Deducted previous credits of ₹{prev_credits}; net credit: ₹{final_amount}")

        if final_amount <= Decimal("0.00"):
            return GoodwillCreditCalculationResult(
                eligible=False,
                amount=Decimal("0.00"),
                currency=self.currency,
                customer_tier=tier_norm,
                verified_order_value=order_val,
                order_value_cap=order_cap,
                tier_max=tier_max,
                tier_default=tier_default,
                existing_credits_deducted=prev_credits,
                reason="Order value 10% ceiling or tier maximum already reached by previous compensation.",
                policy_rules_applied=rules,
            )

        # 7. Approval Matrix Evaluation
        auto_thresh = Decimal(str(getattr(settings, "GOODWILL_AUTO_RECOMMEND_THRESHOLD", "499.00"))).quantize(CENT)
        sup_thresh = Decimal(str(getattr(settings, "GOODWILL_SUPERVISOR_THRESHOLD", "999.00"))).quantize(CENT)
        sr_sup_thresh = Decimal(str(getattr(settings, "GOODWILL_SENIOR_SUPERVISOR_THRESHOLD", "1499.00"))).quantize(CENT)

        if final_amount <= auto_thresh:
            requires_approval = False
            required_role = "AGENT"
            approval_tier = "AUTOMATED_OR_AGENT"
            rules.append(f"Amount ₹{final_amount} <= ₹{auto_thresh}: automated recommendation eligible without supervisor signoff.")
        elif final_amount <= sup_thresh:
            requires_approval = True
            required_role = "SUPERVISOR"
            approval_tier = "SUPERVISOR"
            rules.append(f"Amount ₹{final_amount} in range (₹{auto_thresh}, ₹{sup_thresh}]: requires SUPERVISOR approval.")
        elif final_amount <= sr_sup_thresh:
            requires_approval = True
            required_role = "SENIOR_SUPERVISOR"
            approval_tier = "SENIOR_SUPERVISOR"
            rules.append(f"Amount ₹{final_amount} in range (₹{sup_thresh}, ₹{sr_sup_thresh}]: requires SENIOR_SUPERVISOR approval.")
        else:
            requires_approval = True
            required_role = "ADMIN"
            approval_tier = "MANUAL_REVIEW_REQUIRED"
            rules.append(f"Amount ₹{final_amount} > ₹{sr_sup_thresh}: executive manual review required; automated execution prohibited.")

        return GoodwillCreditCalculationResult(
            eligible=True,
            amount=final_amount,
            currency=self.currency,
            customer_tier=tier_norm,
            verified_order_value=order_val,
            order_value_cap=order_cap,
            tier_max=tier_max,
            tier_default=tier_default,
            existing_credits_deducted=prev_credits,
            requires_approval=requires_approval,
            required_role=required_role,
            approval_tier=approval_tier,
            reason=f"Recommended goodwill credit of ₹{final_amount} ({approval_tier}) for {tier_norm} customer.",
            policy_rules_applied=rules,
        )


_default_goodwill_policy: Optional[GoodwillCreditPolicy] = None


def get_goodwill_credit_policy() -> GoodwillCreditPolicy:
    """Returns singleton instance of GoodwillCreditPolicy."""
    global _default_goodwill_policy
    if _default_goodwill_policy is None:
        _default_goodwill_policy = GoodwillCreditPolicy()
    return _default_goodwill_policy

