"""API v1 Router Aggregator.

Mounts all Module v1 routers under /api/v1.
"""

from fastapi import APIRouter

from app.api.v1.chat import router as chat_router

api_v1_router = APIRouter()

# Register Conversational Chat & Advanced RAG router (/api/v1/chat/message)
api_v1_router.include_router(chat_router)

__all__ = ["api_v1_router"]

