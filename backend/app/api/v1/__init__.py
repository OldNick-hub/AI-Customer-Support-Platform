"""API v1 Router Aggregator.

Mounts all Module v1 routers under /api/v1.
"""

from fastapi import APIRouter

from app.api.v1.chat import router as chat_router
from app.api.v1.escalation import router as escalation_router
from app.api.v1.evidence import router as evidence_router

api_v1_router = APIRouter()

# Register Conversational Chat & Advanced RAG router (/api/v1/chat/message)
api_v1_router.include_router(chat_router)

# Register Intelligent Escalation & After-Hours router (/api/v1/escalation)
api_v1_router.include_router(escalation_router)

# Register Multimodal Evidence & OCR router (/api/v1/evidence)
api_v1_router.include_router(evidence_router)

__all__ = ["api_v1_router"]

