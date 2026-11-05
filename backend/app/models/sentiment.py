"""Sentiment Analysis and Frustration Trajectory Models.

Tracks fine-grained multi-class sentiment classifications, frustration slopes,
and automated escalation triggers per customer message.
"""

import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, UUIDPrimaryKeyMixin, utc_now


class SentimentLog(Base, UUIDPrimaryKeyMixin):
    """Detailed sentiment evaluation per message turn."""

    __tablename__ = "sentiment_logs"

    message_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("messages.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    sentiment_class: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True
    )  # 'positive', 'neutral', 'negative', 'frustrated', 'urgent', 'sarcastic'
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    frustration_index: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    urgency_index: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trajectory_slope: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    requires_escalation: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    escalation_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    message: Mapped["Message"] = relationship("Message", back_populates="sentiment_log")  # type: ignore[name-defined]

