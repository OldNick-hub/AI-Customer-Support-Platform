"""Knowledge Base FAISS Snapshots, Golden Benchmarks, and Watchdog State Models.

Tracks index activation history, Blue/Green candidate promotion, Golden QA evaluation runs,
and restart-recoverable watchdog canary state in PostgreSQL.
"""

import uuid
from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSONBType, UUIDPrimaryKeyMixin, utc_now


class KnowledgeIndex(Base, UUIDPrimaryKeyMixin):
    """Vector index release snapshot with restart-recoverable rollback watchdog state."""

    __tablename__ = "knowledge_indexes"

    index_version_tag: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )  # E.g., 'idx_20261101_v1'
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    total_vectors: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="CANDIDATE", nullable=False, index=True
    )  # 'CANDIDATE', 'CANDIDATE_READY', 'ACTIVE', 'PREVIOUS_ACTIVE', 'ROLLED_BACK'
    benchmark_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Durable watchdog state persisting across worker restarts
    watchdog_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    canary_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    canary_status: Mapped[str] = mapped_column(
        String(50), default="IDLE", nullable=False, index=True
    )  # 'IDLE', 'MONITORING', 'PASSED', 'FAILED_ROLLED_BACK'
    last_health_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollback_target_tag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    benchmark_runs: Mapped[List["BenchmarkRun"]] = relationship(
        "BenchmarkRun", back_populates="knowledge_index", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_knowledge_watchdog", "canary_status", "watchdog_expires_at"),
    )


class BenchmarkRun(Base, UUIDPrimaryKeyMixin):
    """Golden QA evaluation run measuring semantic grounding score and latency against an index."""

    __tablename__ = "benchmark_runs"

    index_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("knowledge_indexes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    benchmark_suite_version: Mapped[str] = mapped_column(String(50), nullable=False)
    total_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    grounding_score: Mapped[float] = mapped_column(Float, nullable=False)
    latency_p95_ms: Mapped[float] = mapped_column(Float, nullable=False)
    is_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    diagnostics: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    knowledge_index: Mapped["KnowledgeIndex"] = relationship(
        "KnowledgeIndex", back_populates="benchmark_runs"
    )

