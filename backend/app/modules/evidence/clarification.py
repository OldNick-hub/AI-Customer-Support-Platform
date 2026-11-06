"""Customer Clarification Management & Re-evaluation Engine.

Manages the lifecycle of customer clarification requests when claims are conflicting,
missing, or uncertain:
- Clarification status: PENDING_CUSTOMER, RESOLVED, SUPERSEDED, FAILED.
- Strict PII/PCI security (never asks for full PAN/card numbers, CVVs, passwords, PINs).
- Polite, targeted, explainable natural language questions.
- Re-evaluation flow: parses customer answers and re-runs reconciliation.
"""

from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger
from app.modules.evidence.claims import ClaimExtractionEngine, ExtractedClaimsPayload
from app.modules.evidence.reconciler import (
    EvidenceReconciliationEngine,
    FieldComparisonResult,
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.modules.evidence.schemas import StructuredInvoiceData

logger = configure_safe_logger(logging.getLogger(__name__))


class ClarificationStatus(str, Enum):
    """Lifecycle state of an outstanding evidence clarification request."""
    NONE_REQUIRED = "NONE_REQUIRED"
    PENDING_CUSTOMER = "PENDING_CUSTOMER"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class ClarificationRequest(BaseModel):
    """Customer-facing clarification request with actionable discrepancy context."""

    clarification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    verification_id: str
    conversation_id: str
    customer_id: Optional[str] = None
    status: ClarificationStatus = ClarificationStatus.PENDING_CUSTOMER
    prompt_message: str
    mismatched_fields: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    uncertain_fields: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None
    customer_response: Optional[str] = None


class ClarificationResponsePayload(BaseModel):
    """Customer submission answering an outstanding clarification request."""

    response_text: str = Field(..., min_length=1, description="Customer reply text")
    attachment_evidence_id: Optional[str] = Field(default=None, description="Optional new evidence file ID uploaded")


class ClarificationEngine:
    """Generates targeted clarification inquiries and orchestrates re-evaluation."""

    # PCI / Sensitive fields that must NEVER be requested
    PROHIBITED_TERMS = {
        "cvv", "cvv2", "security code", "pin", "password", "passcode",
        "card number", "pan", "otp", "one-time password", "secret",
    }

    def __init__(
        self,
        reconciliation_engine: Optional[EvidenceReconciliationEngine] = None,
        claims_engine: Optional[ClaimExtractionEngine] = None,
    ):
        self.reconciliation_engine = reconciliation_engine or EvidenceReconciliationEngine()
        self.claims_engine = claims_engine or ClaimExtractionEngine()

    def generate_clarification(
        self,
        reconciliation: ReconciliationResult,
        verification_id: str,
    ) -> Optional[ClarificationRequest]:
        """Generate a secure, specific clarification request if required."""
        if not reconciliation.requires_clarification:
            return None

        prompt = self._compose_secure_prompt(reconciliation)

        return ClarificationRequest(
            verification_id=verification_id,
            conversation_id=reconciliation.conversation_id or "unknown",
            customer_id=reconciliation.customer_id,
            status=ClarificationStatus.PENDING_CUSTOMER,
            prompt_message=prompt,
            mismatched_fields=reconciliation.mismatched_fields,
            missing_fields=reconciliation.missing_evidence_fields,
            uncertain_fields=reconciliation.uncertain_fields,
        )

    def re_evaluate(
        self,
        original_claims: ExtractedClaimsPayload,
        evidence_data: StructuredInvoiceData,
        customer_response_text: str,
        verification_id: str,
        evidence_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Tuple[ReconciliationResult, ClarificationStatus]:
        """Re-evaluate evidence against updated customer claims after receiving customer clarification."""

        logger.info(
            "Re-evaluating clarification: verification_id=%s conversation_id=%s",
            verification_id,
            conversation_id,
        )

        # 1. Extract new claims from response
        new_claims_payload = self.claims_engine.extract_claims(
            text=customer_response_text,
            message_id=str(uuid.uuid4()),
        )

        # 2. Merge claims: new claims override or supplement old claims
        merged_claims = self._merge_claims(original_claims, new_claims_payload)

        # 3. Re-run reconciliation engine
        new_result = self.reconciliation_engine.reconcile(
            claims_payload=merged_claims,
            evidence_data=evidence_data,
            evidence_id=evidence_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )

        # 4. Determine new clarification status
        if new_result.overall_outcome == ReconciliationOutcome.MATCH:
            new_status = ClarificationStatus.RESOLVED
        elif new_result.overall_outcome in (
            ReconciliationOutcome.MISMATCH,
            ReconciliationOutcome.UNCERTAIN,
            ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
            ReconciliationOutcome.MISSING_FROM_EVIDENCE,
        ):
            new_status = ClarificationStatus.PENDING_CUSTOMER
        else:
            new_status = ClarificationStatus.RESOLVED

        return new_result, new_status

    def _compose_secure_prompt(self, reconciliation: ReconciliationResult) -> str:
        """Compose a user-friendly, specific explanation without violating privacy rules."""

        # If custom prompt was already formulated by reconciler, check security and return
        if reconciliation.clarification_prompt:
            lower = reconciliation.clarification_prompt.lower()
            if not any(term in lower for term in self.PROHIBITED_TERMS):
                return reconciliation.clarification_prompt

        # Fallback synthesizer
        parts: List[str] = []

        if reconciliation.is_quarantined_evidence:
            return "The uploaded file could not be verified due to a security scan flag. Please upload a clean, original document."

        if reconciliation.mismatched_fields:
            fields_str = " and ".join(reconciliation.mismatched_fields)
            parts.append(
                f"The {fields_str} on the document you provided does not match what you described."
            )

        if reconciliation.uncertain_fields:
            fields_str = " and ".join(reconciliation.uncertain_fields)
            parts.append(
                f"The {fields_str} on the uploaded file was blurry or difficult to read clearly."
            )

        if reconciliation.missing_evidence_fields:
            fields_str = " and ".join(reconciliation.missing_evidence_fields)
            parts.append(
                f"The document does not clearly display the {fields_str}."
            )

        if not parts:
            parts.append("We could not fully verify your details from the provided document.")

        parts.append("Could you please double-check the details or provide a clear, full photo of your invoice/receipt?")
        return " ".join(parts)

    def _merge_claims(
        self,
        base: ExtractedClaimsPayload,
        supplement: ExtractedClaimsPayload,
    ) -> ExtractedClaimsPayload:
        """Merge clarification claims into original claims, giving precedence to clarification."""

        combined_claims = list(base.claims) + list(supplement.claims)

        primary_order_id = supplement.primary_order_id or base.primary_order_id
        primary_invoice_number = supplement.primary_invoice_number or base.primary_invoice_number
        primary_amount = supplement.primary_amount if supplement.primary_amount is not None else base.primary_amount
        primary_currency = supplement.primary_currency or base.primary_currency
        primary_date = supplement.primary_date or base.primary_date
        primary_merchant = supplement.primary_merchant or base.primary_merchant
        primary_product = supplement.primary_product or base.primary_product
        primary_quantity = supplement.primary_quantity if supplement.primary_quantity is not None else base.primary_quantity
        intent_double_charge = supplement.intent_double_charge or base.intent_double_charge

        return ExtractedClaimsPayload(
            message_id=supplement.message_id or base.message_id,
            raw_text=f"{base.raw_text} | Clarification: {supplement.raw_text}",
            pii_masked_text=f"{base.pii_masked_text} | Clarification: {supplement.pii_masked_text}",
            claims=combined_claims,
            primary_order_id=primary_order_id,
            primary_invoice_number=primary_invoice_number,
            primary_amount=primary_amount,
            primary_currency=primary_currency,
            primary_date=primary_date,
            primary_merchant=primary_merchant,
            primary_product=primary_product,
            primary_quantity=primary_quantity,
            intent_double_charge=intent_double_charge,
        )
