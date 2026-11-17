"""Customer Conversational Claim Extraction Engine.

Extracts structured claims from customer chat utterances:
- Order IDs & Invoice numbers
- Payment & Refund amounts
- Currencies (INR, USD, EUR, GBP, etc.)
- Transaction & Invoice dates
- Product names, quantities & merchants
- Intent flags (e.g. duplicate billing, charged twice)
"""

from datetime import date, datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pydantic import BaseModel, Field

from app.core.pii import PIIMaskingEngine


class ClaimType(str, Enum):
    """Categorization of verifiable customer claim elements."""
    ORDER_ID = "ORDER_ID"
    INVOICE_NUMBER = "INVOICE_NUMBER"
    TRANSACTION_DATE = "TRANSACTION_DATE"
    INVOICE_DATE = "INVOICE_DATE"
    PAYMENT_AMOUNT = "PAYMENT_AMOUNT"
    REFUND_AMOUNT = "REFUND_AMOUNT"
    CURRENCY = "CURRENCY"
    PRODUCT_NAME = "PRODUCT_NAME"
    PRODUCT_QUANTITY = "PRODUCT_QUANTITY"
    MERCHANT_NAME = "MERCHANT_NAME"
    TRANSACTION_REFERENCE = "TRANSACTION_REFERENCE"
    INTENT_DOUBLE_CHARGE = "INTENT_DOUBLE_CHARGE"


class CustomerClaim(BaseModel):
    """Single atomic factual assertion extracted from a customer message."""

    claim_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message_id: Optional[str] = None
    claim_type: ClaimType
    original_value: str
    normalized_value: Any
    confidence: float = Field(..., ge=0.0, le=1.0)
    extraction_source: str = "regex_heuristics"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_uncertain: bool = False
    uncertainty_reason: Optional[str] = None


class ExtractedClaimsPayload(BaseModel):
    """Structured collection of customer claims extracted from an utterance."""

    message_id: Optional[str] = None
    raw_text: str
    pii_masked_text: str
    claims: List[CustomerClaim] = Field(default_factory=list)
    primary_order_id: Optional[str] = None
    primary_invoice_number: Optional[str] = None
    primary_amount: Optional[float] = None
    primary_currency: Optional[str] = None
    primary_date: Optional[date] = None
    primary_merchant: Optional[str] = None
    primary_product: Optional[str] = None
    primary_quantity: Optional[float] = None
    intent_double_charge: bool = False
    is_adversarial: bool = False
    quarantine_reason: Optional[str] = None
    security_flags: List[str] = Field(default_factory=list)


