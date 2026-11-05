"""SQLAlchemy 2.0 ORM Entity Models Registry.

Registers all domain models with the Declarative Base for Alembic migrations
and application-wide persistence.
"""

from app.db.base import Base
from app.models.auth import Role, User, UserRole
from app.models.chat import ChatIdempotencyRecord, ChatOutboxEvent
from app.models.customer import Conversation, Customer, Message, Session
from app.models.document import Document, DocumentChunk, DocumentVersion
from app.models.evidence import (
    EvidenceFile,
    EvidenceVerification,
    ExtractedEvidenceEntity,
)
from app.models.knowledge_index import BenchmarkRun, KnowledgeIndex
from app.models.sentiment import SentimentLog
from app.models.ticket import EscalationAudit, SLACalendar, Ticket, TicketEvent

__all__ = [
    "Base",
    # Auth & RBAC
    "User",
    "Role",
    "UserRole",
    # Customer & Conversation
    "Customer",
    "Session",
    "Conversation",
    "Message",
    # Chat Idempotency & Outbox
    "ChatIdempotencyRecord",
    "ChatOutboxEvent",
    # Sentiment
    "SentimentLog",
    # Knowledge & Vector Chunks
    "Document",
    "DocumentVersion",
    "DocumentChunk",
    "KnowledgeIndex",
    "BenchmarkRun",
    # Multimodal Evidence
    "EvidenceFile",
    "ExtractedEvidenceEntity",
    "EvidenceVerification",
    # Ticketing & SLA
    "SLACalendar",
    "Ticket",
    "EscalationAudit",
    "TicketEvent",
]

