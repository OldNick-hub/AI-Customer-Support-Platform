"""Evidence Reconciliation & Intelligent Mismatch Detection Engine.

Compares customer conversational claims against extracted OCR evidence entities:
- Outcome states: MATCH, MISMATCH, MISSING_FROM_EVIDENCE, MISSING_FROM_MESSAGE,
  UNCERTAIN, INSUFFICIENT_EVIDENCE.
- Exact difference calculations for financial amounts.
- Explicit currency parity validation (rejects cross-currency matching).
- Date normalization and configurable tolerance.
- Quarantined/malicious evidence exclusion.
- Low OCR confidence detection (< 0.70).
- Complex intent verification (e.g. double charging requiring multi-transaction proof).
"""

from datetime import date, datetime, timezone
from enum import Enum
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger
from app.modules.evidence.claims import ClaimType, CustomerClaim, ExtractedClaimsPayload
from app.modules.evidence.schemas import StructuredInvoiceData

logger = configure_safe_logger(logging.getLogger(__name__))


class ReconciliationOutcome(str, Enum):
    """Authoritative outcome classification for claim vs evidence reconciliation."""
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    MISSING_FROM_EVIDENCE = "MISSING_FROM_EVIDENCE"
    MISSING_FROM_MESSAGE = "MISSING_FROM_MESSAGE"
    UNCERTAIN = "UNCERTAIN"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class FieldComparisonResult(BaseModel):
    """Comparison detail for a single verified field."""

    field_name: str
    customer_claim: Optional[Any] = None
    evidence_value: Optional[Any] = None
    outcome: ReconciliationOutcome
    difference_amount: Optional[float] = None
    difference_unit: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


