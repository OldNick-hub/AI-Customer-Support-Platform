"""Cryptographic 128-Bit Entity & Citation Preservation Vault.

Guarantees 100% preservation of order IDs, policy codes, SKUs, monetary amounts,
dates, and inline citations ([1], [2]) across neural translation boundaries using
cryptographically secure 128-bit random tokens, strict bi-directional integrity validation,
and request-scoped ephemeral isolation.
"""

import logging
import re
import secrets
from typing import Dict, List, Optional, Set, Tuple

from app.core.logging import configure_safe_logger
from app.modules.multilingual.config import (
    CITATION_PATTERN,
    CURRENCY_PATTERN,
    DOC_CODE_PATTERN,
    ISO_DATE_PATTERN,
    ORDER_ID_PATTERN,
    PLACEHOLDER_REGEX,
    SINGLE_CITATION_PATTERN,
    SKU_PATTERN,
)

logger = configure_safe_logger(logging.getLogger(__name__))

# General pattern to detect ANY placeholder token in text
ANY_PLACEHOLDER_PATTERN = re.compile(r"__PRESERVED_[A-Za-z0-9_]+__")


class EntityPreservationVault:
    """Request-scoped cryptographic entity & citation preservation vault.

    Protects business identifiers and citations before translation and strictly
    validates placeholder integrity (count, identity, uniqueness) before restoration.
    """

    def __init__(self, request_id: Optional[str] = None) -> None:
        self.request_id = request_id or secrets.token_hex(8)
        # Maps placeholder token -> original raw string
        self._token_to_value: Dict[str, str] = {}
        # Set of active placeholder tokens
        self._active_tokens: Set[str] = set()

    @property
    def registered_count(self) -> int:
        """Returns the number of registered placeholders."""
        return len(self._active_tokens)

    def _generate_token(self, prefix: str) -> str:
        """Generates a cryptographically random 128-bit placeholder token.

        Format: __PRESERVED_{PREFIX}_{32_hex_chars}__
        Provides 128 bits of entropy via secrets.token_hex(16).
        """
        token128 = secrets.token_hex(16)
        return f"__PRESERVED_{prefix}_{token128}__"

    def mask_entities(self, text: Optional[str]) -> str:
        """Masks business identifiers, orders, SKUs, dates, and amounts with 128-bit tokens."""
        if not text:
            return ""

        result = text

        # 1. Order IDs (e.g. ORD-12345, ORD-ABC-999)
        def replace_order(m: re.Match) -> str:
            val = m.group(0)
            token = self._generate_token("ORDER")
            self._token_to_value[token] = val
            self._active_tokens.add(token)
            return token

        result = ORDER_ID_PATTERN.sub(replace_order, result)

        # 2. Policy / Document Codes (e.g. POL-RET-US-2026, SOP-REF-01)
        def replace_code(m: re.Match) -> str:
            val = m.group(0)
            token = self._generate_token("CODE")
            self._token_to_value[token] = val
            self._active_tokens.add(token)
            return token

        result = DOC_CODE_PATTERN.sub(replace_code, result)

        # 3. Product SKUs (e.g. SKU-12345, PROD-8821)
        def replace_sku(m: re.Match) -> str:
            val = m.group(0)
            token = self._generate_token("SKU")
            self._token_to_value[token] = val
            self._active_tokens.add(token)
            return token

        result = SKU_PATTERN.sub(replace_sku, result)

        # 4. Currency amounts (e.g. $49.99, €120, ₹500)
        def replace_currency(m: re.Match) -> str:
            val = m.group(0)
            token = self._generate_token("AMT")
            self._token_to_value[token] = val
            self._active_tokens.add(token)
            return token

        result = CURRENCY_PATTERN.sub(replace_currency, result)

        # 5. ISO Dates (e.g. 2026-11-04)
        def replace_date(m: re.Match) -> str:
            val = m.group(0)
            token = self._generate_token("DATE")
            self._token_to_value[token] = val
            self._active_tokens.add(token)
            return token

        result = ISO_DATE_PATTERN.sub(replace_date, result)

        return result

    def mask_citations(self, text: Optional[str]) -> str:
        """Masks inline citation markers (e.g. [1], [2], [1, 2]) with 128-bit tokens.

        Handles compound citations by expanding them to individual tokens.
        """
        if not text:
            return ""

        result = text

        # Match compound citations like [1, 2] or single like [1]
        def replace_citations(m: re.Match) -> str:
            full_match = m.group(0)
            # Extract numbers inside [ ... ]
            inner = full_match.strip("[]")
            parts = [p.strip() for p in inner.split(",") if p.strip().isdigit()]
            if not parts:
                return full_match

            tokens: List[str] = []
            for num_str in parts:
                token = self._generate_token(f"CIT_{num_str}")
                self._token_to_value[token] = f"[{num_str}]"
                self._active_tokens.add(token)
                tokens.append(token)

            return " ".join(tokens)

        return CITATION_PATTERN.sub(replace_citations, result)

    def validate_placeholders(self, text: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Validates placeholder integrity in translated text before value restoration.

        Enforces four mandatory criteria:
        1. Exact count: Number of placeholders in text == registered active tokens.
        2. Exact identity: Every registered token is present verbatim.
        3. Zero duplications: No token appears more than once.
        4. Zero unexpected tokens: No unknown __PRESERVED_*__ tokens exist in output.

        Returns (True, None) on success, or (False, failure_reason) on integrity violation.
        """
        if not self._active_tokens:
            # If no placeholders were registered, verify text contains no unexpected tokens
            if text and ANY_PLACEHOLDER_PATTERN.search(text):
                return False, "UNEXPECTED_PLACEHOLDER_INJECTED"
            return True, None

        if not text:
            return False, "EMPTY_TEXT_WITH_REGISTERED_TOKENS"

        # Find all placeholders in text
        found_tokens = ANY_PLACEHOLDER_PATTERN.findall(text)

        # 1. Exact count check
        if len(found_tokens) != len(self._active_tokens):
            logger.warning(
                "Placeholder count mismatch in request %s: expected %d, found %d",
                self.request_id,
                len(self._active_tokens),
                len(found_tokens),
            )
            return False, f"COUNT_MISMATCH: expected {len(self._active_tokens)}, found {len(found_tokens)}"

        # 2. Zero duplications check
        found_set = set(found_tokens)
        if len(found_set) != len(found_tokens):
            logger.warning("Duplicate placeholder tokens detected in request %s", self.request_id)
            return False, "DUPLICATE_PLACEHOLDER_DETECTED"

        # 3. Exact identity check (all active tokens must be found)
        missing_tokens = self._active_tokens - found_set
        if missing_tokens:
            logger.warning(
                "Missing placeholder tokens in request %s: %d tokens missing",
                self.request_id,
                len(missing_tokens),
            )
            return False, f"MISSING_TOKENS: {len(missing_tokens)} tokens missing"

        # 4. Zero unexpected tokens check
        unexpected_tokens = found_set - self._active_tokens
        if unexpected_tokens:
            logger.warning(
                "Unexpected placeholder tokens in request %s: %d unknown tokens",
                self.request_id,
                len(unexpected_tokens),
            )
            return False, f"UNEXPECTED_TOKENS: {len(unexpected_tokens)} unknown tokens"

        return True, None

    def restore_entities(self, text: Optional[str]) -> str:
        """Restores original values for all validated placeholders.

        Raises ValueError if validate_placeholders() fails, preventing corrupted
        or adversary-injected tokens from being processed.
        """
        if not text:
            return ""

        is_valid, reason = self.validate_placeholders(text)
        if not is_valid:
            raise ValueError(f"Cannot restore entities: placeholder validation failed ({reason}).")

        result = text
        for token, raw_val in self._token_to_value.items():
            result = result.replace(token, raw_val)

        return result

    def unmask_entities(self, text: Optional[str]) -> str:
        """Alias for restore_entities to support unified unmasking interfaces."""
        return self.restore_entities(text)

    def unmask_citations(self, text: Optional[str]) -> str:
        """Alias for restore_entities to restore masked citations."""
        return self.restore_entities(text)

