"""PII Masking & Data Sanitization Service.

Provides structured PII detection, character-offset reporting, Luhn-validated payment
isolation, and recursive JSON/dict payload sanitization for API requests, logging,
and vector indexing pipelines.
"""

from enum import Enum
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.core.pii import PIIMaskingEngine


class PIIEntityType(str, Enum):
    """Supported PII classification types."""

    CREDIT_CARD = "CREDIT_CARD"
    CVV = "CVV"
    SSN = "SSN"
    EMAIL = "EMAIL"
    PHONE = "PHONE"


class DetectedEntity(BaseModel):
    """Represents a detected PII entity with original text character offsets."""

    entity_type: PIIEntityType
    start: int
    end: int
    masked_value: str


class PIIMaskResult(BaseModel):
    """Structured result returned by the PII masking service."""

    original_text: str = ""
    masked_text: str = ""
    detected_entities: List[DetectedEntity] = Field(default_factory=list)
    entity_counts: Dict[str, int] = Field(default_factory=dict)
    has_payment_data: bool = False


class PIIMasker:
    """Enterprise PII masking and payload sanitization engine."""

    SENSITIVE_KEY_TOKENS = {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "cvv",
        "cvc",
        "pin",
        "credential",
        "credentials",
        "auth",
        "authorization",
    }

    SENSITIVE_KEY_COMPOUNDS = (
        "api_key",
        "card_number",
        "credit_card",
        "private_key",
        "master_key",
        "access_token",
        "refresh_token",
    )

    SENSITIVE_EXACT_KEYS = {"card", "pan"}

    def __init__(self) -> None:
        self.engine = PIIMaskingEngine

    def _is_sensitive_key(self, key: str) -> bool:
        """Determines if a dictionary key name indicates sensitive or credential data."""
        # Convert camelCase to snake_case and lower
        norm = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
        if norm in self.SENSITIVE_EXACT_KEYS:
            return True

        tokens = set(re.split(r"[_\-.\s]+", norm))
        if self.SENSITIVE_KEY_TOKENS.intersection(tokens):
            return True

        if any(compound in norm for compound in self.SENSITIVE_KEY_COMPOUNDS):
            return True

        return False

    def mask(self, text: Optional[str]) -> PIIMaskResult:
        """Analyzes text for PII entities, masks them, and returns structured metadata.

        Character offsets in detected_entities strictly reference the original input text.
        """
        if text is None:
            return PIIMaskResult(
                original_text="",
                masked_text="",
                detected_entities=[],
                entity_counts={t.value: 0 for t in PIIEntityType},
                has_payment_data=False,
            )

        if not isinstance(text, str):
            text = str(text)

        if not text.strip():
            return PIIMaskResult(
                original_text=text,
                masked_text=text,
                detected_entities=[],
                entity_counts={t.value: 0 for t in PIIEntityType},
                has_payment_data=False,
            )

        # Collect raw candidate spans with priority:
        # 1: Credit Card (Luhn checked)
        # 2: CVV
        # 3: SSN
        # 4: Email
        # 5: Phone
        candidates = []

        # 1. Credit Cards
        for match in self.engine.CREDIT_CARD_PATTERN.finditer(text):
            raw = match.group(0)
            digits = re.sub(r"\D", "", raw)
            if self.engine._luhn_check(digits):
                masked_val = f"****-****-****-{digits[-4:]}"
                candidates.append((match.start(), match.end(), PIIEntityType.CREDIT_CARD, masked_val, 1))

        # 2. CVV
        for match in self.engine.CVV_PATTERN.finditer(text):
            masked_val = "CVV: [REDACTED]"
            candidates.append((match.start(), match.end(), PIIEntityType.CVV, masked_val, 2))

        # 3. SSN
        for match in self.engine.SSN_PATTERN.finditer(text):
            raw = match.group(0)
            digits = re.sub(r"\D", "", raw)
            masked_val = f"***-**-{digits[-4:]}"
            candidates.append((match.start(), match.end(), PIIEntityType.SSN, masked_val, 3))

        # 4. Email
        for match in self.engine.EMAIL_PATTERN.finditer(text):
            first = match.group(1)
            last = match.group(2)
            domain = match.group(3)
            masked_val = f"{first}***{last}@{domain}"
            candidates.append((match.start(), match.end(), PIIEntityType.EMAIL, masked_val, 4))

        # 5. Phone
        for match in self.engine.PHONE_PATTERN.finditer(text):
            masked_val = "[PHONE_REDACTED]"
            candidates.append((match.start(), match.end(), PIIEntityType.PHONE, masked_val, 5))

        # Sort candidates by priority (1 to 5) then start index
        candidates.sort(key=lambda c: (c[4], c[0]))

        # Accept non-overlapping candidates
        accepted_entities: List[DetectedEntity] = []
        accepted_spans: List[tuple[int, int]] = []

        for start, end, entity_type, masked_val, _prio in candidates:
            # Check overlap with already accepted spans
            overlaps = False
            for acc_start, acc_end in accepted_spans:
                if max(start, acc_start) < min(end, acc_end):
                    overlaps = True
                    break
            if not overlaps:
                accepted_spans.append((start, end))
                accepted_entities.append(
                    DetectedEntity(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        masked_value=masked_val,
                    )
                )

        # Sort accepted entities chronologically by original start position
        accepted_entities.sort(key=lambda e: e.start)

        # Construct masked text in single linear pass
        chunks = []
        last_idx = 0
        for entity in accepted_entities:
            chunks.append(text[last_idx:entity.start])
            chunks.append(entity.masked_value)
            last_idx = entity.end
        chunks.append(text[last_idx:])
        masked_text = "".join(chunks)

        # Compute entity counts
        entity_counts = {t.value: 0 for t in PIIEntityType}
        for entity in accepted_entities:
            entity_counts[entity.entity_type.value] += 1

        # Payment data assertion gate
        has_payment = self.engine.contains_payment_data(text)

        return PIIMaskResult(
            original_text=text,
            masked_text=masked_text,
            detected_entities=accepted_entities,
            entity_counts=entity_counts,
            has_payment_data=has_payment,
        )

    def mask_text(self, text: Optional[str]) -> str:
        """Fast helper returning only the masked text string."""
        if text is None:
            return ""
        return self.mask(text).masked_text

    def contains_payment_data(self, text: Optional[str]) -> bool:
        """Returns True if the input text contains raw payment data (card, CVV, exp)."""
        if not text:
            return False
        return self.engine.contains_payment_data(str(text))

    def _sanitize_node(self, node: Any) -> Any:
        """Recursive helper for traversing dicts, lists, and primitives."""
        if isinstance(node, dict):
            sanitized_dict: Dict[str, Any] = {}
            for k, v in node.items():
                if self._is_sensitive_key(str(k)):
                    sanitized_dict[str(k)] = "[REDACTED]"
                else:
                    sanitized_dict[str(k)] = self._sanitize_node(v)
            return sanitized_dict
        elif isinstance(node, list):
            return [self._sanitize_node(item) for item in node]
        elif isinstance(node, tuple):
            return tuple(self._sanitize_node(item) for item in node)
        elif isinstance(node, str):
            return self.mask_text(node)
        else:
            return node

    def sanitize_dict(self, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Deeply sanitizes a dictionary payload.

        Redacts sensitive credential keys entirely and masks inline PII in string values.
        Does not mutate the input dictionary.
        """
        if data is None:
            return {}
        if not isinstance(data, dict):
            return {}
        return self._sanitize_node(data)

    def sanitize_payload(self, payload: Any) -> Any:
        """Sanitizes arbitrary payloads (dict, list, str, or primitive)."""
        if payload is None:
            return None
        return self._sanitize_node(payload)


_default_masker: Optional[PIIMasker] = None


def get_pii_masker() -> PIIMasker:
    """Returns a singleton instance of the PIIMasker service."""
    global _default_masker
    if _default_masker is None:
        _default_masker = PIIMasker()
    return _default_masker