class ClaimExtractionEngine:
    """Extracts verifiable claims from customer natural language messages."""

    def __init__(self, security_guard: Optional[Any] = None):
        from app.modules.evidence.security_guard import AdversarialEvidenceGuard
        self.security_guard = security_guard or AdversarialEvidenceGuard()


    # Currencies
    CURRENCY_SYMBOLS: Dict[str, str] = {
        "₹": "INR",
        "rs": "INR",
        "rs.": "INR",
        "rupees": "INR",
        "$": "USD",
        "dollars": "USD",
        "usd": "USD",
        "€": "EUR",
        "euros": "EUR",
        "eur": "EUR",
        "£": "GBP",
        "pounds": "GBP",
        "gbp": "GBP",
        "¥": "JPY",
        "yen": "JPY",
        "jpy": "JPY",
        "c$": "CAD",
        "a$": "AUD",
    }

    # Order ID Patterns
    ORDER_ID_PATTERNS = [
        re.compile(r"\b(?:order|ord)(?:[\s_-]*(?:id|#|number|no))?[:\s#]*([A-Z0-9-]{4,32})\b", re.IGNORECASE),
        re.compile(r"\b(ORD-[A-Z0-9]{4,20})\b", re.IGNORECASE),
        re.compile(r"\b(ORDER-[A-Z0-9]{4,20})\b", re.IGNORECASE),
    ]

    # Invoice Patterns
    INVOICE_PATTERNS = [
        re.compile(r"\b(?:invoice|inv)(?:[\s_-]*(?:id|#|number|no))?[:\s#]*([A-Z0-9-]{4,32})\b", re.IGNORECASE),
        re.compile(r"\b(INV-[A-Z0-9]{4,20})\b", re.IGNORECASE),
    ]

    # Double Charge / Duplicate billing intent
    DOUBLE_CHARGE_PATTERN = re.compile(
        r"\b(?:charged\s*twice|double\s*charge|charged\s*two\s*times|billed\s*twice|duplicate\s*charge|charged\s*again)\b",
        re.IGNORECASE,
    )

    # Date Patterns
    DATE_PATTERNS = [
        re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
        re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"),
        re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b", re.IGNORECASE),
    ]

    MONTH_MAP = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }

    def extract_claims(self, text: str, message_id: Optional[str] = None) -> ExtractedClaimsPayload:
        """Parses customer utterance into discrete, typed claims."""
        if not text or not text.strip():
            return ExtractedClaimsPayload(
                message_id=message_id,
                raw_text="",
                pii_masked_text="",
            )

        pii_masked = PIIMaskingEngine.mask(text)
        claims: List[CustomerClaim] = []

        # 1. Order ID
        order_id_claim = self._extract_order_id(text, message_id)
        if order_id_claim:
            claims.append(order_id_claim)

        # 2. Invoice Number
        invoice_claim = self._extract_invoice_number(text, message_id)
        if invoice_claim:
            claims.append(invoice_claim)

        # 3. Currency & Amounts
        amount_claims, curr_claim = self._extract_amounts_and_currency(text, message_id)
        claims.extend(amount_claims)
        if curr_claim:
            claims.append(curr_claim)

        # 4. Dates
        date_claim = self._extract_dates(text, message_id)
        if date_claim:
            claims.append(date_claim)

        # 5. Quantities
        qty_claim = self._extract_quantity(text, message_id)
        if qty_claim:
            claims.append(qty_claim)

        # 6. Intent: Charged Twice
        is_double_charge = bool(self.DOUBLE_CHARGE_PATTERN.search(text))
        if is_double_charge:
            claims.append(
                CustomerClaim(
                    message_id=message_id,
                    claim_type=ClaimType.INTENT_DOUBLE_CHARGE,
                    original_value="charged twice",
                    normalized_value=True,
                    confidence=0.95,
                    extraction_source="regex_intent",
                )
            )

        # Extract primary values for quick indexing
        primary_order = order_id_claim.normalized_value if order_id_claim else None
        primary_inv = invoice_claim.normalized_value if invoice_claim else None
        primary_curr = curr_claim.normalized_value if curr_claim else None
        primary_amt = None
        for ac in amount_claims:
            if ac.claim_type == ClaimType.PAYMENT_AMOUNT:
                primary_amt = ac.normalized_value
                break
        if primary_amt is None and amount_claims:
            primary_amt = amount_claims[0].normalized_value

        primary_dt = date_claim.normalized_value if date_claim else None
        primary_qty = qty_claim.normalized_value if qty_claim else None

        # 7. Customer Utterance Security Scan
        sec_res = self.security_guard.scan_customer_claim(text)
        is_adversarial = sec_res.is_malicious
        quarantine_reason = sec_res.quarantine_reason
        security_flags = [f.rule_name for f in sec_res.findings]

        return ExtractedClaimsPayload(
            message_id=message_id,
            raw_text=text,
            pii_masked_text=pii_masked,
            claims=claims,
            primary_order_id=primary_order,
            primary_invoice_number=primary_inv,
            primary_amount=primary_amt,
            primary_currency=primary_curr,
            primary_date=primary_dt,
            primary_quantity=primary_qty,
            intent_double_charge=is_double_charge,
            is_adversarial=is_adversarial,
            quarantine_reason=quarantine_reason,
            security_flags=security_flags,
        )

    def _extract_order_id(self, text: str, message_id: Optional[str]) -> Optional[CustomerClaim]:
        for pattern in self.ORDER_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                raw_val = match.group(1).strip()
                # Exclude false positives like "order", "today", "yesterday"
                if raw_val.lower() in ("id", "number", "no", "order", "item", "status"):
                    continue
                return CustomerClaim(
                    message_id=message_id,
                    claim_type=ClaimType.ORDER_ID,
                    original_value=raw_val,
                    normalized_value=raw_val.upper(),
                    confidence=0.95,
                )
        return None

    def _extract_invoice_number(self, text: str, message_id: Optional[str]) -> Optional[CustomerClaim]:
        for pattern in self.INVOICE_PATTERNS:
            match = pattern.search(text)
            if match:
                raw_val = match.group(1).strip()
                if raw_val.lower() in ("id", "number", "no", "invoice"):
                    continue
                return CustomerClaim(
                    message_id=message_id,
                    claim_type=ClaimType.INVOICE_NUMBER,
                    original_value=raw_val,
                    normalized_value=raw_val.upper(),
                    confidence=0.92,
                )
        return None

    def _extract_amounts_and_currency(
        self, text: str, message_id: Optional[str]
    ) -> Tuple[List[CustomerClaim], Optional[CustomerClaim]]:
        claims: List[CustomerClaim] = []
        detected_currency: Optional[str] = None
        currency_claim: Optional[CustomerClaim] = None

        # 1. Look for currency words or symbols
        for sym, code in self.CURRENCY_SYMBOLS.items():
            if sym in text.lower():
                detected_currency = code
                currency_claim = CustomerClaim(
                    message_id=message_id,
                    claim_type=ClaimType.CURRENCY,
                    original_value=sym,
                    normalized_value=code,
                    confidence=0.90,
                )
                break

        # 2. Match refund amounts specifically
        refund_regex = re.compile(
            r"\brefund(?:\s*(?:of|for))?[:\s]*([$€£₹A-Za-z]{0,4})\s*([0-9,]+(?:\.[0-9]{1,2})?)\b",
            re.IGNORECASE,
        )
        for match in refund_regex.finditer(text):
            sym = match.group(1).strip()
            num_str = match.group(2).replace(",", "")
            try:
                val = float(num_str)
                claims.append(
                    CustomerClaim(
                        message_id=message_id,
                        claim_type=ClaimType.REFUND_AMOUNT,
                        original_value=match.group(0),
                        normalized_value=val,
                        confidence=0.90,
                    )
                )
                if not detected_currency and sym:
                    detected_currency = self.CURRENCY_SYMBOLS.get(sym.lower())
            except ValueError:
                pass

        # 3. Match payment / charge amounts
        amt_regex = re.compile(
            r"(?:paid|charged|amount|cost|price|bill|total|spent|pay|fee|was)[:\s]*([$€£₹A-Za-z]{0,4})\s*([0-9,]+(?:\.[0-9]{1,2})?)\b",
            re.IGNORECASE,
        )
        for match in amt_regex.finditer(text):
            sym = match.group(1).strip()
            num_str = match.group(2).replace(",", "")
            try:
                val = float(num_str)
                claims.append(
                    CustomerClaim(
                        message_id=message_id,
                        claim_type=ClaimType.PAYMENT_AMOUNT,
                        original_value=match.group(0),
                        normalized_value=val,
                        confidence=0.92,
                    )
                )
                if not detected_currency and sym:
                    detected_currency = self.CURRENCY_SYMBOLS.get(sym.lower())
            except ValueError:
                pass

        # 4. Fallback: match standalone amounts with symbol (e.g. "₹2,499" or "$150")
        if not claims:
            standalone_regex = re.compile(
                r"([$€£₹])\s*([0-9,]+(?:\.[0-9]{1,2})?)\b"
            )
            for match in standalone_regex.finditer(text):
                sym = match.group(1)
                num_str = match.group(2).replace(",", "")
                try:
                    val = float(num_str)
                    claims.append(
                        CustomerClaim(
                            message_id=message_id,
                            claim_type=ClaimType.PAYMENT_AMOUNT,
                            original_value=match.group(0),
                            normalized_value=val,
                            confidence=0.88,
                        )
                    )
                    if not detected_currency:
                        detected_currency = self.CURRENCY_SYMBOLS.get(sym.lower())
                except ValueError:
                    pass

        # If detected_currency was found, update or ensure currency claim
        if detected_currency and not currency_claim:
            currency_claim = CustomerClaim(
                message_id=message_id,
                claim_type=ClaimType.CURRENCY,
                original_value=detected_currency,
                normalized_value=detected_currency,
                confidence=0.90,
            )

        return claims, currency_claim

    def _extract_dates(self, text: str, message_id: Optional[str]) -> Optional[CustomerClaim]:
        for pattern in self.DATE_PATTERNS:
            for match in pattern.finditer(text):
                groups = match.groups()
                try:
                    if len(groups) == 3 and groups[0].lower() in self.MONTH_MAP:
                        month = self.MONTH_MAP[groups[0].lower()]
                        day = int(groups[1])
                        year = int(groups[2])
                        dt = date(year, month, day)
                    elif len(groups) == 3 and len(groups[0]) == 4:
                        dt = date(int(groups[0]), int(groups[1]), int(groups[2]))
                    elif len(groups) == 3 and len(groups[2]) == 4:
                        dt = date(int(groups[2]), int(groups[1]), int(groups[0]))
                    else:
                        continue

                    return CustomerClaim(
                        message_id=message_id,
                        claim_type=ClaimType.TRANSACTION_DATE,
                        original_value=match.group(0),
                        normalized_value=dt,
                        confidence=0.90,
                    )
                except ValueError:
                    continue
        return None

    def _extract_quantity(self, text: str, message_id: Optional[str]) -> Optional[CustomerClaim]:
        qty_regex = re.compile(r"\b(\d+)\s*(?:items|units|pcs|pieces|quantity|qty)\b", re.IGNORECASE)
        match = qty_regex.search(text)
        if match:
            try:
                qty = float(match.group(1))
                return CustomerClaim(
                    message_id=message_id,
                    claim_type=ClaimType.PRODUCT_QUANTITY,
                    original_value=match.group(0),
                    normalized_value=qty,
                    confidence=0.85,
                )
            except ValueError:
                pass
        return None
