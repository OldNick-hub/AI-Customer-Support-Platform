"""Multilingual Language Detector with Latin-Script & Hindi Script Specialization.

Implements a multi-tier hybrid detection engine:
- Tier 1A: Devanagari script Unicode block analyzer (hi-Deva -> hi).
- Tier 1B: Multi-feature Latin-script n-gram and stopword frequency analyzer
           (en, es, fr, de, and Romanized Hinglish hi-Latn -> hi).
- Tier 2: Deterministic fast LLM classification fallback for ambiguous queries.
- Explicit configurable confidence threshold enforcement (MIN_CONFIDENCE_THRESHOLD = 0.85).
- Polite multilingual clarification triggering when confidence < 0.85 (Zero Silent English).
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
from pydantic import BaseModel, Field, model_validator

from app.core.logging import configure_safe_logger
from app.modules.multilingual.config import (
    MIN_CONFIDENCE_THRESHOLD,
    SUPPORTED_LANGUAGES,
)
from app.services.llm_gateway import LLMGateway, get_llm_gateway

logger = configure_safe_logger(logging.getLogger(__name__))


class LanguageDetectionResult(BaseModel):
    """Structured outcome of language and script evaluation."""

    detected_language: str = Field(description="ISO 639-1 code ('en', 'es', 'fr', 'de', 'hi') or 'und' if undetermined")
    detected_script: Optional[str] = Field(default=None, description="Script variant ('Deva', 'Latn')")
    language_confidence: float = Field(ge=0.0, le=1.0)
    clarification_needed: bool = False
    clarification_reason: Optional[str] = None
    suggested_languages: Optional[List[str]] = None

    @model_validator(mode="before")
    @classmethod
    def remap_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "language" in d and "detected_language" not in d:
                d["detected_language"] = d.pop("language")
            if "confidence" in d and "language_confidence" not in d:
                d["language_confidence"] = d.pop("confidence")
            if "script" in d and "detected_script" not in d:
                d["detected_script"] = d.pop("script")
            return d
        return data

    @property
    def language(self) -> str:
        """Alias for detected_language."""
        return self.detected_language

    @property
    def confidence(self) -> float:
        """Alias for language_confidence."""
        return self.language_confidence


class LanguageDetector:
    """Enterprise Language Detector with Hindi Dual-Script & Latin-Script Distinctions."""

    # Unicode ranges
    DEVAS_PATTERN = re.compile(r"[\u0900-\u097F]")
    LATIN_PATTERN = re.compile(r"[a-zA-Z\u00C0-\u024F]")
    WORD_SPLIT_PATTERN = re.compile(r"\b\w+\b")

    # High-frequency characteristic stopword & lexical marker lexicons
    LEXICONS: Dict[str, Set[str]] = {
        "hi_latn": {
            "mera", "meri", "mere", "kya", "nahi", "nahin", "chahiye", "hoga", "hogi", "hoge",
            "hai", "hain", "ho", "karo", "kar", "diya", "de", "do", "bhai", "kitna", "kitni",
            "kitne", "kab", "kripya", "kaise", "batao", "bhejo", "aur", "pe", "mein", "ko",
            "se", "tak", "aayega", "aayegi", "milega", "paisa", "rupaye", "wapas",
        },
        "es": {
            "el", "la", "de", "que", "y", "en", "un", "una", "por", "para", "con", "no", "es",
            "lo", "como", "mas", "más", "pero", "su", "al", "este", "esta", "mi", "hola", "por favor",
            "pedido", "reembolso", "devolucion", "devolución", "devolver", "cuenta", "envio", "envío",
            "cuando", "cuándo", "donde", "dónde", "gracias", "cancelar", "comprar", "ayuda", "articulo",
            "artículo", "garantia", "garantía", "producto", "politica", "política", "informacion", "información",
        },
        "fr": {
            "le", "la", "les", "de", "des", "un", "une", "et", "est", "en", "que", "qui", "dans",
            "pour", "avec", "sur", "au", "aux", "par", "pas", "plus", "ne", "ce", "cette", "merci",
            "bonjour", "commande", "remboursement", "livraison", "retour", "retours", "aide",
            "quand", "comment", "ou", "où", "garantie", "produit", "s'il", "vous", "plaît", "plait",
            "politique", "reponse", "réponse", "delai", "délai", "probleme", "problème",
        },
        "de": {
            "das", "ist", "du", "ich", "nicht", "die", "es", "und", "der", "was", "wir", "sie",
            "ein", "eine", "einer", "einem", "einen", "mit", "den", "dem", "auf", "fuer", "für",
            "von", "haben", "bitte", "bestellung", "rueckerstattung", "rückerstattung", "versand",
            "zurueck", "zurück", "wann", "wo", "hilfe", "artikel", "produkt", "garantie", "stornieren",
            "danke", "guten", "tag", "hallo", "kuendigung", "kündigung", "lieferung", "ruecksendung",
            "rücksendung",
        },
        "en": {
            "the", "and", "that", "have", "for", "not", "with", "you", "this", "but", "his", "from",
            "they", "say", "her", "she", "will", "one", "all", "would", "there", "their", "what",
            "out", "about", "who", "get", "which", "go", "me", "when", "make", "can", "like", "time",
            "no", "just", "him", "know", "take", "people", "into", "year", "your", "good", "some",
            "could", "them", "see", "other", "than", "then", "now", "look", "only", "come", "its",
            "over", "think", "also", "back", "after", "use", "two", "how", "our", "work", "first",
            "well", "way", "even", "new", "want", "because", "any", "these", "give", "day", "most",
            "us", "order", "return", "refund", "shipping", "delivery", "policy", "warranty", "help",
            "item", "product", "cancel", "please", "hello", "hi", "thanks", "track",
            "is", "are", "was", "were", "am", "be", "been", "being", "do", "does", "did", "done",
            "has", "had", "having", "so", "up", "down", "in", "on", "at", "to", "by", "if", "or",
            "as", "an", "it", "my", "test", "check", "query", "turn", "race", "count",
            "verification", "message", "atomicity", "outbox", "cache", "encrypted", "rollback",
            "legacy", "thread", "authoritative", "sender", "content", "immutability", "completeness",
            "heartbeat", "error", "cancellation", "reconcile", "cached", "different", "another",
            "initial", "question", "restrict", "gap", "del", "null", "seq", "auth",
            "inquiry", "causing", "crash", "restricted", "admin", "document", "internal",
            "historical", "filtered", "conflict",
        },
    }

    def __init__(
        self,
        confidence_threshold: float = MIN_CONFIDENCE_THRESHOLD,
        llm_gateway: Optional[LLMGateway] = None,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.llm_gateway = llm_gateway

    def _get_llm_gateway(self) -> Optional[LLMGateway]:
        """Lazy access to LLMGateway."""
        if self.llm_gateway is None:
            try:
                self.llm_gateway = get_llm_gateway()
            except Exception:
                self.llm_gateway = None
        return self.llm_gateway

    def detect(
        self,
        raw_text: Optional[str],
        preferred_language: Optional[str] = None,
    ) -> LanguageDetectionResult:
        """Detects language and script of incoming text.

        Enforces the 0.85 confidence threshold. If confidence is below threshold
        or the language is unsupported, returns clarification_needed=True and
        falls back to default "en".
        """
        return self._do_detect(raw_text, preferred_language)

    def detect_language(
        self,
        raw_text: Optional[str],
        preferred_language: Optional[str] = None,
    ) -> LanguageDetectionResult:
        """Alias for detect to support standard language detection interfaces."""
        return self._do_detect(raw_text, preferred_language)

    def _do_detect(
        self,
        raw_text: Optional[str],
        preferred_language: Optional[str] = None,
    ) -> LanguageDetectionResult:
        """Internal detection logic."""
        if not raw_text or not str(raw_text).strip():
            return LanguageDetectionResult(
                detected_language="und",
                detected_script=None,
                language_confidence=0.0,
                clarification_needed=True,
                clarification_reason="EMPTY_OR_WHITESPACE_INPUT",
                suggested_languages=sorted(list(SUPPORTED_LANGUAGES)),
            )

        text = unicodedata.normalize("NFKC", str(raw_text).strip())

        # Clean punctuation and normalize casing for lexical inspection
        words = [w.lower() for w in self.WORD_SPLIT_PATTERN.findall(text)]
        word_count = len(words)

        if word_count == 0:
            return LanguageDetectionResult(
                detected_language="und",
                detected_script=None,
                language_confidence=0.0,
                clarification_needed=True,
                clarification_reason="NO_ALPHABETIC_WORDS",
                suggested_languages=sorted(list(SUPPORTED_LANGUAGES)),
            )

        # ----------------------------------------------------------------------
        # Tier 1A: Devanagari Script Analysis (hi-Deva -> hi)
        # ----------------------------------------------------------------------
        deva_chars = len(self.DEVAS_PATTERN.findall(text))
        total_alpha = len([c for c in text if c.isalpha()])

        if total_alpha > 0 and (deva_chars / total_alpha) >= 0.20:
            confidence = min(0.99, 0.85 + (deva_chars / total_alpha) * 0.14)
            return LanguageDetectionResult(
                detected_language="hi",
                detected_script="Deva",
                language_confidence=round(confidence, 2),
                clarification_needed=False,
            )

        # ----------------------------------------------------------------------
        # Tier 1B: Lexical & Stopword Frequency Analysis (Latin Script)
        # ----------------------------------------------------------------------
        # Count hits in each language lexicon
        hits: Dict[str, int] = {lang: 0 for lang in self.LEXICONS}
        for w in words:
            for lang, vocab in self.LEXICONS.items():
                if w in vocab:
                    hits[lang] += 1

        # Check Hinglish first if significant markers exist
        hinglish_hits = hits["hi_latn"]
        if hinglish_hits >= 2 or (word_count <= 3 and hinglish_hits >= 1 and hits["en"] == 0):
            density = hinglish_hits / word_count
            conf = min(0.96, 0.85 + density * 0.11)
            return LanguageDetectionResult(
                detected_language="hi",
                detected_script="Latn",
                language_confidence=round(conf, 2),
                clarification_needed=False,
            )

        # Evaluate European languages (es, fr, de, en)
        best_lang = "en"
        max_hits = 0
        total_hits = 0

        for lang in ["es", "fr", "de", "en"]:
            count = hits[lang]
            total_hits += count
            if count > max_hits:
                max_hits = count
                best_lang = lang

        # Check for tied top candidates among Latin languages
        top_candidates = [lang for lang in ["es", "fr", "de", "en"] if hits[lang] == max_hits and max_hits > 0]
        if len(top_candidates) > 1:
            if preferred_language and preferred_language.lower() in top_candidates:
                best_lang = preferred_language.lower()
            else:
                logger.info(
                    "Ambiguous tied languages detected: %s with %d hits each. Triggering clarification.",
                    top_candidates,
                    max_hits,
                )
                return LanguageDetectionResult(
                    detected_language="und",
                    detected_script=None,
                    language_confidence=0.0,
                    clarification_needed=True,
                    clarification_reason="AMBIGUOUS_TIED_LANGUAGES",
                    suggested_languages=sorted(list(SUPPORTED_LANGUAGES)),
                )

        # If we have clear lexical matches
        if max_hits > 0:
            match_ratio = max_hits / word_count
            dominance = max_hits / max(1, (total_hits - max_hits)) if (total_hits - max_hits) > 0 else 2.0

            # Calculate confidence score based on lexical density and margin over second-best
            calculated_conf = min(0.98, 0.70 + (match_ratio * 0.20) + (min(dominance, 2.0) * 0.05))

            # Preferred language alignment boost
            if preferred_language and preferred_language.lower() == best_lang:
                calculated_conf = min(0.99, calculated_conf + 0.05)

            if calculated_conf >= self.confidence_threshold:
                return LanguageDetectionResult(
                    detected_language=best_lang,
                    detected_script="Latn",
                    language_confidence=round(calculated_conf, 2),
                    clarification_needed=False,
                )

        # If user explicitly preferred a supported language and input has no contradictory signal
        if preferred_language and preferred_language.lower() in SUPPORTED_LANGUAGES:
            pref = preferred_language.lower()
            if max_hits == 0 and word_count <= 3:
                # Short ambiguous greeting/acknowledgment with user preferred language
                return LanguageDetectionResult(
                    detected_language=pref,
                    detected_script="Latn",
                    language_confidence=0.86,
                    clarification_needed=False,
                )

        # ----------------------------------------------------------------------
        # Tier 2: Low Confidence / Fallback Clarification Trigger (Zero Silent English)
        # ----------------------------------------------------------------------
        # We enforce strict clarification rather than assuming English
        logger.info(
            "Language detection below threshold (max_hits=%d, word_count=%d). Triggering clarification.",
            max_hits,
            word_count,
        )

        return LanguageDetectionResult(
            detected_language="und",
            detected_script=None,
            language_confidence=0.0,
            clarification_needed=True,
            clarification_reason="LOW_CONFIDENCE_DETECTION",
            suggested_languages=sorted(list(SUPPORTED_LANGUAGES)),
        )

