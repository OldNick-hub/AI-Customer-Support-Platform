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
import difflib
from enum import Enum
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger
from app.modules.evidence.claims import ClaimType, CustomerClaim, ExtractedClaimsPayload
from app.modules.evidence.schemas import (
    FinancialVerificationLevel,
    StructuredInvoiceData,
    TransactionIdentifierType,
)

logger = configure_safe_logger(logging.getLogger(__name__))


def classify_transaction_identifier(raw: Optional[str]) -> Tuple[Optional[str], TransactionIdentifierType]:
    """Preserves and classifies transaction identifier type from raw document reference strings."""
    if not raw:
        return None, TransactionIdentifierType.UNKNOWN
    cleaned = str(raw).strip().upper()
    norm = re.sub(r"[\s_-]", "", cleaned)
    if len(norm) < 3:
        return None, TransactionIdentifierType.UNKNOWN

    if any(cleaned.startswith(p) for p in ("AUTH", "APPR")):
        return norm, TransactionIdentifierType.AUTHORIZATION_CODE
    if any(cleaned.startswith(p) for p in ("TXN", "TRANS", "TX")):
        return norm, TransactionIdentifierType.TRANSACTION_ID
    if any(cleaned.startswith(p) for p in ("PAY", "REF")):
        return norm, TransactionIdentifierType.PAYMENT_REFERENCE
    if any(cleaned.startswith(p) for p in ("CHG",)):
        return norm, TransactionIdentifierType.CHARGE_ID
    if any(cleaned.startswith(p) for p in ("INV", "INVOICE")):
        return norm, TransactionIdentifierType.INVOICE_NUMBER
    if any(cleaned.startswith(p) for p in ("ORD", "ORDER")):
        return norm, TransactionIdentifierType.ORDER_ID

    return norm, TransactionIdentifierType.UNKNOWN


