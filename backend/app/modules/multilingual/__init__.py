"""Multilingual Support & Conversation Lifecycle Module.

Phase 1 Components:
- PreTranslationSecurityScanner, PreTranslationSecurityResult
- EntityPreservationVault
- LanguageDetector, LanguageDetectionResult
- Configuration and constants
"""

from app.modules.multilingual.config import (
    IDLE_TIMEOUT_MINUTES,
    LOCALIZED_SERVICE_NOTICES,
    MIN_CONFIDENCE_THRESHOLD,
    MULTILINGUAL_CLARIFICATION_PROMPT,
    RESTORATION_WINDOW_HOURS,
    SUPPORTED_LANGUAGES,
    SUPPORTED_SCRIPTS,
)
from app.modules.multilingual.entity_preserver import EntityPreservationVault
from app.modules.multilingual.language_detector import (
    LanguageDetectionResult,
    LanguageDetector,
)
from app.modules.multilingual.security_scanner import (
    PreTranslationSecurityResult,
    PreTranslationSecurityScanner,
)
from app.modules.multilingual.lifecycle_manager import (
    ConversationLifecycleManager,
    LifecycleStatusEvaluation,
    SessionLifecycleState,
    SessionRestorationResult,
)
from app.modules.multilingual.translator import (
    TranslationResult,
    Translator,
)

__all__ = [
    "SUPPORTED_LANGUAGES",
    "SUPPORTED_SCRIPTS",
    "MIN_CONFIDENCE_THRESHOLD",
    "IDLE_TIMEOUT_MINUTES",
    "RESTORATION_WINDOW_HOURS",
    "LOCALIZED_SERVICE_NOTICES",
    "MULTILINGUAL_CLARIFICATION_PROMPT",
    "PreTranslationSecurityScanner",
    "PreTranslationSecurityResult",
    "EntityPreservationVault",
    "LanguageDetector",
    "LanguageDetectionResult",
    "Translator",
    "TranslationResult",
    "ConversationLifecycleManager",
    "SessionLifecycleState",
    "LifecycleStatusEvaluation",
    "SessionRestorationResult",
]

