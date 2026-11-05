"""Pre-Translation Multilingual Security Scanner.

Enforces zero-trust pre-translation adversarial scanning across English, Spanish,
French, German, and Hindi/Hinglish to intercept prompt injections, role hijacking,
structural delimiter breakouts, and encoded payloads BEFORE data leaves the server.
"""

import base64
import logging
import re
from typing import Optional
import unicodedata
import urllib.parse
from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class PreTranslationSecurityResult(BaseModel):
    """Structured result of pre-translation adversarial security evaluation."""

    clean_text: str
    is_safe: bool = True
    risk_detected: bool = False
    risk_reason: Optional[str] = None
    quarantined: bool = False


class PreTranslationSecurityScanner:
    """Enterprise Pre-Translation Adversarial Scanner.

    Detects prompt injection directives across all supported languages before
    external LLM dispatch, preventing adversarial jailbreaks in foreign scripts.
    """

    # Invisible zero-width and directional control characters
    INVISIBLE_CHARS_PATTERN = re.compile(
        r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00ad]"
    )

    # Delimiter breakout patterns, dangerous markup, and exfiltration links
    DELIMITER_BREAKOUT_PATTERN = re.compile(
        r"<\s*/?\s*(?:script|iframe|style|object|embed|form|input|svg|meta|link|authoritative_data|context|system)\b.*?>|"
        r"\[/?(?:INST|SYS|SYSTEM)\]|<\|im_(?:start|end)\|>|<<SYS>>|<</SYS>>|"
        r"!\[.*?\]\(https?://.*?\)",
        re.IGNORECASE,
    )

    # Pre-existing placeholder probe detection (user attempting to probe vault tokens or normalized cache tags)
    PLACEHOLDER_PROBE_PATTERN = re.compile(r"__(?:PRESERVED|NORM)_[A-Za-z0-9_]+__")

    # Base64 candidate pattern (standard and URL-safe)
    BASE64_CANDIDATE_PATTERN = re.compile(
        r"(?:[A-Za-z0-9+/_\-]{4}){3,}(?:[A-Za-z0-9+/_\-]{2}==|[A-Za-z0-9+/_\-]{3}=)?"
    )

    # Multilingual Adversarial Directive Override Patterns
    ENGLISH_INJECTION_PATTERN = re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+)?(?:previous|prior|system|developer|safety|guardrail|original)?\s*(?:instructions|prompts|directives|rules|guidelines)\b|"
        r"\b(?:you\s+are\s+now|pretend\s+you\s+are|act\s+as|enter|switch\s+to)\s+(?:an?\s+)?(?:admin|root|unrestricted|developer\s+mode|dan\s+mode|jailbreak)\b|"
        r"\b(?:repeat|print|reveal|output|dump|show|display)\s+(?:the\s+|your\s+)?(?:system\s+prompt|developer\s+instructions|system\s+instructions|initial\s+prompt|hidden\s+prompt)\b",
        re.IGNORECASE,
    )

    SPANISH_INJECTION_PATTERN = re.compile(
        r"\b(?:ignora|olvida|descarta|anula|omite|evita)\s+(?:todas?\s+las?\s+)?(?:instrucciones|reglas|directivas|órdenes|prompts)\b|"
        r"\b(?:actúa\s+como|eres\s+ahora|modo)\s+(?:un?\s+)?(?:admin|administrador|root|desarrollador|dan)\b|"
        r"\b(?:repite|muestra|imprime|revela|escribe)\s+(?:el\s+)?(?:prompt\s+del\s+sistema|instrucciones\s+del\s+sistema|prompt\s+inicial)\b",
        re.IGNORECASE,
    )

    FRENCH_INJECTION_PATTERN = re.compile(
        r"\b(?:ignore|oublie|contourne|annule|efface)\s+(?:toutes?\s+les?\s+)?(?:instructions|règles|directives|consignes)\b|"
        r"\b(?:agis\s+en\s+tant\s+que|tu\s+es\s+maintenant)\s+(?:un?\s+)?(?:admin|administrateur|root|développeur)\b|"
        r"\b(?:affiche|répète|montre|révèle|imprime)\s+(?:le\s+)?(?:prompt\s+système|instructions\s+système|prompt\s+initial)\b",
        re.IGNORECASE,
    )

    GERMAN_INJECTION_PATTERN = re.compile(
        r"\b(?:ignoriere|vergiss|übergehe|verwerfe)\s+(?:alle\s+)?(?:anweisungen|regeln|richtlinien|prompts)\b|"
        r"\b(?:handle\s+als|du\s+bist\s+jetzt|wechsle\s+zu)\s+(?:admin|administrator|root|entwickler)\b|"
        r"\b(?:wiederhole|zeige|gib\s+aus|drucke|offenbare)\s+(?:den\s+)?(?:system-prompt|systemanweisungen|ursprünglichen\s+prompt)\b",
        re.IGNORECASE,
    )

    HINDI_INJECTION_PATTERN = re.compile(
        r"\b(?:pichhle|purane|saare|sabhi)\s+(?:nirdesh|instructions|hidayat|rules)\s+(?:bhool|ignore|chhod)\s*(?:jao|karo|do)?\b|"
        r"\b(?:system\s+prompt|developer\s+instructions)\s+(?:dikhao|batao|print\s+karo|reveal|show)\b|"
        r"\b(?:tum\s+ab|act\s+as|pretend\s+karo)\s+(?:admin|root|developer)\b|"
        r"पिछले\s+सभी\s+(?:निर्देश|नियम)\s+भूल\s+जाओ|सिस्टम\s+प्रॉम्प्ट\s+दिखाओ",
        re.IGNORECASE,
    )

    def __init__(self, max_length: int = 2000) -> None:
        self.max_length = max_length

    def scan(self, raw_text: Optional[str]) -> PreTranslationSecurityResult:
        """Evaluates raw text for multilingual adversarial instructions and delimiter breakouts.

        Stateless execution: No per-request state is retained on the scanner instance.
        """
        return self._do_scan(raw_text)

    def scan_inbound(self, raw_text: Optional[str]) -> PreTranslationSecurityResult:
        """Alias for scan to support inbound security scanning interfaces."""
        return self._do_scan(raw_text)

    def _do_scan(self, raw_text: Optional[str]) -> PreTranslationSecurityResult:
        if raw_text is None or not str(raw_text).strip():
            return PreTranslationSecurityResult(
                clean_text="",
                is_safe=False,
                risk_detected=True,
                risk_reason="EMPTY_INPUT",
                quarantined=False,
            )

        text = str(raw_text)

        # 1. Length bounding check
        if len(text) > self.max_length:
            logger.warning("Pre-translation input length %d exceeds max %d", len(text), self.max_length)
            return PreTranslationSecurityResult(
                clean_text="",
                is_safe=False,
                risk_detected=True,
                risk_reason="INPUT_OVERSIZED",
                quarantined=True,
            )

        # 2. Strip invisible zero-width and directional control characters
        text_stripped = self.INVISIBLE_CHARS_PATTERN.sub("", text)

        # 3. Unicode normalization (NFKC)
        normalized = unicodedata.normalize("NFKC", text_stripped)

        # URL-decode to catch encoded attacks
        try:
            url_decoded = urllib.parse.unquote_plus(normalized)
        except Exception:
            url_decoded = normalized

        # 4. Check for structural breakouts (tags, system delimiters, exfiltration)
        if self.DELIMITER_BREAKOUT_PATTERN.search(normalized) or self.DELIMITER_BREAKOUT_PATTERN.search(url_decoded):
            logger.warning("Adversarial delimiter breakout detected in pre-translation scan.")
            return PreTranslationSecurityResult(
                clean_text=normalized,
                is_safe=False,
                risk_detected=True,
                risk_reason="DELIMITER_BREAKOUT_DETECTED",
                quarantined=True,
            )

        # 5. Check for placeholder probe attacks (user input injecting internal __PRESERVED_*__ tokens)
        if self.PLACEHOLDER_PROBE_PATTERN.search(normalized) or self.PLACEHOLDER_PROBE_PATTERN.search(url_decoded):
            logger.warning("Adversarial placeholder probe attempt detected in pre-translation scan.")
            return PreTranslationSecurityResult(
                clean_text=normalized,
                is_safe=False,
                risk_detected=True,
                risk_reason="PLACEHOLDER_PROBE_DETECTED",
                quarantined=True,
            )

        # 6. Base64 payload inspection (standard and URL-safe)
        for b64_match in self.BASE64_CANDIDATE_PATTERN.finditer(normalized):
            candidate = b64_match.group(0)
            try:
                candidate_clean = candidate.replace("-", "+").replace("_", "/")
                pad_len = (4 - len(candidate_clean) % 4) % 4
                candidate_clean += "=" * pad_len
                decoded_bytes = base64.b64decode(candidate_clean, validate=True)
                decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
                if decoded_str and self._contains_injection(decoded_str):
                    logger.warning("Base64 encoded injection detected in pre-translation scan.")
                    return PreTranslationSecurityResult(
                        clean_text=normalized,
                        is_safe=False,
                        risk_detected=True,
                        risk_reason="BASE64_INJECTION_DETECTED",
                        quarantined=True,
                    )
            except Exception:
                pass

        # 7. Direct Multilingual Adversarial Directive Scanning
        if self._contains_injection(normalized) or self._contains_injection(url_decoded):
            logger.warning("Multilingual prompt injection detected in pre-translation scan.")
            return PreTranslationSecurityResult(
                clean_text=normalized,
                is_safe=False,
                risk_detected=True,
                risk_reason="PROMPT_INJECTION_DETECTED",
                quarantined=True,
            )

        return PreTranslationSecurityResult(
            clean_text=normalized,
            is_safe=True,
            risk_detected=False,
            risk_reason=None,
            quarantined=False,
        )

    def _contains_injection(self, text: str) -> bool:
        """Checks if text matches any multilingual prompt injection pattern."""
        return bool(
            self.ENGLISH_INJECTION_PATTERN.search(text)
            or self.SPANISH_INJECTION_PATTERN.search(text)
            or self.FRENCH_INJECTION_PATTERN.search(text)
            or self.GERMAN_INJECTION_PATTERN.search(text)
            or self.HINDI_INJECTION_PATTERN.search(text)
        )

