"""Advanced RAG Knowledge Assistant Module.

Exports security context resolver, 3-layer retriever, grounded answer generator,
citation manager, and claim verification engine.
"""

from app.modules.rag.citation import (
    CitationManager,
    CitationMetadata,
    ClaimCitationVerifier,
    ClaimVerificationResult,
)
from app.modules.rag.generator import (
    GroundedAnswerGenerator,
    RAGResolutionResult,
)
from app.modules.rag.orchestrator import (
    RAGInternalRequest,
    RAGKnowledgeAssistant,
    RAGOrchestrator,
)
from app.modules.rag.retriever import (
    RetrievalResult,
    RetrievedChunk,
    SecurityAwareRetriever,
)
from app.modules.rag.sanitizer import (
    RAGSanitizer,
    SanitizationResult,
    get_rag_sanitizer,
)
from app.modules.rag.security import (
    CUSTOMER_ALLOWED_CATEGORIES,
    SAFE_GLOBAL_CATEGORIES,
    SecurityContext,
    SecurityContextResolver,
)

__all__ = [
    "SecurityContext",
    "SecurityContextResolver",
    "SecurityAwareRetriever",
    "RetrievedChunk",
    "RetrievalResult",
    "SAFE_GLOBAL_CATEGORIES",
    "CUSTOMER_ALLOWED_CATEGORIES",
    "CitationManager",
    "CitationMetadata",
    "ClaimCitationVerifier",
    "ClaimVerificationResult",
    "GroundedAnswerGenerator",
    "RAGResolutionResult",
    "RAGSanitizer",
    "SanitizationResult",
    "get_rag_sanitizer",
    "RAGInternalRequest",
    "RAGOrchestrator",
    "RAGKnowledgeAssistant",
]