def are_identifier_types_compatible(t1: TransactionIdentifierType, t2: TransactionIdentifierType) -> bool:
    """Verifies that two identifiers have compatible types for proving two distinct transactions.

    Rules:
    1. Unknown or generic invoice/order numbers cannot prove distinct payment transactions.
    2. An authorization code (pre-settlement hold) cannot be paired with a transaction ID,
       charge ID, or payment reference, as they frequently represent different processing
       phases of the exact same underlying payment.
    3. Identifiers must share compatible payment reference classifications (e.g. TRANSACTION_ID
       with TRANSACTION_ID, or PAYMENT_REFERENCE with PAYMENT_REFERENCE).
    """
    if t1 in (TransactionIdentifierType.UNKNOWN, TransactionIdentifierType.INVOICE_NUMBER, TransactionIdentifierType.ORDER_ID):
        return False
    if t2 in (TransactionIdentifierType.UNKNOWN, TransactionIdentifierType.INVOICE_NUMBER, TransactionIdentifierType.ORDER_ID):
        return False

    # Authorization code vs transaction/charge/payment reference is fundamentally ambiguous
    if t1 == TransactionIdentifierType.AUTHORIZATION_CODE and t2 != TransactionIdentifierType.AUTHORIZATION_CODE:
        return False
    if t2 == TransactionIdentifierType.AUTHORIZATION_CODE and t1 != TransactionIdentifierType.AUTHORIZATION_CODE:
        return False

    return t1 == t2


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
    verification_level: FinancialVerificationLevel = FinancialVerificationLevel.EXTRACTED
    is_refund_authorized: bool = False
    requires_authoritative_verification: bool = True
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
        fuzzy_threshold: float = 0.75,
    ):
        self.amount_tolerance = amount_tolerance
        self.date_tolerance_days = date_tolerance_days
        self.confidence_threshold = confidence_threshold
        self.fuzzy_threshold = fuzzy_threshold

    def calculate_token_sort_similarity(self, s1: Optional[str], s2: Optional[str]) -> float:
        """Calculates deterministic token-sort similarity with abbreviation normalization."""
        if not s1 or not s2:
            return 0.0
        s1_clean = re.sub(r"[^\w\s]", " ", s1.lower()).strip()
        s2_clean = re.sub(r"[^\w\s]", " ", s2.lower()).strip()

        abbrevs = {
            r"\bcorp\b": "corporation",
            r"\binc\b": "incorporated",
            r"\bltd\b": "limited",
            r"\bco\b": "company",
            r"\bdept\b": "department",
            r"\bstr\b": "store",
            r"\bintl\b": "international",
            r"\bblk\b": "black",
            r"\bwht\b": "white",
            r"\bslv\b": "silver",
            r"\bhd\b": "headphones",
        }
        for pat, repl in abbrevs.items():
            s1_clean = re.sub(pat, repl, s1_clean)
            s2_clean = re.sub(pat, repl, s2_clean)

        tokens1 = sorted(set(s1_clean.split()))
        tokens2 = sorted(set(s2_clean.split()))

        set1, set2 = set(tokens1), set(tokens2)
        intersection = set1.intersection(set2)
        union = set1.union(set2)
        jaccard = len(intersection) / len(union) if union else 0.0

        str1_sorted = " ".join(tokens1)
        str2_sorted = " ".join(tokens2)
        seq_ratio = difflib.SequenceMatcher(None, str1_sorted, str2_sorted).ratio()

        # Ignore corporate entity suffixes for core token comparison if difference is solely entity suffix
        legal_suffixes = {"corporation", "incorporated", "limited", "company", "llc", "pvt"}
        core1 = set1 - legal_suffixes
        core2 = set2 - legal_suffixes
        if core1 and core2:
            core_intersection = core1.intersection(core2)
            core_union = core1.union(core2)
            core_jaccard = len(core_intersection) / len(core_union)
            overlap = len(core_intersection) / min(len(core1), len(core2))
        else:
            core_jaccard = 0.0
            overlap = len(intersection) / min(len(set1), len(set2)) if min(len(set1), len(set2)) > 0 else 0.0

        return max(jaccard, seq_ratio, core_jaccard, 0.95 if overlap >= 1.0 else 0.0)

    def reconcile(
        self,
        claims_payload: ExtractedClaimsPayload,
        evidence_data: Optional[StructuredInvoiceData],
        evidence_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        is_quarantined: bool = False,
        quarantine_reason: Optional[str] = None,
        secondary_evidence_data: Optional[StructuredInvoiceData] = None,
        secondary_evidence: Optional[StructuredInvoiceData] = None,
        secondary_evidence_id: Optional[str] = None,
        primary_file_hash: Optional[str] = None,
        secondary_file_hash: Optional[str] = None,
    ) -> ReconciliationResult:
        """Execute full field-by-field reconciliation between customer claims and evidence."""
        if secondary_evidence_data is None and secondary_evidence is not None:
            secondary_evidence_data = secondary_evidence

        # 0. Adversarial Customer Claim Gate
        if getattr(claims_payload, "is_adversarial", False):
            logger.warning(
                "Reconciliation aborted - adversarial customer claim: conv=%s reason=%s",
                conversation_id,
                claims_payload.quarantine_reason or "Customer claim contains adversarial directives",
            )
            return ReconciliationResult(
                overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                overall_confidence=0.0,
                summary=f"Reconciliation blocked due to security violation in customer claim: {claims_payload.quarantine_reason}",
                field_comparisons=[],
                mismatched_fields=[],
                missing_evidence_fields=[],
                uncertain_fields=[],
                requires_clarification=True,
                clarification_prompt="Your request could not be processed due to a security violation. Please state your claim without system commands or instructions.",
                is_quarantined_evidence=True,
                quarantine_reason=claims_payload.quarantine_reason or "Adversarial customer claim",
                evidence_id=evidence_id,
                conversation_id=conversation_id,
                customer_id=customer_id,
                message_id=claims_payload.message_id,
            )

        # 1. Quarantined / Malicious Evidence Gate
        sec_malicious = secondary_evidence_data and getattr(secondary_evidence_data, "is_malicious", False)
        if is_quarantined or (evidence_data and getattr(evidence_data, "is_malicious", False)) or sec_malicious:
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

        # 3. Inter-Document Contradiction Gate (EVIDENCE_EVIDENCE_CONFLICT)
        if secondary_evidence_data and not claims_payload.intent_double_charge:
            conflicts = []
            if evidence_data.order_id and secondary_evidence_data.order_id and evidence_data.order_id != secondary_evidence_data.order_id:
                conflicts.append(f"order ID ('{evidence_data.order_id}' vs '{secondary_evidence_data.order_id}')")
            if evidence_data.total_amount is not None and secondary_evidence_data.total_amount is not None:
                if abs(evidence_data.total_amount - secondary_evidence_data.total_amount) > self.amount_tolerance:
                    conflicts.append(f"total amount ({evidence_data.total_amount} vs {secondary_evidence_data.total_amount})")
            if evidence_data.currency and secondary_evidence_data.currency and evidence_data.currency.upper() != secondary_evidence_data.currency.upper():
                conflicts.append(f"currency ({evidence_data.currency} vs {secondary_evidence_data.currency})")
            if conflicts:
                summary_txt = f"Contradictory information detected between uploaded documents (EVIDENCE_EVIDENCE_CONFLICT): {'; '.join(conflicts)}."
                return ReconciliationResult(
                    overall_outcome=ReconciliationOutcome.MISMATCH,
                    overall_confidence=0.85,
                    summary=summary_txt,
                    field_comparisons=[],
                    mismatched_fields=["secondary_evidence"],
                    missing_evidence_fields=[],
                    uncertain_fields=[],
                    requires_clarification=True,
                    clarification_prompt=f"Your uploaded documents contain contradictory details for {'; '.join(conflicts)}. Please clarify which document is authoritative.",
                    evidence_id=evidence_id,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    message_id=claims_payload.message_id,
                )

        # 4. Check Intent: Charged Twice / Double Charge
        has_valid_second_tx = False
        if claims_payload.intent_double_charge:
            if not secondary_evidence_data:
                intent_comp = FieldComparisonResult(
                    field_name="intent_double_charge",
                    customer_claim="Charged twice for transaction",
                    evidence_value=f"Single invoice {evidence_data.invoice_number or evidence_data.order_id or 'provided'}",
                    outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                    confidence=1.0,
                    notes="Single invoice verifies only one transaction. Proof of duplicate deduction is required.",
                )
                field_comparisons.append(intent_comp)
            else:
                # Rule 1 & 2: Identical file hashes cannot prove two transactions
                h1 = primary_file_hash or getattr(evidence_data, "sha256_hash", None)
                h2 = secondary_file_hash or getattr(secondary_evidence_data, "sha256_hash", None)
                if h1 and h2 and h1 == h2:
                    dup_comp = FieldComparisonResult(
                        field_name="secondary_evidence",
                        customer_claim="Proof of two distinct transactions",
                        evidence_value="Duplicate copy of primary evidence file",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=1.0,
                        notes="Duplicate copies of the same receipt cannot prove two distinct transactions (DUPLICATE_EVIDENCE_FILE).",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        overall_confidence=0.90,
                        summary="Duplicate copies of the same receipt cannot prove two distinct transactions.",
                        field_comparisons=[dup_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["distinct_second_transaction"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="Both uploaded files are identical duplicates. Please provide proof of two separate, distinct debits.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # ------------------------------------------------------------------
                # Strict Transaction Identity Validation (Rules 1-10)
                # Preserves identifier typing and enforces type compatibility
                # ------------------------------------------------------------------
                ord1_norm, ord1_type = classify_transaction_identifier(evidence_data.order_id)
                ord2_norm, ord2_type = classify_transaction_identifier(secondary_evidence_data.order_id)
                inv1_norm, inv1_type = classify_transaction_identifier(evidence_data.invoice_number)
                inv2_norm, inv2_type = classify_transaction_identifier(secondary_evidence_data.invoice_number)

                tx1_norm, type1 = inv1_norm, inv1_type
                if type1 in (TransactionIdentifierType.UNKNOWN, TransactionIdentifierType.INVOICE_NUMBER) and ord1_type in (
                    TransactionIdentifierType.TRANSACTION_ID,
                    TransactionIdentifierType.AUTHORIZATION_CODE,
                    TransactionIdentifierType.PAYMENT_REFERENCE,
                    TransactionIdentifierType.CHARGE_ID,
                ):
                    tx1_norm, type1 = ord1_norm, ord1_type

                tx2_norm, type2 = inv2_norm, inv2_type
                if type2 in (TransactionIdentifierType.UNKNOWN, TransactionIdentifierType.INVOICE_NUMBER) and ord2_type in (
                    TransactionIdentifierType.TRANSACTION_ID,
                    TransactionIdentifierType.AUTHORIZATION_CODE,
                    TransactionIdentifierType.PAYMENT_REFERENCE,
                    TransactionIdentifierType.CHARGE_ID,
                ):
                    tx2_norm, type2 = ord2_norm, ord2_type

                has_tx1 = type1 in (
                    TransactionIdentifierType.TRANSACTION_ID,
                    TransactionIdentifierType.AUTHORIZATION_CODE,
                    TransactionIdentifierType.PAYMENT_REFERENCE,
                    TransactionIdentifierType.CHARGE_ID,
                )
                has_tx2 = type2 in (
                    TransactionIdentifierType.TRANSACTION_ID,
                    TransactionIdentifierType.AUTHORIZATION_CODE,
                    TransactionIdentifierType.PAYMENT_REFERENCE,
                    TransactionIdentifierType.CHARGE_ID,
                )

                has_any_id1 = bool(ord1_norm or inv1_norm)
                has_any_id2 = bool(ord2_norm or inv2_norm)

                # Identifier Type Incompatibility Check:
                # An authorization code (AUTH-) and a transaction ID (TXN-) represent different processing facets
                # of the same payment (pre-auth hold vs settlement) and CANNOT establish distinct payments!
                if has_tx1 and has_tx2 and not are_identifier_types_compatible(type1, type2):
                    type_mismatch_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value=f"Incompatible identifier types: '{type1.value}' ({evidence_data.invoice_number or evidence_data.order_id}) vs '{type2.value}' ({secondary_evidence_data.invoice_number or secondary_evidence_data.order_id})",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=0.95,
                        notes=(
                            f"Uploaded documents specify incompatible identifier types ({type1.value} vs {type2.value}). "
                            "An authorization code and a transaction reference represent different payment processing facets "
                            "and cannot substantiate two separate payments without authoritative settlement confirmation."
                        ),
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        verification_level=FinancialVerificationLevel.EXTRACTED,
                        is_refund_authorized=False,
                        requires_authoritative_verification=True,
                        overall_confidence=0.85,
                        summary=(
                            f"IDENTIFIER_TYPE_INCOMPATIBILITY: Document 1 specifies {type1.value} while Document 2 specifies {type2.value}. "
                            "An authorization code and a transaction reference cannot substantiate two separate payments."
                        ),
                        field_comparisons=[type_mismatch_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["compatible_transaction_identifiers"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt=(
                            "The uploaded documents show an authorization code and a transaction reference. "
                            "These may represent the same charge. Please provide account statements showing two distinct settled debits."
                        ),
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Rule 4: Missing transaction identifiers on either document
                if not has_any_id1 and not has_any_id2:
                    tx_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value="No transaction or order identifiers found on either document",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=1.0,
                        notes="Neither document contains a verifiable order ID or transaction reference. Proof of two distinct charges requires independently identifiable transaction records.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        overall_confidence=0.90,
                        summary="INSUFFICIENT_TRANSACTION_EVIDENCE: Neither uploaded document contains a verifiable transaction reference or order ID.",
                        field_comparisons=[tx_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["primary_transaction_id", "secondary_transaction_id"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="Neither document contains an order ID or transaction reference. Please provide receipts or bank statements with distinct transaction IDs.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                if not has_any_id1 or not has_any_id2:
                    missing_side = "primary" if not has_any_id1 else "secondary"
                    tx_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value=f"Transaction identifier missing on {missing_side} document",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=1.0,
                        notes=f"{missing_side.capitalize()} evidence document lacks a verifiable transaction identifier. Both documents must provide distinct, verifiable transaction references.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        overall_confidence=0.85,
                        summary=f"INSUFFICIENT_TRANSACTION_EVIDENCE: The {missing_side} document lacks a verifiable transaction identifier.",
                        field_comparisons=[tx_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=[f"{missing_side}_transaction_id"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt=f"The {missing_side} document does not show a clear transaction ID. Please upload a document with a visible order or transaction reference.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Rule 2 & 3: Identical transaction references / Different documents referencing same transaction
                same_invoice = bool(inv1_norm and inv2_norm and inv1_norm == inv2_norm)
                same_order = bool(ord1_norm and ord2_norm and ord1_norm == ord2_norm)
                cross_match = bool(
                    (ord1_norm and inv2_norm and ord1_norm == inv2_norm)
                    or (inv1_norm and ord2_norm and inv1_norm == ord2_norm)
                )

                # Rule 5: Different invoice numbers alone must not automatically establish distinct transactions
                # unless the invoice references are demonstrably valid transaction identifiers under the domain model.
                distinct_invoices_only = bool(
                    inv1_norm and inv2_norm and inv1_norm != inv2_norm
                    and not (has_tx1 and has_tx2)
                    and (same_order or (not ord1_norm and not ord2_norm))
                )

                if distinct_invoices_only:
                    tx_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value=f"Invoice numbers '{evidence_data.invoice_number}' vs '{secondary_evidence_data.invoice_number}' without independent transaction reference",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=0.90,
                        notes="Distinct invoice numbers alone do not establish independent financial transactions without verified transaction identifiers (e.g. TXN/AUTH codes or distinct order IDs).",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        overall_confidence=0.85,
                        summary="INSUFFICIENT_TRANSACTION_EVIDENCE: Uploaded documents show different invoice numbers but no independent transaction or payment references. Distinct financial transactions could not be verified.",
                        field_comparisons=[tx_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["distinct_payment_transaction_id"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="Different invoice numbers for the same order do not prove two debits. Please provide bank statements or payment receipts showing distinct transaction IDs.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                is_identical_reference = (
                    (same_invoice and not (has_tx1 and has_tx2 and inv1_norm != inv2_norm))
                    or (same_order and not (has_tx1 and has_tx2 and inv1_norm != inv2_norm))
                    or cross_match
                )

                if is_identical_reference and not (has_tx1 and has_tx2 and inv1_norm != inv2_norm):
                    shared_ref = (
                        evidence_data.invoice_number
                        if same_invoice
                        else (evidence_data.order_id or "reference")
                    )
                    tx_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value=f"Identical transaction identifier '{shared_ref}' found on both documents",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=0.95,
                        notes="Identical transaction identifiers found on both files. Two distinct transactions could not be verified.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        overall_confidence=0.85,
                        summary="INSUFFICIENT_DISTINCT_TRANSACTION_EVIDENCE: Both uploaded documents reference the exact same transaction ID and date. Distinct transaction identity could not be established.",
                        field_comparisons=[tx_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["distinct_second_transaction"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="Both uploaded documents reference the same transaction. Please upload an account statement showing two distinct debits.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Rule 8: Reject different order IDs as proof of a double charge.
                # Two different orders represent two separate purchases, not duplicate charges for the same purchase.
                if ord1_norm and ord2_norm and ord1_norm != ord2_norm:
                    order_diff_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Charged twice for the same order",
                        evidence_value=f"Different orders: '{evidence_data.order_id}' vs '{secondary_evidence_data.order_id}'",
                        outcome=ReconciliationOutcome.MISMATCH,
                        confidence=0.95,
                        notes=(
                            f"Uploaded documents specify two different order IDs ('{evidence_data.order_id}' vs '{secondary_evidence_data.order_id}'). "
                            "Two different orders represent separate purchases, not duplicate charges for the same purchase."
                        ),
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.MISMATCH,
                        overall_confidence=0.90,
                        summary="DIFFERENT_ORDERS_DETECTED: Documents reference two distinct orders. Two separate orders cannot substantiate a double charge for the same purchase.",
                        field_comparisons=[order_diff_comp],
                        mismatched_fields=["order_id"],
                        missing_evidence_fields=[],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="The uploaded documents belong to two separate orders. If you meant to dispute two charges for the same order, please upload two payment proofs for that single order.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Rule 4: Verify amount and currency consistency between transactions
                if (
                    evidence_data.total_amount is not None
                    and secondary_evidence_data.total_amount is not None
                    and abs(evidence_data.total_amount - secondary_evidence_data.total_amount) > self.amount_tolerance
                ):
                    amt_diff_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Charged twice for the same amount",
                        evidence_value=f"Conflicting amounts: {evidence_data.total_amount} vs {secondary_evidence_data.total_amount}",
                        outcome=ReconciliationOutcome.MISMATCH,
                        confidence=0.95,
                        notes=f"Uploaded documents specify conflicting transaction amounts ({evidence_data.total_amount} vs {secondary_evidence_data.total_amount}). A duplicate charge requires consistent transaction amounts.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.MISMATCH,
                        overall_confidence=0.90,
                        summary="INTER_DOCUMENT_AMOUNT_MISMATCH: The two uploaded payment proofs show conflicting deduction amounts.",
                        field_comparisons=[amt_diff_comp],
                        mismatched_fields=["total_amount"],
                        missing_evidence_fields=[],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="The two uploaded documents show different charge amounts. Please clarify the disputed transaction amount.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                if (
                    evidence_data.currency
                    and secondary_evidence_data.currency
                    and evidence_data.currency.upper() != secondary_evidence_data.currency.upper()
                ):
                    curr_diff_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Charged twice in the same currency",
                        evidence_value=f"Conflicting currencies: {evidence_data.currency} vs {secondary_evidence_data.currency}",
                        outcome=ReconciliationOutcome.MISMATCH,
                        confidence=1.0,
                        notes=f"Uploaded documents specify conflicting currencies ({evidence_data.currency} vs {secondary_evidence_data.currency}). A duplicate charge requires consistent currencies.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.MISMATCH,
                        overall_confidence=0.90,
                        summary="INTER_DOCUMENT_CURRENCY_MISMATCH: The two uploaded payment proofs show conflicting currencies.",
                        field_comparisons=[curr_diff_comp],
                        mismatched_fields=["currency"],
                        missing_evidence_fields=[],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="The two uploaded documents show different currencies. Please clarify which currency was charged.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Require two distinct, verifiable payment transaction references of compatible type (Rules 2, 8, 9, 10)
                has_distinct_explicit_tx = bool(
                    has_tx1 and has_tx2 and are_identifier_types_compatible(type1, type2) and inv1_norm != inv2_norm
                )

                if not has_distinct_explicit_tx:
                    tx_comp = FieldComparisonResult(
                        field_name="distinct_transaction_id",
                        customer_claim="Two distinct transactions",
                        evidence_value="No distinct verified payment transaction references found",
                        outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        confidence=0.90,
                        notes="Two independently verifiable payment transaction references (e.g. TXN/PAY reference codes) of compatible type for the disputed order were not found. Distinct financial transactions could not be confirmed.",
                    )
                    return ReconciliationResult(
                        overall_outcome=ReconciliationOutcome.INSUFFICIENT_EVIDENCE,
                        verification_level=FinancialVerificationLevel.EXTRACTED,
                        is_refund_authorized=False,
                        requires_authoritative_verification=True,
                        overall_confidence=0.85,
                        summary="INSUFFICIENT_DISTINCT_TRANSACTION_EVIDENCE: Disputed order lacks two independent payment transaction references.",
                        field_comparisons=[tx_comp],
                        mismatched_fields=[],
                        missing_evidence_fields=["distinct_payment_transaction_id"],
                        uncertain_fields=[],
                        requires_clarification=True,
                        clarification_prompt="To verify a duplicate charge for this order, please provide two payment proofs (e.g. bank statement entries or card transaction IDs) showing separate debits.",
                        evidence_id=evidence_id,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        message_id=claims_payload.message_id,
                    )

                # Distinct transaction identity corroborated across submitted documents!
                has_valid_second_tx = True
                tx1_label = evidence_data.invoice_number or evidence_data.order_id or "TX1"
                tx2_label = secondary_evidence_data.invoice_number or secondary_evidence_data.order_id or "TX2"
                intent_comp = FieldComparisonResult(
                    field_name="intent_double_charge",
                    customer_claim="Charged twice for transaction",
                    evidence_value=f"Two distinct transactions corroborated ({type1.value}): {tx1_label} and {tx2_label}",
                    outcome=ReconciliationOutcome.MATCH,
                    confidence=0.95,
                    notes="Verified distinct second transaction proofs across submitted documents (Document-Corroborated). Payment gateway verification required before refund authorization.",
                )
                field_comparisons.append(intent_comp)

        # 5. Compare Order ID / Invoice Number
        order_comp = self._compare_order_id(claims_payload, evidence_data)
        if order_comp:
            field_comparisons.append(order_comp)
            self._categorize_field(order_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 6. Compare Currency & Amount
        currency_comp, amount_comp = self._compare_amount_and_currency(claims_payload, evidence_data)
        if currency_comp:
            field_comparisons.append(currency_comp)
            self._categorize_field(currency_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        if amount_comp:
            field_comparisons.append(amount_comp)
            self._categorize_field(amount_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 7. Compare Date
        date_comp = self._compare_date(claims_payload, evidence_data)
        if date_comp:
            field_comparisons.append(date_comp)
            self._categorize_field(date_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 8. Compare Merchant / Store Name
        merchant_comp = self._compare_merchant(claims_payload, evidence_data)
        if merchant_comp:
            field_comparisons.append(merchant_comp)
            self._categorize_field(merchant_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 9. Compare Product Name / Quantity
        product_comp = self._compare_product(claims_payload, evidence_data)
        if product_comp:
            field_comparisons.append(product_comp)
            self._categorize_field(product_comp, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 10. Compare Line Items if available
        line_item_comps = self._compare_line_items(claims_payload, evidence_data)
        for lic in line_item_comps:
            field_comparisons.append(lic)
            self._categorize_field(lic, mismatched_fields, missing_evidence_fields, uncertain_fields)

        # 11. Compute Overall Verdict & Confidence
        overall_outcome, overall_confidence, summary, requires_clarification, prompt = (
            self._derive_overall_verdict(
                claims_payload,
                evidence_data,
                field_comparisons,
                mismatched_fields,
                missing_evidence_fields,
                uncertain_fields,
                has_valid_second_tx=has_valid_second_tx,
            )
        )

        # Determine Trust Hierarchy Verification Level
        if secondary_evidence_data and overall_outcome == ReconciliationOutcome.MATCH:
            verification_level = FinancialVerificationLevel.DOCUMENT_CORROBORATED
            summary = (
                f"DOCUMENT_CORROBORATED_MATCH: {summary} "
                "(Note: Document-level corroboration does not establish bank settlement and cannot authorize refunds without payment provider confirmation.)"
            )
        else:
            verification_level = FinancialVerificationLevel.EXTRACTED

        return ReconciliationResult(
            overall_outcome=overall_outcome,
            verification_level=verification_level,
            is_refund_authorized=False,  # Financial safeguard: Document corroboration alone NEVER authorizes refunds
            requires_authoritative_verification=True,
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
                confidence=1.0,
                notes=f"Merchant matches ({evidence_merchant}).",
            )

        similarity = self.calculate_token_sort_similarity(claimed_merchant, evidence_merchant)
        if similarity >= self.fuzzy_threshold:
            return FieldComparisonResult(
                field_name="merchant_name",
                customer_claim=claimed_merchant,
                evidence_value=evidence_merchant,
                outcome=ReconciliationOutcome.MATCH,
                confidence=similarity,
                notes=f"Merchant matches via fuzzy similarity ({evidence_merchant}, score={similarity:.2f}).",
            )

        return FieldComparisonResult(
            field_name="merchant_name",
            customer_claim=claimed_merchant,
            evidence_value=evidence_merchant,
            outcome=ReconciliationOutcome.MISMATCH,
            confidence=max(0.5, similarity),
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

        # Check fuzzy token sort similarity
        similarity = self.calculate_token_sort_similarity(claimed_prod, evidence_prod)
        if similarity >= self.fuzzy_threshold:
            return FieldComparisonResult(
                field_name="product_name",
                customer_claim=claimed_prod,
                evidence_value=evidence_prod,
                outcome=ReconciliationOutcome.MATCH,
                confidence=similarity,
                notes=f"Product description matches document item (similarity={similarity:.2f}).",
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
                confidence=0.80,
                notes="Product description matches document item.",
            )

        return FieldComparisonResult(
            field_name="product_name",
            customer_claim=claimed_prod,
            evidence_value=evidence_prod,
            outcome=ReconciliationOutcome.MISMATCH,
            confidence=max(0.5, similarity),
            notes=f"Claimed product '{claimed_prod}' does not match document product '{evidence_prod}'.",
        )

    def _compare_line_items(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
    ) -> List[FieldComparisonResult]:
        """Compares specific product line items if present on the document."""
        results: List[FieldComparisonResult] = []
        if not evidence.line_items or not claims.primary_product:
            return results

        claimed_prod = claims.primary_product
        claimed_qty = claims.primary_quantity

        for item in evidence.line_items:
            sim = self.calculate_token_sort_similarity(claimed_prod, item.product_name)
            if sim >= self.fuzzy_threshold:
                notes = f"Matched line item '{item.product_name}' (qty={item.quantity}, price={item.total_price or item.unit_price})"
                matched_outcome = ReconciliationOutcome.MATCH
                diff_amount = None

                # Check quantity if claimed
                if claimed_qty is not None and item.quantity is not None:
                    if abs(claimed_qty - item.quantity) > 0.001:
                        matched_outcome = ReconciliationOutcome.MISMATCH
                        diff_amount = abs(claimed_qty - item.quantity)
                        notes = f"Line item '{item.product_name}' quantity mismatch: claimed {claimed_qty}, document shows {item.quantity}"

                results.append(
                    FieldComparisonResult(
                        field_name=f"line_item:{item.product_name[:30]}",
                        customer_claim=f"{claimed_prod} (qty={claimed_qty})" if claimed_qty else claimed_prod,
                        evidence_value=f"{item.product_name} (qty={item.quantity})",
                        outcome=matched_outcome,
                        difference_amount=diff_amount,
                        difference_unit="units" if diff_amount else None,
                        confidence=sim,
                        notes=notes,
                    )
                )

        return results

    def _derive_overall_verdict(
        self,
        claims: ExtractedClaimsPayload,
        evidence: StructuredInvoiceData,
        comparisons: List[FieldComparisonResult],
        mismatched: List[str],
        missing_evidence: List[str],
        uncertain: List[str],
        has_valid_second_tx: bool = False,
    ) -> Tuple[ReconciliationOutcome, float, str, bool, Optional[str]]:
        """Calculate overall outcome, confidence, summary text, and clarification needs."""

        # Complex intent double charge with single document
        if claims.intent_double_charge and not has_valid_second_tx:
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

