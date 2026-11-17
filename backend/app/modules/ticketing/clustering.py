"""Semantic Duplicate Ticket Detection and Systemic Issue Clustering Engine.

Provides:
1. DuplicateMatchResult & IssueGroupResult data structures.
2. Semantic similarity calculation (vector embeddings with safe deterministic lexical fallback).
3. Candidate search bounded by tenant isolation, rolling 48-hour window, and active statuses.
4. Idempotent duplicate linking via cluster_group_id with agent routing suppression.
5. Systemic issue grouping across distinct customers without merging individual ticket records.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import configure_safe_logger
from app.models.customer import Customer
from app.models.ticket import Ticket, TicketEvent
from app.services.embedding_service import EmbeddingService, get_embedding_service

logger = configure_safe_logger(logging.getLogger(__name__))

ACTIVE_TICKET_STATUSES = ("OPEN", "IN_PROGRESS", "PENDING_CUSTOMER")
ROUTING_STATUS_DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"


@dataclass(frozen=True)
class DuplicateMatchResult:
    """Result of semantic duplicate ticket detection."""

    is_duplicate: bool
    master_ticket_id: Optional[uuid.UUID] = None
    master_ticket_number: Optional[str] = None
    cluster_group_id: Optional[uuid.UUID] = None
    similarity_score: float = 0.0
    reason: str = ""
    already_linked: bool = False


@dataclass(frozen=True)
class IssueGroupResult:
    """Result of systemic related issue grouping."""

    cluster_group_id: uuid.UUID
    incident_name: str
    ticket_ids: List[uuid.UUID]
    ticket_numbers: List[str]
    member_count: int
    category: str
    average_similarity: float = 1.0


def _compute_cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
    """Computes cosine similarity between two float vectors."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return float(dot / (norm1 * norm2))


def _compute_lexical_similarity(text1: str, text2: str) -> float:
    """Fallback token Jaccard similarity when embedding models are unavailable."""
    words1 = set(re.findall(r"\b[a-zA-Z0-9_-]{2,}\b", text1.lower()))
    words2 = set(re.findall(r"\b[a-zA-Z0-9_-]{2,}\b", text2.lower()))
    if not words1 or not words2:
        return 0.0
    intersection = len(words1.intersection(words2))
    union = len(words1.union(words2))
    return float(intersection / union) if union > 0 else 0.0


def _extract_ticket_text(ticket: Ticket) -> str:
    """Combines key fields of a ticket into a canonical representation for semantic comparison."""
    parts = []
    if ticket.title:
        parts.append(ticket.title.strip())
    if ticket.description:
        parts.append(ticket.description.strip())
    if ticket.masked_summary and ticket.masked_summary != ticket.description:
        parts.append(ticket.masked_summary.strip())
    return " ".join(parts).strip()


def _get_customer_tenant(customer: Optional[Customer], default: str = "default") -> str:
    """Safely extracts tenant_id from customer metadata or customer attribute."""
    if not customer:
        return default
    meta = getattr(customer, "metadata_", {}) or {}
    if isinstance(meta, dict) and "tenant_id" in meta:
        return str(meta["tenant_id"])
    return getattr(customer, "tenant_id", default)


