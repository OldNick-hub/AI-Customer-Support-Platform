"""Sanitizer Vault for Advanced RAG Knowledge Assistant.

Enforces zero-trust input/output sanitization, adversarial prompt injection detection,
control delimiter neutralization, PII masking with entity preservation, and context isolation.
"""

import base64
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
import urllib.parse
import uuid

from pydantic import BaseModel, Field

from app.core.exceptions import RAGSanitizationError
from app.core.logging import configure_safe_logger
from app.services.pii_masker import PIIMasker, PIIMaskResult, get_pii_masker

logger = configure_safe_logger(logging.getLogger(__name__))


class SanitizationResult(BaseModel):
    """Structured outcome of input sanitization and adversarial threat evaluation."""

    clean_query: str
    is_safe: bool = True
    risk_detected: bool = False
    risk_reason: Optional[str] = None
    detected_pii: Dict[str, int] = Field(default_factory=dict)
    has_payment_data: bool = False
    preserved_entities: List[str] = Field(default_factory=list)
    quarantine_triggered: bool = False


class RAGSanitizer:
    """Enterprise Sanitizer Vault for Knowledge Assistant.

    Provides stateless, isolated sanitization boundaries preventing prompt injection,
    protecting PII, preserving business identifiers (order IDs, policy codes, dates),
    and scrubbing output of sensitive credentials and internal prompt artifacts.
    """

    # Invisible zero-width and directional control characters
    INVISIBLE_CHARS_PATTERN = re.compile(
        r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00ad]"
    )

    # Delimiter, structural breakout tokens, and dangerous HTML
    TAG_DELIMITERS_PATTERN = re.compile(
        r"<\s*/?\s*(?:authoritative_data|context|system)\s*>",
        re.IGNORECASE,
    )
    HTML_TAG_PATTERN = re.compile(
        r"<\s*/?\s*(?:script|iframe|style|object|embed|form|input|svg|meta|link)\b.*?>",
        re.IGNORECASE,
    )
    INST_DELIMITERS_PATTERN = re.compile(
        r"\[/?(?:INST|SYS|SYSTEM)\]",
        re.IGNORECASE,
    )
    CHATML_DELIMITERS_PATTERN = re.compile(
        r"<\|im_(?:start|end)\|>",
        re.IGNORECASE,
    )
    SYS_ANGULAR_PATTERN = re.compile(
        r"<<SYS>>|<</SYS>>",
        re.IGNORECASE,
    )
    EXFILTRATION_PATTERN = re.compile(
        r"!\[.*?\]\(https?://.*?\)",
        re.IGNORECASE,
    )

    # Prompt injection and adversarial directive patterns
    DIRECTIVE_OVERRIDE_PATTERN = re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+)?(?:previous|prior|system|developer|safety|guardrail|original)?\s*(?:instructions|prompts|directives|rules|guidelines|guardrails)\b",
        re.IGNORECASE,
    )
    ROLE_HIJACKING_PATTERN = re.compile(
        r"\b(?:you\s+are\s+now|pretend\s+you\s+are|act\s+as|enter|switch\s+to)\s+(?:an?\s+)?(?:admin|root|unrestricted|advocate|developer\s+mode|dan\s+mode|jailbreak)\b",
        re.IGNORECASE,
    )
    SYSTEM_PROMPT_LEAK_PATTERN = re.compile(
        r"\b(?:repeat|print|reveal|output|dump|show|display)\s+(?:the\s+|your\s+)?(?:system\s+prompt|developer\s+instructions|system\s+instructions|initial\s+prompt|hidden\s+prompt)\b",
        re.IGNORECASE,
    )
    CODE_EXECUTION_PATTERN = re.compile(
        r"\b(?:eval|exec|os\.system|__import__|subprocess|open\s*\(['\"].*?['\"]\))\b",
        re.IGNORECASE,
    )
    BASE64_CANDIDATE_PATTERN = re.compile(
        r"(?:[A-Za-z0-9+/]{4}){4,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?"
    )


    # Business entity preservation patterns
    ORDER_ID_PATTERN = re.compile(r"\bORD-[A-Za-z0-9\-]+\b", re.IGNORECASE)
    DOC_CODE_PATTERN = re.compile(r"\b(?:POL|SOP|GUIDE|FAQ|DOC)-[A-Za-z0-9\-]+\b", re.IGNORECASE)
    ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

    # Sensitive internal artifacts in output
    TRACEBACK_PATTERN = re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE)
    SYSTEM_INSTRUCTION_LEAK = re.compile(r"\[SYSTEM INSTRUCTION\]", re.IGNORECASE)

    def __init__(
        self,
        max_query_length: int = 2000,
        pii_masker: Optional[PIIMasker] = None,
    ) -> None:
        self.max_query_length = max_query_length
        self.pii_masker = pii_masker or get_pii_masker()

    def sanitize_input(self, raw_query: Optional[str]) -> SanitizationResult:
        """Sanitizes customer input query with entity preservation and adversarial scanning.

        Stateless execution: No per-request context is stored on the instance.
        """
        if raw_query is None or not str(raw_query).strip():
            return SanitizationResult(
                clean_query="",
                is_safe=False,
                risk_detected=True,
                risk_reason="EMPTY_QUERY",
            )

        text = str(raw_query)

        # 1. Length bounding check
        if len(text) > self.max_query_length:
            logger.warning("Query rejected: length %d exceeds max %d", len(text), self.max_query_length)
            return SanitizationResult(
                clean_query="",
                is_safe=False,
                risk_detected=True,
                risk_reason="QUERY_OVERSIZED",
            )

        # 2. Strip invisible zero-width characters and apply Unicode normalization (NFKC)
        text_stripped = self.INVISIBLE_CHARS_PATTERN.sub("", text)
        normalized = unicodedata.normalize("NFKC", text_stripped)

        # URL-decode to catch encoded directive payloads
        try:
            url_decoded = urllib.parse.unquote_plus(normalized)
        except Exception:
            url_decoded = normalized

        # Base64 inspection
        b64_payload_found = False
        for b64_match in self.BASE64_CANDIDATE_PATTERN.finditer(normalized):
            candidate = b64_match.group(0)
            try:
                decoded_bytes = base64.b64decode(candidate, validate=True)
                decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
                if decoded_str and (
                    self.DIRECTIVE_OVERRIDE_PATTERN.search(decoded_str)
                    or self.ROLE_HIJACKING_PATTERN.search(decoded_str)
                    or self.CODE_EXECUTION_PATTERN.search(decoded_str)
                ):
                    b64_payload_found = True
                    break
            except Exception:
                pass

        # 3. Adversarial / Prompt-Injection scanning
        risk_detected = False
        risk_reason: Optional[str] = None
        quarantine = False

        if (
            self.DIRECTIVE_OVERRIDE_PATTERN.search(normalized)
            or self.ROLE_HIJACKING_PATTERN.search(normalized)
            or self.SYSTEM_PROMPT_LEAK_PATTERN.search(normalized)
            or self.CODE_EXECUTION_PATTERN.search(normalized)
            or self.DIRECTIVE_OVERRIDE_PATTERN.search(url_decoded)
            or self.ROLE_HIJACKING_PATTERN.search(url_decoded)
            or self.SYSTEM_PROMPT_LEAK_PATTERN.search(url_decoded)
            or self.CODE_EXECUTION_PATTERN.search(url_decoded)
            or b64_payload_found
        ):
            risk_detected = True
            risk_reason = "PROMPT_INJECTION_DETECTED"
            quarantine = True

        # Check for adversarial delimiter and dangerous HTML breakouts
        has_breakout_delimiters = (
            bool(self.TAG_DELIMITERS_PATTERN.search(normalized))
            or bool(self.HTML_TAG_PATTERN.search(normalized))
            or bool(self.INST_DELIMITERS_PATTERN.search(normalized))
            or bool(self.CHATML_DELIMITERS_PATTERN.search(normalized))
            or bool(self.SYS_ANGULAR_PATTERN.search(normalized))
            or bool(self.EXFILTRATION_PATTERN.search(normalized))
            or bool(self.TAG_DELIMITERS_PATTERN.search(url_decoded))
            or bool(self.HTML_TAG_PATTERN.search(url_decoded))
        )
        if has_breakout_delimiters:
            risk_detected = True
            risk_reason = "PROMPT_INJECTION_DETECTED"
            quarantine = True

        # 4. Neutralize control tags, HTML, and exfiltration links
        sanitized_delimiters = self.TAG_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", normalized)
        sanitized_delimiters = self.HTML_TAG_PATTERN.sub("[ESCAPED_TAG]", sanitized_delimiters)
        sanitized_delimiters = self.INST_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", sanitized_delimiters)
        sanitized_delimiters = self.CHATML_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", sanitized_delimiters)
        sanitized_delimiters = self.SYS_ANGULAR_PATTERN.sub("[ESCAPED_TAG]", sanitized_delimiters)
        sanitized_delimiters = self.EXFILTRATION_PATTERN.sub("[ESCAPED_LINK]", sanitized_delimiters)

        # 5. Entity Preservation + PII Masking
        # Identify non-sensitive business entities to preserve:
        # Order numbers, policy/document codes, ISO dates
        preserved_spans: List[Tuple[int, int, str]] = []
        for pat in (self.ORDER_ID_PATTERN, self.DOC_CODE_PATTERN, self.ISO_DATE_PATTERN):
            for match in pat.finditer(sanitized_delimiters):
                preserved_spans.append((match.start(), match.end(), match.group(0)))

        # Sort spans by start index and resolve overlaps
        preserved_spans.sort(key=lambda s: s[0])
        resolved_spans: List[Tuple[int, int, str]] = []
        for span in preserved_spans:
            if not resolved_spans or span[0] >= resolved_spans[-1][1]:
                resolved_spans.append(span)

        # Replace preserved entities with collision-proof nonce placeholders
        execution_nonce = uuid.uuid4().hex[:12]
        placeholder_map: Dict[str, str] = {}
        chunks: List[str] = []
        last_idx = 0
        for i, (start, end, val) in enumerate(resolved_spans):
            token = f"__PRESERVED_BIZ_{execution_nonce}_{i}__"
            placeholder_map[token] = val
            chunks.append(sanitized_delimiters[last_idx:start])
            chunks.append(token)
            last_idx = end
        chunks.append(sanitized_delimiters[last_idx:])
        intermediate_text = "".join(chunks)


        # Mask PII in intermediate text
        pii_result: PIIMaskResult = self.pii_masker.mask(intermediate_text)

        # Restore preserved entities
        masked_text = pii_result.masked_text
        for token, original_val in placeholder_map.items():
            masked_text = masked_text.replace(token, original_val)

        preserved_entities_list = list(placeholder_map.values())

        return SanitizationResult(
            clean_query=masked_text.strip(),
            is_safe=not risk_detected,
            risk_detected=risk_detected,
            risk_reason=risk_reason,
            detected_pii=pii_result.entity_counts,
            has_payment_data=pii_result.has_payment_data,
            preserved_entities=preserved_entities_list,
            quarantine_triggered=quarantine,
        )

    def sanitize_output(self, text: Optional[str]) -> str:
        """Sanitizes generated LLM answers before client emission.

        Masks detected PII, strips internal prompt artifacts, scrubs stack traces,
        and preserves bracketed citation tokens [1], [2].
        """
        if text is None:
            return ""

        clean = unicodedata.normalize("NFKC", str(text))

        # 1. Scrub internal exception tracebacks if any leaked
        clean = self.TRACEBACK_PATTERN.sub("[SYSTEM_ERROR_REDACTED]", clean)
        clean = self.SYSTEM_INSTRUCTION_LEAK.sub("[SYSTEM_DIRECTIVE_REDACTED]", clean)

        # 2. Scrub structural delimiters and HTML tags
        clean = self.TAG_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", clean)
        clean = self.HTML_TAG_PATTERN.sub("[ESCAPED_TAG]", clean)
        clean = self.INST_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", clean)
        clean = self.CHATML_DELIMITERS_PATTERN.sub("[ESCAPED_TAG]", clean)
        clean = self.SYS_ANGULAR_PATTERN.sub("[ESCAPED_TAG]", clean)

        # 3. Mask PII via PIIMasker
        pii_result = self.pii_masker.mask(clean)
        return pii_result.masked_text.strip()


_default_sanitizer: Optional[RAGSanitizer] = None


def get_rag_sanitizer() -> RAGSanitizer:
    """Returns the singleton instance of RAGSanitizer."""
    global _default_sanitizer
    if _default_sanitizer is None:
        _default_sanitizer = RAGSanitizer()
    return _default_sanitizer

