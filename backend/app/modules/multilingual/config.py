"""Configuration Constants & Localized Templates for Multilingual Support.

Defines supported language codes, script variants, tunable confidence thresholds,
session lifecycle parameters, and static pre-rendered localized fallback notices.
"""

from typing import Dict, Set
import re

# Supported ISO 639-1 languages
SUPPORTED_LANGUAGES: Set[str] = {"en", "es", "fr", "de", "hi"}

# Script variants mapping
SUPPORTED_SCRIPTS: Dict[str, Set[str]] = {
    "hi": {"Deva", "Latn"},
    "en": {"Latn"},
    "es": {"Latn"},
    "fr": {"Latn"},
    "de": {"Latn"},
}

# Configured confidence threshold (tunable system parameter)
MIN_CONFIDENCE_THRESHOLD: float = 0.85

# Conversational session lifecycle durations
IDLE_TIMEOUT_MINUTES: int = 30
IDLE_TIMEOUT_SECONDS: int = 30 * 60  # 1800 seconds
RESTORATION_WINDOW_HOURS: int = 24
RESTORATION_WINDOW_SECONDS: int = 24 * 3600  # 86400 seconds
HOT_CONTEXT_WINDOW_SIZE: int = 10  # 10 turns (5 customer turns + 5 agent turns)

# Redis ephemeral cache TTLs
REDIS_SESSION_TTL_SECONDS: int = 3600  # 1 hour
REDIS_TRANSLATION_TTL_SECONDS: int = 86400  # 24 hours

# Pre-rendered polite localized service notices (Zero Silent English)
LOCALIZED_SERVICE_NOTICES: Dict[str, str] = {
    "es": (
        "Lo sentimos, nuestro servicio de traducción no está disponible temporalmente. "
        "Su consulta ha sido registrada y un agente de soporte le responderá a la brevedad."
    ),
    "fr": (
        "Désolé, notre service de traduction est temporairement indisponible. "
        "Votre demande a été enregistrée et un agent d'assistance vous répondra sous peu."
    ),
    "de": (
        "Entschuldigung, unser Übersetzungsdienst ist vorübergehend nicht verfügbar. "
        "Ihre Anfrage wurde erfasst und ein Support-Mitarbeiter wird sich in Kürze bei Ihnen melden."
    ),
    "hi": (
        "क्षमा करें, हमारी अनुवाद सेवा वर्तमान में अनुपलब्ध है। "
        "आपका अनुरोध दर्ज कर लिया गया है और हमारा सहायता प्रतिनिधि शीघ्र ही आपसे संपर्क करेगा।"
    ),
    "en": (
        "We apologize, but our processing service is temporarily unavailable. "
        "Your inquiry has been logged and a support representative will assist you shortly."
    ),
}

# Multi-lingual polite clarification prompt
MULTILINGUAL_CLARIFICATION_PROMPT: str = (
    "We could not determine your preferred language with certainty. / "
    "No pudimos determinar su idioma con certeza. / "
    "Nous n'avons pas pu déterminer votre langue avec certitude. / "
    "Wir konnten Ihre Sprache nicht eindeutig feststellen. / "
    "हम आपकी भाषा की पुष्टि नहीं कर सके। "
    "Please reply in English, Español, Français, Deutsch, or हिंदी."
)

# Business entity preservation patterns
ORDER_ID_PATTERN = re.compile(r"\bORD-[A-Za-z0-9\-]+\b", re.IGNORECASE)
DOC_CODE_PATTERN = re.compile(r"\b(?:POL|SOP|GUIDE|FAQ|DOC)-[A-Za-z0-9\-]+\b", re.IGNORECASE)
SKU_PATTERN = re.compile(r"\b(?:SKU-[A-Za-z0-9]+|[A-Z]{2,4}-\d{4,8})\b")
CURRENCY_PATTERN = re.compile(r"(?:\$|€|£|₹)\s*\d+(?:\.\d{2})?\b")
ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CITATION_PATTERN = re.compile(r"\[\d+(?:,\s*\d+)*\]")
SINGLE_CITATION_PATTERN = re.compile(r"\[(\d+)\]")

# Placeholder format regex for validation: __PRESERVED_{TYPE}_{TOKEN128}__
# where TOKEN128 is 32 hexadecimal characters (128 bits of entropy)
PLACEHOLDER_REGEX = re.compile(
    r"__PRESERVED_(ORDER|CODE|SKU|DATE|AMT|CIT_\d+|PII_[A-Z_]+)_([0-9a-f]{32})__"
)