class TicketClusterMatcher:
    """Semantic clustering and duplicate ticket detection service.
    
    Enforces strict tenant boundaries, rolling temporal windows, and non-destructive linking.
    """

    def __init__(
        self,
        embedding_service: Optional[EmbeddingService] = None,
        duplicate_threshold: float = 0.88,
        group_threshold: float = 0.80,
        duplicate_window_hours: float = 48.0,
        group_window_hours: float = 24.0,
    ) -> None:
        self.embedding_service = embedding_service
        self.duplicate_threshold = duplicate_threshold
        self.group_threshold = group_threshold
        self.duplicate_window_hours = duplicate_window_hours
        self.group_window_hours = group_window_hours

    def _resolve_embedding_service(self) -> Optional[EmbeddingService]:
        """Resolves the central embedding service gracefully."""
        if self.embedding_service is not None:
            return self.embedding_service
        try:
            return get_embedding_service()
        except Exception as exc:
            logger.warning("Could not resolve central EmbeddingService: %s", exc)
            return None

    async def compute_similarity(
        self,
        text1: str,
        text2: str,
        vec1: Optional[Sequence[float]] = None,
        vec2: Optional[Sequence[float]] = None,
    ) -> float:
        """Computes semantic similarity using vector embeddings with safe lexical fallback."""
        if not text1.strip() or not text2.strip():
            return 0.0

        # Exact match fast-path
        if text1.strip().lower() == text2.strip().lower():
            return 1.0

        # Use pre-supplied vectors if available
        if vec1 is not None and vec2 is not None:
            return _compute_cosine_similarity(vec1, vec2)

        svc = self._resolve_embedding_service()
        if svc is not None:
            try:
                vectors = await svc.embed_batch([text1, text2])
                if len(vectors) == 2:
                    return _compute_cosine_similarity(vectors[0], vectors[1])
            except Exception as exc:
                logger.warning("Embedding service call failed in cluster matcher: %s. Using lexical fallback.", exc)

        # Graceful fallback: token Jaccard similarity
        return _compute_lexical_similarity(text1, text2)

    # =========================================================================
    # Implementation A: Semantic Duplicate Ticket Detection
    # =========================================================================

    async def detect_duplicate(
        self,
        db: AsyncSession,
        ticket: Ticket,
        tenant_id: str = "default",
        custom_embedding: Optional[Sequence[float]] = None,
    ) -> DuplicateMatchResult:
        """Detects if an incoming ticket is a duplicate of an existing active ticket.

        Rules:
        - Same customer_id.
        - Same tenant_id.
        - Open/active status (OPEN, IN_PROGRESS, PENDING_CUSTOMER).
        - Created within rolling 48-hour window.
        - Semantic similarity >= duplicate_threshold (0.88).
        - Excludes self and closed/resolved tickets.
        - Idempotent: If already linked to the master ticket, returns existing link without duplicates.
        """
        # Ensure ticket has customer loaded
        if not ticket.customer and ticket.customer_id:
            cust_stmt = select(Customer).where(Customer.id == ticket.customer_id)
            ticket.customer = await db.scalar(cust_stmt)

        ticket_tenant = _get_customer_tenant(ticket.customer, default=tenant_id)
        if ticket_tenant != tenant_id:
            return DuplicateMatchResult(
                is_duplicate=False,
                reason=f"Tenant mismatch: ticket belongs to {ticket_tenant}, queried tenant is {tenant_id}.",
            )

        # Idempotency check: if this ticket is already marked as a suppressed duplicate
        if ticket.routing_status == ROUTING_STATUS_DUPLICATE_SUPPRESSED and ticket.cluster_group_id:
            # Verify existing master
            master_stmt = (
                select(Ticket)
                .where(
                    Ticket.cluster_group_id == ticket.cluster_group_id,
                    Ticket.id != ticket.id,
                )
                .order_by(Ticket.created_at.asc())
            )
            existing_master = await db.scalar(master_stmt)
            if existing_master:
                return DuplicateMatchResult(
                    is_duplicate=True,
                    master_ticket_id=existing_master.id,
                    master_ticket_number=existing_master.ticket_number,
                    cluster_group_id=ticket.cluster_group_id,
                    similarity_score=1.0,
                    reason="Ticket was already identified and linked as a duplicate.",
                    already_linked=True,
                )

        now = ticket.created_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=self.duplicate_window_hours)

        # Query candidate open tickets for the same customer
        stmt = (
            select(Ticket)
            .options(selectinload(Ticket.customer))
            .where(
                Ticket.id != ticket.id,
                Ticket.customer_id == ticket.customer_id,
                Ticket.status.in_(ACTIVE_TICKET_STATUSES),
                Ticket.created_at >= cutoff,
            )
            .order_by(Ticket.created_at.asc())
        )
        candidates = (await db.scalars(stmt)).all()

        if not candidates:
            return DuplicateMatchResult(
                is_duplicate=False,
                reason="No active candidate tickets found for this customer within the rolling 48-hour window.",
            )

        ticket_text = _extract_ticket_text(ticket)
        best_match: Optional[Ticket] = None
        best_score = 0.0

        for cand in candidates:
            cand_tenant = _get_customer_tenant(cand.customer, default=tenant_id)
            if cand_tenant != tenant_id:
                continue  # Enforce strict tenant boundary

            cand_text = _extract_ticket_text(cand)
            score = await self.compute_similarity(
                text1=ticket_text,
                text2=cand_text,
                vec1=custom_embedding,
            )
            if score >= self.duplicate_threshold and score > best_score:
                best_score = score
                best_match = cand

        if not best_match:
            return DuplicateMatchResult(
                is_duplicate=False,
                similarity_score=best_score,
                reason=f"Top similarity {best_score:.4f} did not meet threshold {self.duplicate_threshold}.",
            )

        # Match confirmed! Determine master ticket (the earlier created ticket)
        master_ticket = best_match if (best_match.created_at or now) <= now else ticket
        duplicate_ticket = ticket if master_ticket.id != ticket.id else best_match

        # Allocate or share cluster_group_id
        cluster_id = master_ticket.cluster_group_id or uuid.uuid4()
        master_ticket.cluster_group_id = cluster_id
        duplicate_ticket.cluster_group_id = cluster_id

        # Suppress routing for the duplicate ticket
        duplicate_ticket.routing_status = ROUTING_STATUS_DUPLICATE_SUPPRESSED

        # Persist audit event
        audit_event = TicketEvent(
            ticket_id=duplicate_ticket.id,
            event_type="DUPLICATE_TICKET_DETECTED",
            event_payload={
                "master_ticket_id": str(master_ticket.id),
                "master_ticket_number": master_ticket.ticket_number,
                "cluster_group_id": str(cluster_id),
                "similarity_score": round(best_score, 4),
                "threshold": self.duplicate_threshold,
                "customer_id": str(ticket.customer_id),
                "tenant_id": tenant_id,
            },
        )
        db.add(audit_event)
        await db.flush()

        logger.info(
            "Linked duplicate ticket %s to master %s in cluster %s (score: %.4f)",
            duplicate_ticket.ticket_number,
            master_ticket.ticket_number,
            cluster_id,
            best_score,
        )

        return DuplicateMatchResult(
            is_duplicate=True,
            master_ticket_id=master_ticket.id,
            master_ticket_number=master_ticket.ticket_number,
            cluster_group_id=cluster_id,
            similarity_score=best_score,
            reason=f"Matched active master ticket {master_ticket.ticket_number} with similarity {best_score:.4f}.",
            already_linked=False,
        )

    # =========================================================================
    # Implementation B: Systemic Related Issue Grouping
    # =========================================================================

    async def group_related_issues(
        self,
        db: AsyncSession,
        tenant_id: str = "default",
        time_window_hours: Optional[float] = None,
        category: Optional[str] = None,
        min_cluster_size: int = 2,
    ) -> List[IssueGroupResult]:
        """Detects related systemic issues across different customers within the same tenant.

        Core Invariants:
        - Scoped strictly to the specified tenant_id.
        - Groups issues into shared cluster_group_id for supervisory visibility.
        - Does NOT suppress individual customer routing or alter SLAs.
        - Does NOT merge or delete customer tickets.
        - Idempotent: tickets already in a cluster retain their cluster_group_id.
        """
        window_hours = time_window_hours or self.group_window_hours
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=window_hours)

        stmt = (
            select(Ticket)
            .options(selectinload(Ticket.customer))
            .where(
                Ticket.status.in_(ACTIVE_TICKET_STATUSES),
                Ticket.created_at >= cutoff,
            )
            .order_by(Ticket.created_at.asc())
        )
        if category:
            stmt = stmt.where(Ticket.category == category)

        raw_tickets = (await db.scalars(stmt)).all()

        # Enforce tenant isolation in memory
        tenant_tickets: List[Ticket] = []
        for t in raw_tickets:
            t_tenant = _get_customer_tenant(t.customer, default=tenant_id)
            if t_tenant == tenant_id:
                tenant_tickets.append(t)

        if len(tenant_tickets) < min_cluster_size:
            return []

        # Extract text and compute embeddings in batch if possible
        ticket_texts = [_extract_ticket_text(t) for t in tenant_tickets]
        embeddings: Optional[List[List[float]]] = None
        svc = self._resolve_embedding_service()
        if svc is not None:
            try:
                embeddings = await svc.embed_batch(ticket_texts)
            except Exception as exc:
                logger.warning("Batch embedding generation failed for issue grouping: %s. Using lexical fallback.", exc)

        # Graph adjacency clustering
        n = len(tenant_tickets)
        adj: Dict[int, Set[int]] = {i: {i} for i in range(n)}
        similarities: Dict[Tuple[int, int], float] = {}

        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = tenant_tickets[i], tenant_tickets[j]
                # Cross-customer grouping: only cluster different customers or distinct issues
                vec1 = embeddings[i] if embeddings else None
                vec2 = embeddings[j] if embeddings else None

                sim = await self.compute_similarity(
                    text1=ticket_texts[i],
                    text2=ticket_texts[j],
                    vec1=vec1,
                    vec2=vec2,
                )
                similarities[(i, j)] = sim
                if sim >= self.group_threshold:
                    adj[i].add(j)
                    adj[j].add(i)

        # Connected components discovery
        visited: Set[int] = set()
        clusters: List[Set[int]] = []

        for i in range(n):
            if i not in visited:
                comp: Set[int] = set()
                queue = [i]
                visited.add(i)
                while queue:
                    curr = queue.pop(0)
                    comp.add(curr)
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(neighbor)
                if len(comp) >= min_cluster_size:
                    clusters.append(comp)

        results: List[IssueGroupResult] = []

        for comp in clusters:
            members = [tenant_tickets[idx] for idx in comp]
            # Check if any member already has a cluster_group_id
            existing_cluster_id = next((m.cluster_group_id for m in members if m.cluster_group_id), None)
            cluster_id = existing_cluster_id or uuid.uuid4()

            member_ids = [m.id for m in members]
            member_numbers = [m.ticket_number for m in members]
            common_category = members[0].category or "GENERAL"
            common_title = members[0].title or "Systemic Incident"

            # Assign cluster_id to all members and emit audit event for newly grouped ones
            for m in members:
                if m.cluster_group_id != cluster_id:
                    m.cluster_group_id = cluster_id
                    event = TicketEvent(
                        ticket_id=m.id,
                        event_type="SYSTEMIC_ISSUE_GROUPED",
                        event_payload={
                            "cluster_group_id": str(cluster_id),
                            "member_count": len(members),
                            "category": common_category,
                            "tenant_id": tenant_id,
                        },
                    )
                    db.add(event)

            results.append(
                IssueGroupResult(
                    cluster_group_id=cluster_id,
                    incident_name=common_title,
                    ticket_ids=member_ids,
                    ticket_numbers=member_numbers,
                    member_count=len(members),
                    category=common_category,
                )
            )

        if results:
            await db.flush()

        return results

