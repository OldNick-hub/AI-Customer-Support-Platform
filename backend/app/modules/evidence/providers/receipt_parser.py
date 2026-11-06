"""Rule-Based Invoice and Receipt Parsing Engine.

Extracts structured fields from raw document text using normalized regex patterns,
financial heuristics, date parsers, and field-level confidence scoring.
"""

from datetime import date, datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from app.modules.evidence.schemas import (
    PageExtractionMetadata,
    ReceiptLineItem,
    StructuredInvoiceData,
)


class RuleBasedReceiptParser:
    """Parses invoices, receipts, and order confirmation documents."""

    # Field-specific Regex Patterns
    ORDER_ID_PATTERNS = [
        re.compile(r"(?:order\s*(?:id|#|number|no|code|ref))[:\s#]*([A-Z0-9-]{4,32})\b", re.IGNORECASE),
        re.compile(r"\b(ORD-[A-Z0-9]{4,20})\b", re.IGNORECASE),
        re.compile(r"\b(ORDER-[A-Z0-9]{4,20})\b", re.IGNORECASE),
    ]

    INVOICE_NUMBER_PATTERNS = [
        re.compile(r"(?:invoice\s*(?:id|#|number|no|code|ref))[:\s#]*([A-Z0-9-]{4,32})\b", re.IGNORECASE),
        re.compile(r"\b(INV-[A-Z0-9]{4,20})\b", re.IGNORECASE),
    ]

    TOTAL_AMOUNT_PATTERNS = [
        re.compile(r"(?:grand\s*total|total\s*amount|total\s*due|amount\s*due|balance\s*due|final\s*total|total)[:\s]*([$€£₹A-Z]{0,4})\s*([0-9,]+\.[0-9]{2})\b", re.IGNORECASE),
        re.compile(r"(?:total\s*paid)[:\s]*([$€£₹A-Z]{0,4})\s*([0-9,]+\.[0-9]{2})\b", re.IGNORECASE),
    ]

    TAX_AMOUNT_PATTERNS = [
        re.compile(r"(?:sales\s*tax|tax\s*amount|vat|gst|tax)[:\s]*([$€£₹A-Z]{0,4})\s*([0-9,]+\.[0-9]{2})\b", re.IGNORECASE),
    ]

    CURRENCY_SYMBOLS: Dict[str, str] = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "₹": "INR",
        "¥": "JPY",
        "C$": "CAD",
        "A$": "AUD",
    }

    CURRENCY_WORDS = re.compile(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|CHF)\b", re.IGNORECASE)

    DATE_PATTERNS = [
        # YYYY-MM-DD or YYYY/MM/DD
        re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
        # DD-MM-YYYY or DD/MM/YYYY
        re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"),
        # Month name DD, YYYY (e.g. October 15, 2026 or Oct 15, 2026)
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

    MERCHANT_PATTERNS = [
        re.compile(r"\b(?:merchant|seller|store|vendor|retailer|sold\s*by)\s*[:=]\s*([^\r\n,;]{3,60})", re.IGNORECASE),
        re.compile(r"(?:welcome\s*to\s+|thank\s*you\s*for\s*shopping\s*at\s*)([^\r\n,;]{3,40})", re.IGNORECASE),
    ]

    def parse(
        self,
        text: str,
        page_count: int = 1,
        pages: Optional[List[PageExtractionMetadata]] = None,
        is_scanned: bool = False,
        provider_used: str = "rule_based",
    ) -> StructuredInvoiceData:
        """Parses raw OCR or document text into structured invoice fields."""
        if not text:
            return StructuredInvoiceData(
                missing_fields=[
                    "order_id", "invoice_number", "invoice_date", "total_amount",
                    "currency", "merchant_name", "product_name",
                ],
                warnings=["Empty document text; no entities could be extracted."],
                page_count=page_count,
                pages=pages or [],
                is_scanned=is_scanned,
                provider_used=provider_used,
            )

        field_confidence: Dict[str, float] = {}
        warnings: List[str] = []
        missing_fields: List[str] = []
        uncertain_fields: List[str] = []

        # 1. Order ID
        order_id, o_conf = self._extract_order_id(text)
        if order_id:
            field_confidence["order_id"] = o_conf
            if o_conf < 0.70:
                uncertain_fields.append("order_id")
        else:
            missing_fields.append("order_id")

        # 2. Invoice Number
        invoice_number, inv_conf = self._extract_invoice_number(text)
        if invoice_number:
            field_confidence["invoice_number"] = inv_conf
            if inv_conf < 0.70:
                uncertain_fields.append("invoice_number")
        else:
            missing_fields.append("invoice_number")

        # 3. Currency
        currency, c_conf = self._extract_currency(text)
        if currency:
            field_confidence["currency"] = c_conf
        else:
            missing_fields.append("currency")

        # 4. Total Amount
        total_amount, t_conf, found_curr = self._extract_total_amount(text)
        if total_amount is not None:
            field_confidence["total_amount"] = t_conf
            if not currency and found_curr:
                currency = found_curr
                field_confidence["currency"] = 0.85
            if t_conf < 0.70:
                uncertain_fields.append("total_amount")
        else:
            missing_fields.append("total_amount")
            warnings.append("Could not confidently extract total invoice amount.")

        # 5. Tax Amount
        tax_amount, tax_conf = self._extract_tax_amount(text)
        if tax_amount is not None:
            field_confidence["tax_amount"] = tax_conf
            if total_amount is not None and tax_amount > total_amount:
                warnings.append(f"Extracted tax amount ({tax_amount}) exceeds total amount ({total_amount}).")
                uncertain_fields.append("tax_amount")
                field_confidence["tax_amount"] = 0.40
        else:
            missing_fields.append("tax_amount")

        # 6. Invoice Date & Transaction Date
        inv_date, trans_date, d_conf = self._extract_dates(text)
        if inv_date:
            field_confidence["invoice_date"] = d_conf
            if d_conf < 0.70:
                uncertain_fields.append("invoice_date")
        else:
            missing_fields.append("invoice_date")

        if trans_date:
            field_confidence["transaction_date"] = d_conf

        # 7. Merchant Name
        merchant_name, m_conf = self._extract_merchant(text)
        if merchant_name:
            field_confidence["merchant_name"] = m_conf
            if m_conf < 0.70:
                uncertain_fields.append("merchant_name")
        else:
            missing_fields.append("merchant_name")

        # 8. Line Items & Product Name
        line_items, prod_name, prod_qty, prod_price = self._extract_line_items(text)
        if prod_name:
            field_confidence["product_name"] = 0.80
        else:
            missing_fields.append("product_name")

        # Calculate Overall Confidence
        if field_confidence:
            overall_conf = round(sum(field_confidence.values()) / max(len(field_confidence), 1), 3)
        else:
            overall_conf = 0.0

        return StructuredInvoiceData(
            order_id=order_id,
            invoice_number=invoice_number,
            invoice_date=inv_date,
            transaction_date=trans_date,
            total_amount=total_amount,
            currency=currency,
            tax_amount=tax_amount,
            product_name=prod_name,
            product_quantity=prod_qty,
            product_price=prod_price,
            merchant_name=merchant_name,
            line_items=line_items,
            overall_confidence=overall_conf,
            field_confidence=field_confidence,
            uncertain_fields=uncertain_fields,
            missing_fields=missing_fields,
            warnings=warnings,
            page_count=page_count,
            pages=pages or [],
            is_scanned=is_scanned,
            provider_used=provider_used,
        )

    def _extract_order_id(self, text: str) -> Tuple[Optional[str], float]:
        for pattern in self.ORDER_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                order_id = match.group(1).strip().strip(":#")
                return order_id, 0.95
        return None, 0.0

    def _extract_invoice_number(self, text: str) -> Tuple[Optional[str], float]:
        for pattern in self.INVOICE_NUMBER_PATTERNS:
            match = pattern.search(text)
            if match:
                inv_no = match.group(1).strip().strip(":#")
                return inv_no, 0.92
        return None, 0.0

    def _extract_currency(self, text: str) -> Tuple[Optional[str], float]:
        word_match = self.CURRENCY_WORDS.search(text)
        if word_match:
            return word_match.group(1).upper(), 0.95

        for sym, code in self.CURRENCY_SYMBOLS.items():
            if sym in text:
                return code, 0.90
        return None, 0.0

    def _extract_total_amount(self, text: str) -> Tuple[Optional[float], float, Optional[str]]:
        for pattern in self.TOTAL_AMOUNT_PATTERNS:
            match = pattern.search(text)
            if match:
                curr_sym = match.group(1).strip()
                val_str = match.group(2).replace(",", "")
                try:
                    amount = float(val_str)
                    detected_curr = self.CURRENCY_SYMBOLS.get(curr_sym) or (curr_sym if len(curr_sym) == 3 else None)
                    return amount, 0.95, detected_curr
                except ValueError:
                    continue
        return None, 0.0, None

    def _extract_tax_amount(self, text: str) -> Tuple[Optional[float], float]:
        for pattern in self.TAX_AMOUNT_PATTERNS:
            match = pattern.search(text)
            if match:
                val_str = match.group(2).replace(",", "")
                try:
                    return float(val_str), 0.88
                except ValueError:
                    continue
        return None, 0.0

    def _extract_dates(self, text: str) -> Tuple[Optional[date], Optional[date], float]:
        found_dates: List[date] = []
        for line in text.splitlines():
            # Check for explicitly labeled dates
            for pattern in self.DATE_PATTERNS:
                for match in pattern.finditer(line):
                    groups = match.groups()
                    try:
                        if len(groups) == 3 and groups[0].lower() in self.MONTH_MAP:
                            month = self.MONTH_MAP[groups[0].lower()]
                            day = int(groups[1])
                            year = int(groups[2])
                            found_dates.append(date(year, month, day))
                        elif len(groups) == 3 and len(groups[0]) == 4:
                            found_dates.append(date(int(groups[0]), int(groups[1]), int(groups[2])))
                        elif len(groups) == 3 and len(groups[2]) == 4:
                            found_dates.append(date(int(groups[2]), int(groups[1]), int(groups[0])))
                    except ValueError:
                        continue

        if not found_dates:
            return None, None, 0.0

        inv_date = found_dates[0]
        trans_date = found_dates[1] if len(found_dates) > 1 else inv_date
        return inv_date, trans_date, 0.90

    def _extract_merchant(self, text: str) -> Tuple[Optional[str], float]:
        for pattern in self.MERCHANT_PATTERNS:
            match = pattern.search(text)
            if match:
                name = match.group(1).strip()
                # Clean up trailing punctuation
                name = re.sub(r"[\r\n,;]+.*", "", name).strip()
                if len(name) >= 2:
                    return name, 0.85

        # Heuristic fallback: check first non-empty lines for brand header
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines[:3]:
            if not any(token in line.lower() for token in ["invoice", "receipt", "order", "bill to", "ship to", "date"]):
                if 3 <= len(line) <= 40:
                    return line, 0.65  # lower confidence fallback

        return None, 0.0

    def _extract_line_items(
        self, text: str
    ) -> Tuple[List[ReceiptLineItem], Optional[str], Optional[float], Optional[float]]:
        line_items: List[ReceiptLineItem] = []
        # Matches e.g. "Wireless Headphones   2   $149.99   $299.98" or "Product 1 x 49.99"
        item_regex = re.compile(
            r"^(?P<name>[A-Za-z0-9\s\-_,]{3,50}?)\s+(?:(?P<qty>\d+)\s+)?(?:x\s+)?[$€£₹]?(?P<price>\d+\.\d{2})",
            re.MULTILINE,
        )

        for match in item_regex.finditer(text):
            name = match.group("name").strip()
            # Ignore headers
            if any(k in name.lower() for k in ["total", "subtotal", "tax", "balance", "amount", "invoice", "date", "item"]):
                continue
            qty_str = match.group("qty")
            qty = float(qty_str) if qty_str else 1.0
            price = float(match.group("price"))
            line_items.append(
                ReceiptLineItem(
                    product_name=name,
                    quantity=qty,
                    unit_price=price,
                    total_price=round(qty * price, 2),
                    confidence=0.85,
                )
            )

        if line_items:
            primary = line_items[0]
            return line_items, primary.product_name, primary.quantity, primary.unit_price

        return [], None, None, None

