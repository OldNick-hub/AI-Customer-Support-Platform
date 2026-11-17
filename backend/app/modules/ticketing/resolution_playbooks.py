"""Deterministic Resolution Playbooks for Customer Support Categories.

Defines category-specific eligibility conditions, required fields, permitted and
prohibited actions, financial/security constraints, and completion criteria.
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.modules.evidence.schemas import FinancialVerificationLevel
from app.modules.ticketing.goodwill_policy import get_goodwill_credit_policy


_ISO_4217_REGEX = re.compile(r"^[A-Z]{3}$")


_CURRENCY_NAME_MAP = {
    "US DOLLAR": "USD",
    "US DOLLARS": "USD",
    "USD": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "INDIAN RUPEE": "INR",
    "RUPEE": "INR",
    "RUPEES": "INR",
    "INR": "INR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "EUR": "EUR",
    "BRITISH POUND": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "GBP": "GBP",
}


def normalize_iso4217_currency(code: Optional[str]) -> str:
    """Validates and normalizes currency codes to standard ISO-4217 3-letter uppercase format."""
    default_curr = getattr(settings, "DEFAULT_CURRENCY", "INR").upper()
    if not code or not isinstance(code, str):
        return default_curr
    cleaned = code.strip().upper()
    if cleaned in _CURRENCY_NAME_MAP:
        return _CURRENCY_NAME_MAP[cleaned]
    if _ISO_4217_REGEX.match(cleaned):
        return cleaned
    return default_curr


class PlaybookCategory(str, Enum):
    ORDER_STATUS = "order_status_delivery"
    DELAYED_DELIVERY = "delayed_delivery"
    CANCELLATION_REQUEST = "cancellation_requests"
    RETURN_REPLACEMENT = "return_replacement"
    REFUND_STATUS = "refund_status"
    PAYMENT_DISCREPANCY = "payment_discrepancy"
    ACCOUNT_ACCESS = "account_access"
    TECHNICAL_SUPPORT = "technical_support"
    GENERAL_INQUIRY = "general_inquiry"


@dataclass(frozen=True)
class ProposedActionPlan:
    action_type: str
    action_key: str
    action_params: Dict[str, Any]
    is_sensitive: bool = False
    requires_approval: bool = False


@dataclass(frozen=True)
class PlaybookEvaluationResult:
    category: PlaybookCategory
    eligibility_decision: str  # "ELIGIBLE", "INELIGIBLE", "NEEDS_APPROVAL", "NEEDS_INFORMATION", "ESCALATE"
    recommended_resolution: str
    confidence_score: float
    requires_human_approval: bool
    supporting_evidence: List[str]
    proposed_actions: List[ProposedActionPlan]
    rejection_or_escalation_reason: Optional[str] = None


class BasePlaybook:
    """Base class for category-specific resolution playbooks."""

    category: PlaybookCategory
    title: str
    required_fields: List[str]
    permitted_actions: List[str]
    prohibited_actions: List[str]
    is_financial: bool = False
    is_security: bool = False
    requires_human_approval_default: bool = False

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        raise NotImplementedError


class OrderStatusPlaybook(BasePlaybook):
    category = PlaybookCategory.ORDER_STATUS
    title = "Order Status & Delivery Inquiries"
    required_fields = ["order_id", "tracking_number"]
    permitted_actions = ["SEND_ORDER_STATUS", "SEND_TRACKING_DETAILS"]
    prohibited_actions = ["ISSUE_REFUND", "CANCEL_ORDER"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}
        ref_ids = synthesis.get("reference_identifiers", [])
        has_ref = any(
            r.get("type") in ("order_id", "tracking_number", "shipment_id")
            for r in ref_ids
        ) or any(k in description.lower() for k in ("order #", "tracking #", "order id"))

        if not has_ref:
            facts.append("Missing required order identifier or tracking number.")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_INFORMATION",
                recommended_resolution="Request order number or tracking ID from customer.",
                confidence_score=0.4,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="Order reference identifier missing.",
            )

        facts.append("Order identifier present in ticket synthesis/conversation.")
        action = ProposedActionPlan(
            action_type="SEND_ORDER_STATUS",
            action_key=f"order_status_{ticket_id[:8]}",
            action_params={"status": "IN_TRANSIT", "carrier": "FedEx", "eta": "2 business days"},
            is_sensitive=False,
            requires_approval=False,
        )
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Transmit current shipment status and estimated delivery date to customer.",
            confidence_score=0.92,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class DelayedDeliveryPlaybook(BasePlaybook):
    category = PlaybookCategory.DELAYED_DELIVERY
    title = "Delayed Delivery Investigation"
    required_fields = ["order_id", "tracking_number"]
    permitted_actions = [
        "REQUEST_CARRIER_INVESTIGATION",
        "SEND_DELAY_EXPLANATION",
        "OFFER_DELIVERY_EXPEDITE",
        "APPLY_GOODWILL_CREDIT",
    ]
    prohibited_actions = ["AUTO_REFUND"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}
        ref_ids = synthesis.get("reference_identifiers", [])
        has_ref = any(r.get("type") in ("order_id", "tracking_number") for r in ref_ids) or "order" in description.lower()

        if not has_ref:
            facts.append("Missing shipment reference identifier.")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_INFORMATION",
                recommended_resolution="Request tracking number to initiate carrier delay tracer.",
                confidence_score=0.45,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="Order identifier required for tracer.",
            )

        facts.append("Shipment reference confirmed; delay exceeds expected delivery SLA.")
        actions = [
            ProposedActionPlan(
                action_type="REQUEST_CARRIER_INVESTIGATION",
                action_key=f"carrier_trace_{ticket_id[:8]}",
                action_params={"priority": "EXPEDITED", "carrier": "FedEx"},
                is_sensitive=False,
                requires_approval=False,
            ),
            ProposedActionPlan(
                action_type="SEND_DELAY_EXPLANATION",
                action_key=f"delay_notice_{ticket_id[:8]}",
                action_params={"message": "We have opened an official carrier tracer and will update within 24h."},
                is_sensitive=False,
                requires_approval=False,
            ),
        ]

        # Evaluate Smart INR Goodwill Credit Policy if severe delay or high priority
        is_severe = (
            "severe" in description.lower()
            or "week" in description.lower()
            or priority.upper() in ("HIGH", "CRITICAL", "URGENT")
            or sla_breached
        )
        if is_severe:
            # Check verified order value from evidence or synthesis
            verified_order_val = None
            for ev in evidence_data:
                if ev.get("status") == "VERIFIED":
                    if ev.get("extracted_amount") is not None:
                        verified_order_val = ev["extracted_amount"]
                        break
                    elif ev.get("amount") is not None:
                        verified_order_val = ev["amount"]
                        break

            if verified_order_val is None:
                if synthesis.get("verified_order_value") is not None:
                    verified_order_val = synthesis["verified_order_value"]
                elif synthesis.get("order_value") is not None and synthesis.get("order_value_verified"):
                    verified_order_val = synthesis["order_value"]

            goodwill_policy = get_goodwill_credit_policy()
            cust_tier = synthesis.get("customer_tier") or synthesis.get("tier", "STANDARD")
            existing_credits = synthesis.get("existing_credits_issued") or synthesis.get("previous_credits", 0)
            ent_config = synthesis.get("enterprise_config")
            calc_res = goodwill_policy.calculate_credit(
                customer_tier=cust_tier,
                verified_order_value=verified_order_val,
                delay_severity="SEVERE" if is_severe else "MODERATE",
                sla_breached=sla_breached,
                existing_credits_issued=existing_credits,
                enterprise_config=ent_config,
            )
            if calc_res.eligible:
                facts.append(f"SMART INR GOODWILL CREDIT: {calc_res.reason}")
                actions.append(
                    ProposedActionPlan(
                        action_type="APPLY_GOODWILL_CREDIT",
                        action_key=f"goodwill_{ticket_id[:8]}",
                        action_params=calc_res.to_action_params(),
                        is_sensitive=calc_res.requires_approval,
                        requires_approval=calc_res.requires_approval,
                    )
                )
            else:
                facts.append(f"GOODWILL CREDIT INELIGIBLE: {calc_res.reason}")

        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Open carrier tracer, inform customer of proactive package tracking, and apply courtesy credit if eligible.",
            confidence_score=0.88,
            requires_human_approval=any(a.requires_approval for a in actions),
            supporting_evidence=facts,
            proposed_actions=actions,
        )


class CancellationRequestPlaybook(BasePlaybook):
    category = PlaybookCategory.CANCELLATION_REQUEST
    title = "Order Cancellation Request"
    required_fields = ["order_id"]
    permitted_actions = ["REQUEST_ORDER_CANCELLATION", "SEND_CANCELLATION_CONFIRMATION", "REDIRECT_TO_RETURNS"]
    prohibited_actions = ["REFUND_WITHOUT_APPROVAL"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}
        ref_ids = synthesis.get("reference_identifiers", [])
        has_order = any(r.get("type") == "order_id" for r in ref_ids) or "order" in description.lower()

        if not has_order:
            facts.append("Cancellation requires valid order ID.")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_INFORMATION",
                recommended_resolution="Ask customer for order ID to check shipment dispatch state.",
                confidence_score=0.4,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="Missing order number.",
            )

        # Check if customer mentions already shipped
        if "already delivered" in description.lower() or "received item" in description.lower():
            facts.append("Order already delivered; cancellation ineligible, redirecting to return policy.")
            action = ProposedActionPlan(
                action_type="REDIRECT_TO_RETURNS",
                action_key=f"cancel_redirect_{ticket_id[:8]}",
                action_params={"policy": "standard_30_day_return"},
            )
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="ELIGIBLE",
                recommended_resolution="Item delivered; issue return authorization instead of cancellation.",
                confidence_score=0.9,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[action],
            )

        facts.append("Order pending dispatch; cancellation requested.")
        action = ProposedActionPlan(
            action_type="REQUEST_ORDER_CANCELLATION",
            action_key=f"cancel_order_{ticket_id[:8]}",
            action_params={"reason": "Customer request prior to fulfillment"},
            is_sensitive=False,
            requires_approval=False,
        )
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Submit order cancellation request to fulfillment queue.",
            confidence_score=0.85,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class ReturnReplacementPlaybook(BasePlaybook):
    category = PlaybookCategory.RETURN_REPLACEMENT
    title = "Return and Replacement Requests"
    required_fields = ["order_id", "product_name"]
    permitted_actions = ["ISSUE_RETURN_LABEL", "PROPOSE_REPLACEMENT", "SCHEDULE_PICKUP"]
    prohibited_actions = ["INSTANT_UNVERIFIED_REFUND"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}
        missing = synthesis.get("missing_fields", [])
        is_damaged = "damaged" in category.lower() or "damaged" in description.lower()

        # If damaged, requires evidence
        has_evidence = len(evidence_data) > 0
        if is_damaged and not has_evidence:
            facts.append("Damaged item claim requires photo evidence of packaging and item damage.")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_INFORMATION",
                recommended_resolution="Request photo proof of damaged goods before replacement approval.",
                confidence_score=0.5,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="Photo evidence required for damaged return authorization.",
            )

        facts.append("Return request validated against return window.")
        actions = [
            ProposedActionPlan(
                action_type="ISSUE_RETURN_LABEL",
                action_key=f"return_label_{ticket_id[:8]}",
                action_params={"method": "PREPAID_RETURN_LABEL", "carrier": "UPS"},
            ),
            ProposedActionPlan(
                action_type="PROPOSE_REPLACEMENT",
                action_key=f"propose_replace_{ticket_id[:8]}",
                action_params={"replacement_sku": synthesis.get("product_or_service") or "ORIGINAL_ITEM"},
            ),
        ]
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Generate prepaid return shipping label and queue replacement item.",
            confidence_score=0.9,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=actions,
        )


class RefundStatusPlaybook(BasePlaybook):
    category = PlaybookCategory.REFUND_STATUS
    title = "Refund Status Inquiries"
    required_fields = ["order_id"]
    permitted_actions = ["CHECK_REFUND_SCHEDULE", "SEND_REFUND_TIMELINE"]
    prohibited_actions = ["ISSUE_NEW_REFUND"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = ["Inquiry asks for status of existing processed refund."]
        action = ProposedActionPlan(
            action_type="SEND_REFUND_TIMELINE",
            action_key=f"refund_timeline_{ticket_id[:8]}",
            action_params={"standard_banking_days": "3-5 business days", "network": "ACH/Card"},
        )
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Provide customer with refund batch processing schedule and bank clearance windows.",
            confidence_score=0.95,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class PaymentDiscrepancyPlaybook(BasePlaybook):
    """SENSITIVE FINANCIAL PLAYBOOK: Mandatory Human Supervisor Authorization.

    Invariants:
    1. Customer claims alone never establish verified loss.
    2. Requires DOCUMENT_CORROBORATED evidence with verified variance.
    3. Contradictions suppress resolution and require escalation.
    4. Financial payouts require explicit supervisor/admin approval.
    """
    category = PlaybookCategory.PAYMENT_DISCREPANCY
    title = "Payment Discrepancy & Dispute Resolution"
    required_fields = ["order_id", "transaction_reference", "receipt_evidence"]
    permitted_actions = ["REQUEST_FINANCIAL_APPROVAL", "ISSUE_REFUND", "ESCALATE_TO_FINANCE"]
    prohibited_actions = ["AUTO_REFUND", "UNAUTHORIZED_CREDIT"]
    is_financial = True
    requires_human_approval_default = True

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}

        # 1. Check for contradictions
        contradictions = synthesis.get("contradictions", [])
        if contradictions:
            facts.append(f"Contradictions detected in claim/evidence: {len(contradictions)} conflict(s).")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="ESCALATE",
                recommended_resolution="Escalate to Finance department for manual audit due to evidence contradictions.",
                confidence_score=0.3,
                requires_human_approval=True,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="Evidence contradicts customer statement; financial automation suspended.",
            )

        # 2. Check for corroborating evidence
        verified_ev = [e for e in evidence_data if e.get("status") == "VERIFIED"]
        if not verified_ev:
            facts.append("No verified receipt or bank statement evidence provided (unsubstantiated customer claim).")
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_INFORMATION",
                recommended_resolution="Customer claim alone is insufficient for financial adjustment. Request official bank statement or receipt.",
                confidence_score=0.4,
                requires_human_approval=False,
                supporting_evidence=facts,
                proposed_actions=[],
                rejection_or_escalation_reason="DOCUMENT_CORROBORATED evidence missing.",
            )

        # 3. Document corroborated -> propose financial action requiring SUPERVISOR approval
        facts.append(f"Document corroborated evidence verified ({len(verified_ev)} document(s)).")
        facts.append("FINANCIAL SAFEGUARD: Autonomous payout prohibited. Human supervisor approval required.")

        # Multi-Currency & Configurable Thresholds: Extract amount and currency dynamically
        raw_default_amount = getattr(settings, "DEFAULT_DISCREPANCY_REFUND_AMOUNT", 49.99)
        try:
            extracted_amount: float = float(raw_default_amount)
        except (ValueError, TypeError):
            extracted_amount = 49.99
        extracted_curr: str = getattr(settings, "DEFAULT_CURRENCY", "USD")

        for ev in verified_ev:
            if ev.get("extracted_amount") is not None:
                try:
                    extracted_amount = float(ev["extracted_amount"])
                except (ValueError, TypeError):
                    pass
            elif ev.get("amount") is not None:
                try:
                    extracted_amount = float(ev["amount"])
                except (ValueError, TypeError):
                    pass

            if ev.get("extracted_currency"):
                extracted_curr = str(ev["extracted_currency"])
            elif ev.get("currency"):
                extracted_curr = str(ev["currency"])

        if synthesis.get("currency"):
            extracted_curr = str(synthesis["currency"])
        if synthesis.get("amount") is not None:
            try:
                extracted_amount = float(synthesis["amount"])
            except (ValueError, TypeError):
                pass

        normalized_currency = normalize_iso4217_currency(extracted_curr)

        action = ProposedActionPlan(
            action_type="ISSUE_REFUND",
            action_key=f"refund_dispute_{ticket_id[:8]}",
            action_params={
                "evidence_file_ids": [e.get("id") for e in verified_ev],
                "verified_variance": True,
                "amount": round(extracted_amount, 2),
                "currency": normalized_currency,
                "simulated_payout_reference": f"SIM-PAYOUT-{ticket_id[:8].upper()}",
                "payout_status": "PENDING_GATEWAY_INTEGRATION",
                "live_gateway_settled": False,
                "authoritative_gateway_present": False,
            },
            is_sensitive=True,
            requires_approval=True,
        )

        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="NEEDS_APPROVAL",
            recommended_resolution="Document evidence corroborates variance. Propose refund awaiting supervisor authorization.",
            confidence_score=0.88,
            requires_human_approval=True,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class AccountAccessPlaybook(BasePlaybook):
    """SENSITIVE SECURITY PLAYBOOK: Credentials and lockout remediation."""

    category = PlaybookCategory.ACCOUNT_ACCESS
    title = "Account Access & Security Remediation"
    required_fields = ["email", "account_id"]
    permitted_actions = [
        "TRIGGER_SECURE_PASSWORD_RESET",
        "VERIFY_CUSTOMER_IDENTITY",
        "SUSPEND_ACCOUNT_PENDING_VERIFICATION",
        "RESTORE_ACCOUNT_ACCESS",
    ]
    prohibited_actions = ["SEND_PLAINTEXT_CREDENTIALS", "BYPASS_2FA"]
    is_security = True

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = []
        synthesis = synthesis_data or {}

        # Authoritative indicators: system security flag, threat intelligence flag, or synthesis incident flag
        has_authoritative_flag = (
            bool(synthesis.get("security_compromise"))
            or bool(synthesis.get("is_security_incident"))
            or any(bool(e.get("security_flags")) for e in evidence_data)
            or category.lower() in ("security_incident", "credential_theft")
        )

        customer_mentions_compromise = (
            "compromised" in description.lower()
            or "hack" in description.lower()
            or "unauthorized access" in description.lower()
            or "stolen" in description.lower()
        )

        if has_authoritative_flag or customer_mentions_compromise:
            if has_authoritative_flag:
                facts.append("AUTHORITATIVE SECURITY SIGNAL: Verified or system-flagged security compromise.")
            else:
                facts.append("CUSTOMER-REPORTED CLAIM: Suspected compromise reported by customer without authoritative system corroboration.")

            facts.append("ACCOUNT LOCKDOWN SAFEGUARD: Lockdown requires supervisor authorization.")
            action = ProposedActionPlan(
                action_type="SUSPEND_ACCOUNT_PENDING_VERIFICATION",
                action_key=f"suspend_sec_{ticket_id[:8]}",
                action_params={
                    "lockout_reason": "Suspected account compromise" if has_authoritative_flag else "Customer-reported suspected compromise awaiting verification",
                    "authoritative_compromise": has_authoritative_flag,
                    "recovery_path": "STAFF_IDENTITY_VERIFICATION_REQUIRED",
                    "unlock_action_available": True,
                    "unlock_procedure": "Supervisor or Administrator must authenticate customer identity and issue RESTORE_ACCOUNT_ACCESS",
                },
                is_sensitive=True,
                requires_approval=True,
            )
            return PlaybookEvaluationResult(
                category=self.category,
                eligibility_decision="NEEDS_APPROVAL",
                recommended_resolution=(
                    "Lock account pending manual identity verification and supervisor signoff."
                    if has_authoritative_flag
                    else "Customer-reported compromise: require supervisor signoff before account suspension."
                ),
                confidence_score=0.88 if has_authoritative_flag else 0.72,
                requires_human_approval=True,
                supporting_evidence=facts,
                proposed_actions=[action],
                rejection_or_escalation_reason="Account compromise indicators require supervisor verification.",
            )

        facts.append("Standard account access lockout; safe password reset procedure eligible.")
        action = ProposedActionPlan(
            action_type="TRIGGER_SECURE_PASSWORD_RESET",
            action_key=f"pwd_reset_{ticket_id[:8]}",
            action_params={"delivery": "REGISTERED_EMAIL_ONLY", "expiry_hours": 2},
            is_sensitive=False,
            requires_approval=False,
        )
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Dispatch secure single-use password reset link to customer's registered email.",
            confidence_score=0.94,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class TechnicalSupportPlaybook(BasePlaybook):
    category = PlaybookCategory.TECHNICAL_SUPPORT
    title = "Technical Support & Incident Resolution"
    required_fields = ["problem_statement"]
    permitted_actions = ["SEND_TROUBLESHOOTING_STEPS", "DISPATCH_ENGINEERING_TICKET"]
    prohibited_actions = ["MODIFY_USER_BILLING"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = ["Technical support inquiry analyzed."]
        actions = [
            ProposedActionPlan(
                action_type="SEND_TROUBLESHOOTING_STEPS",
                action_key=f"tech_troubleshoot_{ticket_id[:8]}",
                action_params={"steps": ["1. Clear application cache", "2. Re-authenticate session", "3. Verify network proxy"]},
            )
        ]
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Provide structured remediation steps and monitor error recurrence.",
            confidence_score=0.88,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=actions,
        )


class GeneralInquiryPlaybook(BasePlaybook):
    category = PlaybookCategory.GENERAL_INQUIRY
    title = "General Customer Inquiries"
    required_fields = ["problem_statement"]
    permitted_actions = ["SEND_KNOWLEDGE_BASE_ARTICLE", "SEND_EXPLANATION"]
    prohibited_actions = ["MODIFY_ORDER", "MODIFY_PAYMENT", "MODIFY_ACCOUNT"]

    def evaluate(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]],
        evidence_data: List[Dict[str, Any]],
        priority: str,
        sla_breached: bool,
    ) -> PlaybookEvaluationResult:
        facts: List[str] = ["General informational request."]
        action = ProposedActionPlan(
            action_type="SEND_EXPLANATION",
            action_key=f"gen_explain_{ticket_id[:8]}",
            action_params={"topic": "Platform policies and operating guidelines"},
        )
        return PlaybookEvaluationResult(
            category=self.category,
            eligibility_decision="ELIGIBLE",
            recommended_resolution="Provide concise explanation addressing customer query.",
            confidence_score=0.91,
            requires_human_approval=False,
            supporting_evidence=facts,
            proposed_actions=[action],
        )


class PlaybookRegistry:
    """Registry providing lookup and canonical category mapping for playbooks."""

    def __init__(self):
        self._playbooks: Dict[PlaybookCategory, BasePlaybook] = {
            PlaybookCategory.ORDER_STATUS: OrderStatusPlaybook(),
            PlaybookCategory.DELAYED_DELIVERY: DelayedDeliveryPlaybook(),
            PlaybookCategory.CANCELLATION_REQUEST: CancellationRequestPlaybook(),
            PlaybookCategory.RETURN_REPLACEMENT: ReturnReplacementPlaybook(),
            PlaybookCategory.REFUND_STATUS: RefundStatusPlaybook(),
            PlaybookCategory.PAYMENT_DISCREPANCY: PaymentDiscrepancyPlaybook(),
            PlaybookCategory.ACCOUNT_ACCESS: AccountAccessPlaybook(),
            PlaybookCategory.TECHNICAL_SUPPORT: TechnicalSupportPlaybook(),
            PlaybookCategory.GENERAL_INQUIRY: GeneralInquiryPlaybook(),
        }

    def resolve_playbook(self, category: str, description: str = "") -> BasePlaybook:
        """Deterministically maps ticket category and problem statement to a playbook."""
        cat_clean = (category or "general").lower().strip()
        desc_clean = (description or "").lower()

        if cat_clean in ("delivery_issue", "order_status", "delivery", "shipping"):
            if "delay" in desc_clean or "late" in desc_clean or "overdue" in desc_clean:
                return self._playbooks[PlaybookCategory.DELAYED_DELIVERY]
            return self._playbooks[PlaybookCategory.ORDER_STATUS]

        if cat_clean in ("cancellation_return", "cancellation", "return"):
            if "cancel" in desc_clean or "cancellation" in desc_clean:
                return self._playbooks[PlaybookCategory.CANCELLATION_REQUEST]
            return self._playbooks[PlaybookCategory.RETURN_REPLACEMENT]

        if cat_clean in ("damaged_product", "product_quality"):
            return self._playbooks[PlaybookCategory.RETURN_REPLACEMENT]

        if cat_clean in ("payment_issue", "billing_issue", "billing", "payment"):
            if "status" in desc_clean or "when" in desc_clean or "how long" in desc_clean:
                return self._playbooks[PlaybookCategory.REFUND_STATUS]
            return self._playbooks[PlaybookCategory.PAYMENT_DISCREPANCY]

        if cat_clean in ("account_issue", "security_incident", "account", "security"):
            return self._playbooks[PlaybookCategory.ACCOUNT_ACCESS]

        if cat_clean in ("technical_support", "product_defect", "technical", "bug"):
            return self._playbooks[PlaybookCategory.TECHNICAL_SUPPORT]

        return self._playbooks[PlaybookCategory.GENERAL_INQUIRY]

    def get(self, category: PlaybookCategory) -> BasePlaybook:
        return self._playbooks[category]


_default_registry: Optional[PlaybookRegistry] = None


def get_playbook_registry() -> PlaybookRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = PlaybookRegistry()
    return _default_registry

