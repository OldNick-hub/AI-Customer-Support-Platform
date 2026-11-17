"""API v1 Router Aggregator.

Mounts all Module v1 routers under /api/v1.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.escalation import router as escalation_router
from app.api.v1.evidence import router as evidence_router
from app.api.v1.reconciliation import router as reconciliation_router
from app.api.v1.tickets import router as tickets_router
from app.api.v1.analytics import router as analytics_router

api_v1_router = APIRouter()

# Register Authentication & Dev Personas router (/api/v1/auth)
api_v1_router.include_router(auth_router)

# Register Conversational Dialog & History router (/api/v1/conversations)
api_v1_router.include_router(conversations_router)

# Register Conversational Chat & Advanced RAG router (/api/v1/chat/message)
api_v1_router.include_router(chat_router)

# Register Intelligent Escalation & After-Hours router (/api/v1/escalation)
api_v1_router.include_router(escalation_router)

# Register Multimodal Evidence & OCR router (/api/v1/evidence)
api_v1_router.include_router(evidence_router)

# Register Evidence Reconciliation & Mismatch Detection router (/api/v1/evidence/reconcile)
api_v1_router.include_router(reconciliation_router)

# Register Intelligent Ticket Management router (/api/v1/tickets)
api_v1_router.include_router(tickets_router)

# Register Analytics & Operational Intelligence router (/api/v1/analytics)
api_v1_router.include_router(analytics_router)

__all__ = ["api_v1_router"]