class ReconciliationResult(BaseModel):
    """Comprehensive verdict returned by the Evidence Reconciliation Engine."""

    overall_outcome: ReconciliationOutcome
    overall_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: str
    field_comparisons: List[FieldComparisonResult] = Field(default_factory=list)
    mismatched_fields: List[str] = Field(default_factory=list)
    missing_evidence_fields: List[str] = Field(default_factory=list)
    uncertain_fields: List[str] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_prompt: Optional[str] = None
    is_quarantined_evidence: bool = False
    quarantine_reason: Optional[str] = None
    evidence_id: Optional[str] = None
    conversation_id: Optional[str] = None
    customer_id: Optional[str] = None
    message_id: Optional[str] = None
    reconciled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceReconciliationEngine:
    """Engine responsible for comparing extracted customer claims against document evidence."""

    CONFIDENCE_THRESHOLD: float = 0.70

    def __init__(
        self,
        amount_tolerance: float = 0.00,
        date_tolerance_days: int = 0,
        confidence_threshold: float = 0.70,
    ):
        self.amount_tolerance = amount_tolerance
        self.date_tolerance_days = date_tolerance_days
        self.confidence_threshold = confidence_threshold

    def reconcile(
        self,
        claims_payload: ExtractedClaimsPayload,
        evidence_data: Optional[StructuredInvoiceData],
        evidence_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        is_quarantined: bool = False,
        quarantine_reason: Optional[str] = None,
    ) -> ReconciliationResult:
        """Execute full field-by-field reconciliation between customer claims and evidence."""

        # 1. Quarantined / Malicious Evidence Gate
        if is_quarantined or (evidence_data and getattr(evidence_data, "is_malicious", False)):
            logger.warning(
                "Reconciliation aborted - quarantined evidence: id=%s conv=%s reason=%s",
                evidence_id,
                conversation_id,
                quarantine_reason or "Malicious or flagged unsafe payload",
            )
            return ReconciliationResult(
                overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                overall_confidence=0.0,
                summary="Evidence document is flagged unsafe or quarantined. It cannot be used to verify claims.",
                field_comparisons=[],
                mismatched_fields=[],
                missing_evidence_fields=[],
                uncertain_fields=[],
                requires_clarification=True,
                clarification_prompt="The uploaded file could not be verified due to a security flag. Please provide a clear, original document.",
                is_quarantined_evidence=True,
                quarantine_reason=quarantine_reason or "Document flagged unsafe",
                evidence_id=evidence_id,
                conversation_id=conversation_id,
                customer_id=customer_id,
                message_id=claims_payload.message_id,
            )

        # 2. Missing Evidence Check
        if not evidence_data:
            return ReconciliationResult(
                overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                overall_confidence=0.0,
                summary="No valid evidence data provided for reconciliation.",
                field_comparisons=[],
                mismatched_fields=[],
                missing_evidence_fields=["all"],
                uncertain_fields=[],
                requires_clarification=True,
                clarification_prompt="Please upload your receipt or invoice document to support your claim.",
                evidence_id=evidence_id,
                conversation_id=conversation_id,
                customer_id=customer_id,
                message_id=claims_payload.message_id,
            )

        field_comparisons: List[FieldComparisonResult] = []
        mismatched_fields: List[str] = []
        missing_evidence_fields: List[str] = []
        uncertain_fields: List[str] = []

        # 3. Check Intent: Charged Twice / Double Charge
        if claims_payload.intent_double_charge:
            # Single invoice only proves one charge. To verify a double charge, a second transaction is needed.
            intent_comp = FieldComparisonResult(
                field_name="intent_double_charge",
                customer_claim="Charged twice for transaction",
                evidence_value=f"Single invoice {evidence_data.invoice_number or evidence_data.order_id or 'provided'}",
                outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                confidence=1.0,
                notes="Single invoice verifies only one transaction. Proof of duplicate deduction is required.",
            )
            field_comparisons.append(intent_comp)

        # 4. Compare Order ID / Invoice Number
        order_comp = self._compare_order_id(claims_payload, evidence_data)
        if order_comp:
            field_comparisons.append(order_comp)
            self._categorize_field(order_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 5. Compare Currency & Amount
        currency_comp, amount_comp = self._compare_amount_and_currency(claims_payload, evidence_data)
        if currency_comp:
            field_comparisons.append(currency_comp)
            self._categorize_field(currency_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        if amount_comp:
            field_comparisons.append(amount_comp)
            self._categorize_field(amount_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 6. Compare Date
        date_comp = self._compare_date(claims_payload, evidence_data)
        if date_comp:
            field_comparisons.append(date_comp)
            self._categorize_field(date_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 7. Compare Merchant / Store Name
        merchant_comp = self._compare_merchant(claims_payload, evidence_data)
        if merchant_comp:
            field_comparisons.append(merchant_comp)
            self._categorize_field(merchant_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 8. Compare Product Name / Quantity
        product_comp = self._compare_product(claims_payload, evidence_data)
        if product_comp:
            field_comparisons.append(product_comp)
            self._categorize_field(product_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 9. Compute Overall Verdict & Confidence
        overall_outcome, overall_confidence, summary, requires_clarification, prompt = (
            self._derive_overall_verdict(
                claims_payload,
                evidence_data,
                field_comparisons,
                mismatched_fields,
                missing_evidence_fields,
                uncertain_fields,
            )
        )

        return ReconciliationResult(
            overall_outcome=overall_outcome,
            overall_confidence=overall_confidence,
            summary=summary,
            field_comparisons=field_comparisons,
            mismatched_fields=mismatched_fields,
            missing_evidence_fields=missing_evidence_fields,
            uncertain_fields=uncertain_fields,
            requires_clarification=requires_clarification,
            clarification_prompt=prompt,
            evidence_id=evidence_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            message_id=claims_payload.message_id,
        )

    def _categorize_field(
        self,
        comp: FieldComparisonResult,
        mismatched: List[str],
        missing: List[str],
        uncertain: List[str],
    ) -> None:
        if comp.outcome == ReconciliationOutcome.MISMATCH:
            mismatched.append(comp.field_name)
        elif comp.outcome == ReconciliationOutcome.MISSING_FROM_EVIDENCE:
            missing.append(comp.field_name)
        elif comp.outcome == ReconciliationOutcome.UNCERTAIN:
            uncertain.append(comp.field_name)

    def _compare_order_id(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> Optional[FieldComparisonResult]:
        claimed_order = claims.primary_order_id or claims.primary_invoice_number
        evidence_order = evidence.order_id or evidence.invoice_number

        if not claimed_order and not evidence_order:
            return None

        if claimed_order and not evidence_order:
            return FieldComparisonResult(
                field_name="order_id",
                customer_claim=claimed_order,
                evidence_value=None,
                outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                confidence=1.0,
                notes="Customer specified an Order ID but none was detected on the document.",
            )

        if not claimed_order and evidence_order:
            return FieldComparisonResult(
                field_name="order_id",
                customer_claim=None,
                evidence_value=evidence_order,
                outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                confidence=evidence.field_confidence.get("order_id", evidence.overall_confidence),
                notes="Document specifies an Order ID but customer message did not reference it.",
            )

        # Check OCR confidence
        order_conf = evidence.field_confidence.get(
            "order_id",
            evidence.field_confidence.get("invoice_number", evidence.overall_confidence),
        )
        if order_conf < self.confidence_threshold:
            return FieldComparisonResult(
                field_name="order_id",
                customer_claim=claimed_order,
                evidence_value=evidence_order,
                outcome=ReconciliationOutcome.UNCERTAIN,
                confidence=order_conf,
                notes=f"Low OCR confidence ({order_conf:.2f}) on extracted document identifier.",
            )

        # Normalized token comparison
        norm_claimed = re.sub(r"[\s_-]", "", claimed_order.upper())
        norm_evidence = re.sub(r"[\s_-]", "", evidence_order.upper())

        # Exact or substring match (e.g. "ORD-12345" vs "12345")
        if norm_claimed == norm_evidence or norm_claimed in norm_evidence or norm_evidence in norm_claimed:
            return FieldComparisonResult(
                field_name="order_id",
                customer_claim=claimed_order,
                evidence_value=evidence_order,
                outcome=ReconciliationOutcome.MATCH,
                confidence=order_conf,
                notes="Order / Invoice identifier matches document.",
            )

        return FieldComparisonResult(
            field_name="order_id",
            customer_claim=claimed_order,
            evidence_value=evidence_order,
            outcome=ReconciliationOutcome.MISMATCH,
            confidence=order_conf,
            notes=f"Order identifier '{claimed_order}' differs from document identifier '{evidence_order}'.",
        )

    def _compare_amount_and_currency(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> Tuple[Optional[FieldComparisonResult], Optional[FieldComparisonResult]]:
        claimed_amount = claims.primary_amount
        claimed_currency = claims.primary_currency
        evidence_amount = evidence.total_amount
        evidence_currency = evidence.currency

        currency_comp = None
        amount_comp = None

        # Currency Comparison
        if claimed_currency or evidence_currency:
            if claimed_currency and not evidence_currency:
                currency_comp = FieldComparisonResult(
                    field_name="currency",
                    customer_claim=claimed_currency,
                    evidence_value=None,
                    outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                    notes="Currency was specified by customer but missing on document.",
                )
            elif not claimed_currency and evidence_currency:
                currency_comp = FieldComparisonResult(
                    field_name="currency",
                    customer_claim=None,
                    evidence_value=evidence_currency,
                    outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                    notes="Document displays currency but customer did not specify.",
                )
            else:
                norm_c_curr = claimed_currency.upper()
                norm_e_curr = evidence_currency.upper()
                if norm_c_curr == norm_e_curr:
                    currency_comp = FieldComparisonResult(
                        field_name="currency",
                        customer_claim=claimed_currency,
                        evidence_value=evidence_currency,
                        outcome=ReconciliationOutcome.MATCH,
                        notes=f"Currency matches ({norm_c_curr}).",
                    )
                else:
                    currency_comp = FieldComparisonResult(
                        field_name="currency",
                        customer_claim=claimed_currency,
                        evidence_value=evidence_currency,
                        outcome=ReconciliationOutcome.MISMATCH,
                        notes=f"Customer currency '{claimed_currency}' does not match document currency '{evidence_currency}'.",
                    )

        # Amount Comparison
        if claimed_amount is not None or evidence_amount is not None:
            if claimed_amount is not None and evidence_amount is None:
                amount_comp = FieldComparisonResult(
                    field_name="total_amount",
                    customer_claim=claimed_amount,
                    evidence_value=None,
                    outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                    notes="Customer specified an amount but no total amount was found on document.",
                )
            elif claimed_amount is None and evidence_amount is not None:
                amount_comp = FieldComparisonResult(
                    field_name="total_amount",
                    customer_claim=None,
                    evidence_value=evidence_amount,
                    outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                    notes="Document shows total amount but customer did not state it.",
                )
            else:
                amt_conf = evidence.field_confidence.get("total_amount", evidence.overall_confidence)
                if amt_conf < self.confidence_threshold:
                    amount_comp = FieldComparisonResult(
                        field_name="total_amount",
                        customer_claim=claimed_amount,
                        evidence_value=evidence_amount,
                        outcome=ReconciliationOutcome.UNCERTAIN,
                        confidence=amt_conf,
                        notes=f"Low OCR confidence ({amt_conf:.2f}) on document amount.",
                    )
                else:
                    diff = round(claimed_amount - evidence_amount, 2)
                    unit = claimed_currency or evidence_currency or "units"

                    # If currency is mismatched, amount comparison is invalid / mismatch
                    if currency_comp and currency_comp.outcome == ReconciliationOutcome.MISMATCH:
                        amount_comp = FieldComparisonResult(
                            field_name="total_amount",
                            customer_claim=claimed_amount,
                            evidence_value=evidence_amount,
                            outcome=ReconciliationOutcome.MISMATCH,
                            difference_amount=diff,
                            difference_unit=unit,
                            confidence=amt_conf,
                            notes=f"Amount cannot be matched due to currency mismatch ({claimed_currency} vs {evidence_currency}).",
                        )
                    elif abs(diff) <= self.amount_tolerance:
                        amount_comp = FieldComparisonResult(
                            field_name="total_amount",
                            customer_claim=claimed_amount,
                            evidence_value=evidence_amount,
                            outcome=ReconciliationOutcome.MATCH,
                            difference_amount=0.0,
                            difference_unit=unit,
                            confidence=amt_conf,
                            notes=f"Amount matches exactly ({evidence_amount} {unit}).",
                        )
                    else:
                        amount_comp = FieldComparisonResult(
                            field_name="total_amount",
                            customer_claim=claimed_amount,
                            evidence_value=evidence_amount,
                            outcome=ReconciliationOutcome.MISMATCH,
                            difference_amount=diff,
                            difference_unit=unit,
                            confidence=amt_conf,
                            notes=f"Claimed amount {claimed_amount} differs from document total {evidence_amount} by {abs(diff):.2f} {unit}.",
                        )

        return currency_comp, amount_comp

    def _compare_date(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> Optional[FieldComparisonResult]:
        claimed_date = claims.primary_date
        evidence_date = evidence.transaction_date or evidence.invoice_date

        if not claimed_date and not evidence_date:
            return None

        if claimed_date and not evidence_date:
            return FieldComparisonResult(
                field_name="transaction_date",
                customer_claim=str(claimed_date),
                evidence_value=None,
                outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                notes="Customer specified a date but no date was found on the document.",
            )

        if not claimed_date and evidence_date:
            return FieldComparisonResult(
                field_name="transaction_date",
                customer_claim=None,
                evidence_value=str(evidence_date),
                outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                notes="Document shows date but customer did not specify it in the message.",
            )

        date_conf = evidence.field_confidence.get(
            "transaction_date",
            evidence.field_confidence.get("invoice_date", evidence.overall_confidence),
        )

        if date_conf < self.confidence_threshold:
            return FieldComparisonResult(
                field_name="transaction_date",
                customer_claim=str(claimed_date),
                evidence_value=str(evidence_date),
                outcome=ReconciliationOutcome.UNCERTAIN,
                confidence=date_conf,
                notes=f"Low OCR confidence ({date_conf:.2f}) on document date.",
            )

        delta_days = abs((claimed_date - evidence_date).days)
        if delta_days <= self.date_tolerance_days:
            return FieldComparisonResult(
                field_name="transaction_date",
                customer_claim=str(claimed_date),
                evidence_value=str(evidence_date),
                outcome=ReconciliationOutcome.MATCH,
                confidence=date_conf,
                notes=f"Date matches ({evidence_date}).",
            )

        return FieldComparisonResult(
            field_name="transaction_date",
            customer_claim=str(claimed_date),
            evidence_value=str(evidence_date),
            outcome=ReconciliationOutcome.MISMATCH,
            difference_amount=float(delta_days),
            difference_unit="days",
            confidence=date_conf,
            notes=f"Claimed date {claimed_date} differs from document date {evidence_date} by {delta_days} day(s).",
        )

    def _compare_merchant(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> Optional[FieldComparisonResult]:
        claimed_merchant = claims.primary_merchant
        evidence_merchant = evidence.merchant_name

        if not claimed_merchant and not evidence_merchant:
            return None

        if claimed_merchant and not evidence_merchant:
            return FieldComparisonResult(
                field_name="merchant_name",
                customer_claim=claimed_merchant,
                evidence_value=None,
                outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                notes="Customer mentioned a merchant but none was detected on document.",
            )

        if not claimed_merchant and evidence_merchant:
            return FieldComparisonResult(
                field_name="merchant_name",
                customer_claim=None,
                evidence_value=evidence_merchant,
                outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                notes="Document contains merchant name but customer did not state it.",
            )

        c_norm = claimed_merchant.strip().lower()
        e_norm = evidence_merchant.strip().lower()

        if c_norm in e_norm or e_norm in c_norm:
            return FieldComparisonResult(
                field_name="merchant_name",
                customer_claim=claimed_merchant,
                evidence_value=evidence_merchant,
                outcome=ReconciliationOutcome.MATCH,
                notes=f"Merchant matches ({evidence_merchant}).",
            )

        return FieldComparisonResult(
            field_name="merchant_name",
            customer_claim=claimed_merchant,
            evidence_value=evidence_merchant,
            outcome=ReconciliationOutcome.MISMATCH,
            notes=f"Claimed merchant '{claimed_merchant}' differs from document '{evidence_merchant}'.",
        )

    def _compare_product(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> Optional[FieldComparisonResult]:
        claimed_prod = claims.primary_product
        evidence_prod = evidence.product_name

        # Also search line items if evidence product_name is empty
        if not evidence_prod and evidence.line_items:
            evidence_prod = ", ".join(item.product_name for item in evidence.line_items[:2])

        if not claimed_prod and not evidence_prod:
            return None

        if claimed_prod and not evidence_prod:
            return FieldComparisonResult(
                field_name="product_name",
                customer_claim=claimed_prod,
                evidence_value=None,
                outcome=ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                notes="Customer stated product name but none was detected on document.",
            )

        if not claimed_prod and evidence_prod:
            return FieldComparisonResult(
                field_name="product_name",
                customer_claim=None,
                evidence_value=evidence_prod,
                outcome=ReconciliationOutcome.MISSING_FROM_MESSAGE,
                notes="Document contains product info not mentioned by customer.",
            )

        c_tokens = set(re.findall(r"\w+", claimed_prod.lower()))
        e_tokens = set(re.findall(r"\w+", evidence_prod.lower()))

        # If any token overlap exists
        if c_tokens.intersection(e_tokens):
            return FieldComparisonResult(
                field_name="product_name",
                customer_claim=claimed_prod,
                evidence_value=evidence_prod,
                outcome=ReconciliationOutcome.MATCH,
                notes="Product description matches document item.",
            )

        return FieldComparisonResult(
            field_name="product_name",
            customer_claim=claimed_prod,
            evidence_value=evidence_prod,
            outcome=ReconciliationOutcome.MISMATCH,
            notes=f"Claimed product '{claimed_prod}' does not match document product '{evidence_prod}'.",
        )

    def _derive_overall_verdict(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
        comparisons: List[FieldComparisonResult],
        mismatched: List[str],
        missing_evidence: List[str],
        uncertain: List[str],
    ) -> Tuple[ReconciliationOutcome, float, str, bool, Optional[str]]:
        """Calculate overall outcome, confidence, summary text, and clarification needs."""

        # Complex intent double charge with single document
        if claims.intent_double_charge:
            return (
                ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                0.80,
                "Customer claims a duplicate charge, but only a single transaction receipt was provided.",
                True,
                "You mentioned being charged twice. The uploaded invoice confirms one transaction. Please provide a bank statement or second receipt showing the duplicate deduction.",
            )

        if not comparisons:
            return (
                ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                0.50,
                "No verifiable claims or document fields could be aligned.",
                True,
                "We could not find matching details between your message and the document. Please provide your Order ID or receipt amount.",
            )

        # Average confidence across compared fields
        avg_conf = sum(c.confidence for c in comparisons) / len(comparisons)

        # High priority: Low OCR confidence / Uncertain fields
        if uncertain:
            fields_str = ", ".join(uncertain)
            return (
                ReconciliationOutcome.UNCERTAIN,
                avg_conf,
                f"Document text for {fields_str} could not be read with sufficient confidence.",
                True,
                f"The image quality for {fields_str} on your document was low. Could you please confirm the exact {fields_str} or provide a clearer photo?",
            )

        # Mismatch detected
        if mismatched:
            mismatched_notes = [c.notes for c in comparisons if c.outcome == ReconciliationOutcome.MISMATCH and c.notes]
            notes_str = "; ".join(mismatched_notes[:2])
            fields_str = ", ".join(mismatched)
            return (
                ReconciliationOutcome.MISMATCH,
                avg_conf,
                f"Discrepancy detected between claim and document for: {fields_str}. {notes_str}",
                True,
                f"We noticed a discrepancy with your {fields_str}. The document shows different details. Could you clarify or upload the correct invoice?",
            )

        # Customer provided claims, but key fields missing on document
        if missing_evidence:
            fields_str = ", ".join(missing_evidence)
            return (
                ReconciliationOutcome.MISSING_FROM_EVIDENCE,
                avg_conf,
                f"The document is missing the following details you mentioned: {fields_str}.",
                True,
                f"Your document does not clearly display the {fields_str}. Please upload an itemized invoice or receipt that includes this information.",
            )

        # All evaluated active claims matched
        match_count = sum(1 for c in comparisons if c.outcome == ReconciliationOutcome.MATCH)
        if match_count > 0 and not mismatched and not uncertain:
            return (
                ReconciliationOutcome.MATCH,
                avg_conf,
                f"All {match_count} verifiable claim(s) match the uploaded document.",
                False,
                None,
            )

        # If only MISSING_FROM_MESSAGE occurred (document had data, customer gave none)
        return (
            ReconciliationOutcome.MISSING_FROM_MESSAGE,
            avg_conf,
            "Document details were extracted, but customer provided no specific factual assertions to verify against.",
            False,
            None,
        )
