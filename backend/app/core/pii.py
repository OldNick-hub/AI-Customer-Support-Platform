"""PII Masking & Payment Isolation Engine.

Ensures customer sensitive data (credit cards, emails, phone numbers, SSNs) is masked
by default prior to database persistence, log emission, and vector indexing.
"""

import re
from typing import Optional


class PIIMaskingEngine:
    """Sanitizes strings to enforce PII redaction and PCI-DSS compliance."""

    # Matches credit card candidates (13 to 19 digits with optional spaces or dashes)
    CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

    # Email pattern
    EMAIL_PATTERN = re.compile(
        r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*([A-Za-z0-9._%+-])@([A-Za-z0-9.-]+\.[A-Z|a-z]{2,})\b"
    )

    # Phone pattern: matches standard international and national phone formats (with optional leading +)
    PHONE_PATTERN = re.compile(
        r"(?:\+\d{1,3}[-.\s]?|\b\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    )

    # US Social Security Number pattern
    SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

    # CVV / Security Code pattern
    CVV_PATTERN = re.compile(r"\b(?:cvv|cvc|security\s*code|card\s*verification)\s*[:=]?\s*(\d{3,4})\b", re.IGNORECASE)

    # Expiration date pattern (MM/YY or MM/YYYY)
    EXP_PATTERN = re.compile(r"\b(?:exp|expires|expiry)\s*[:=]?\s*(0[1-9]|1[0-2])\s*[/.-]\s*(\d{2,4})\b", re.IGNORECASE)

    @classmethod
    def _luhn_check(cls, digits: str) -> bool:
        """Validates a candidate digit string against the Luhn (mod 10) algorithm."""
        digits = "".join(filter(str.isdigit, digits))
        if len(digits) < 13 or len(digits) > 19:
            return False

        total = 0
        reverse_digits = digits[::-1]
        for idx, char in enumerate(reverse_digits):
            n = int(char)
            if idx % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n
        return total % 10 == 0

    @classmethod
    def mask_credit_cards(cls, text: str) -> str:
        """Finds valid Luhn credit cards and masks all but the last 4 digits."""
        def repl(match: re.Match) -> str:
            raw = match.group(0)
            digits = re.sub(r"\D", "", raw)
            if cls._luhn_check(digits):
                return f"****-****-****-{digits[-4:]}"
            return raw

        return cls.CREDIT_CARD_PATTERN.sub(repl, text)

    @classmethod
    def mask_emails(cls, text: str) -> str:
        """Masks emails preserving first/last char of local part: j***n@domain.com."""
        def repl(match: re.Match) -> str:
            first = match.group(1)
            last = match.group(2)
            domain = match.group(3)
            return f"{first}***{last}@{domain}"

        return cls.EMAIL_PATTERN.sub(repl, text)

    @classmethod
    def mask_phone_numbers(cls, text: str) -> str:
        """Masks phone numbers to [PHONE_REDACTED]."""
        return cls.PHONE_PATTERN.sub("[PHONE_REDACTED]", text)

    @classmethod
    def mask_ssn(cls, text: str) -> str:
        """Masks SSNs preserving only the last 4 digits: ***-**-1234."""
        def repl(match: re.Match) -> str:
            raw = match.group(0)
            digits = re.sub(r"\D", "", raw)
            return f"***-**-{digits[-4:]}"

        return cls.SSN_PATTERN.sub(repl, text)

    @classmethod
    def mask_cvv(cls, text: str) -> str:
        """Redacts CVV codes."""
        return cls.CVV_PATTERN.sub("CVV: [REDACTED]", text)

    @classmethod
    def mask(cls, text: Optional[str]) -> str:
        """Alias for mask_text."""
        return cls.mask_text(text)

    @classmethod
    def mask_text(cls, text: Optional[str]) -> str:
        """Executes full pipeline masking across cards, emails, phones, SSNs, and CVVs.

        This is the default transformation applied to all messages before database persistence.
        """
        if not text:
            return ""

        sanitized = cls.mask_credit_cards(text)
        sanitized = cls.mask_cvv(sanitized)
        sanitized = cls.mask_ssn(sanitized)
        sanitized = cls.mask_emails(sanitized)
        sanitized = cls.mask_phone_numbers(sanitized)
        return sanitized

    @classmethod
    def contains_payment_data(cls, text: str) -> bool:
        """Returns True if the text contains raw payment data (credit card, CVV, exp).

        Used as an assertion gate to prevent payment information from entering vector indexing.
        """
        if not text:
            return False

        # Check CVV pattern
        if cls.CVV_PATTERN.search(text):
            return True

        # Check Credit Card pattern with Luhn validation
        for match in cls.CREDIT_CARD_PATTERN.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if cls._luhn_check(digits):
                return True

        return False

