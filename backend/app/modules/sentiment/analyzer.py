"""Sentiment and Frustration Trajectory Analysis Engine.

Evaluates customer sentiment, frustration index, urgency index, frustration slopes,
and detects repeated negative interactions across multi-turn conversations.
"""

from dataclasses import dataclass
import logging
import re
from typing import List, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.models.customer import Message
from app.models.sentiment import SentimentLog

logger = configure_safe_logger(logging.getLogger(__name__))

# Lexical indicators for fine-grained sentiment analysis
_POSITIVE_PATTERNS = [
    r"\b(thank\s*you|thanks|great|excellent|awesome|helpful|appreciate|resolved|perfect|wonderful|love|good job)\b",
]

_URGENT_PATTERNS = [
    r"\b(urgent|emergency|immediately|asap|critical|right now|down|blocking|production down|system crash|outage)\b",
]

_FRUSTRATED_PATTERNS = [
    r"\b(ridiculous|unacceptable|furious|horrible|worst|useless|scam|terrible|incompetent|disgusted|waste of time|angry|broken)\b",
    r"\b(still not fixed|again and again|fourth time|third time|nobody helps|fed up|lawyer|sue|cancel my account)\b",
    r"[!?]{2,}",  # Multiple exclamation or question marks e.g. "??", "!!", "!?!?"
]

_NEGATIVE_PATTERNS = [
    r"\b(not working|doesn't work|fail|failed|failure|broken|error|issue|problem|bug|can't|cannot|unable to|wrong)\b",
    r"\b(disappointed|annoyed|unhappy|bad|poor|slow|frustrated)\b",
]

_SARCASTIC_PATTERNS = [
    r"\b(oh great, another|wonderful, now it's|thanks for nothing|just brilliant|as expected.*not)\b",
]


@dataclass(frozen=True)
class SentimentAnalysisResult:
    """Detailed sentiment scoring for a single message."""

    sentiment_class: str  # 'positive', 'neutral', 'negative', 'frustrated', 'urgent', 'sarcastic'
    confidence_score: float
    frustration_index: float  # 0.0 - 1.0
    urgency_index: float  # 0.0 - 1.0
    trajectory_slope: float  # -1.0 to 1.0
    requires_escalation: bool
    escalation_reason: Optional[str] = None


@dataclass(frozen=True)
class ConversationSentimentAssessment:
    """Conversation-level assessment for repeated negative interactions."""

    repeated_negative: bool
    negative_turn_count: int
    latest_sentiment_class: str
    latest_frustration_index: float
    latest_urgency_index: float
    overall_slope: float
    requires_escalation: bool
    escalation_reason: Optional[str] = None


class SentimentAnalyzer:
    """Heuristic and lexical sentiment evaluation engine."""

    def __init__(
        self,
        frustration_threshold: Optional[float] = None,
        urgency_threshold: Optional[float] = None,
        repeated_negative_threshold: Optional[int] = None,
    ) -> None:
        self.frustration_threshold = (
            frustration_threshold
            if frustration_threshold is not None
            else getattr(settings, "ESCALATION_FRUSTRATION_INDEX_THRESHOLD", 0.70)
        )
        self.urgency_threshold = (
            urgency_threshold
            if urgency_threshold is not None
            else getattr(settings, "ESCALATION_URGENCY_INDEX_THRESHOLD", 0.80)
        )
        self.repeated_negative_threshold = (
            repeated_negative_threshold
            if repeated_negative_threshold is not None
            else getattr(settings, "ESCALATION_REPEATED_NEGATIVE_THRESHOLD", 2)
        )

    def analyze_text(
        self,
        text: str,
        prior_frustrations: Optional[Sequence[float]] = None,
    ) -> SentimentAnalysisResult:
        """Analyzes single message text and computes sentiment, frustration, and urgency indices."""
        raw = text.strip()
        lower = raw.lower()

        # Check for ALL CAPS (shouting)
        words = [w for w in raw.split() if w.isalpha()]
        caps_ratio = 0.0
        if len(words) >= 3:
            caps_count = sum(1 for w in words if w.isupper() and len(w) > 1)
            caps_ratio = caps_count / len(words)

        # Match counts
        pos_matches = sum(len(re.findall(p, lower)) for p in _POSITIVE_PATTERNS)
        urg_matches = sum(len(re.findall(p, lower)) for p in _URGENT_PATTERNS)
        fru_matches = sum(len(re.findall(p, lower)) for p in _FRUSTRATED_PATTERNS)
        neg_matches = sum(len(re.findall(p, lower)) for p in _NEGATIVE_PATTERNS)
        sarc_matches = sum(len(re.findall(p, lower)) for p in _SARCASTIC_PATTERNS)

        # Compute Indices
        frustration = 0.0
        urgency = 0.0

        if fru_matches > 0:
            frustration += min(0.4 + 0.2 * fru_matches, 0.95)
        if caps_ratio >= 0.5:
            frustration += 0.25
        if neg_matches > 0 and fru_matches == 0:
            frustration += min(0.2 + 0.15 * neg_matches, 0.65)
        if sarc_matches > 0:
            frustration += 0.35

        # Positive dampening
        if pos_matches > 0 and fru_matches == 0:
            frustration = max(0.0, frustration - 0.3 * pos_matches)

        if urg_matches > 0:
            urgency += min(0.5 + 0.2 * urg_matches, 1.0)
        elif "asap" in lower or "immediately" in lower:
            urgency += 0.6

        frustration = min(max(frustration, 0.0), 1.0)
        urgency = min(max(urgency, 0.0), 1.0)

        # Trajectory slope
        history = list(prior_frustrations or [])
        history.append(frustration)
        slope = 0.0
        if len(history) >= 2:
            slope = (history[-1] - history[0]) / max(len(history) - 1, 1)

        # Class determination
        if sarc_matches > 0 and frustration >= 0.4:
            s_class = "sarcastic"
            conf = 0.85
        elif urgency >= self.urgency_threshold or (urg_matches > 0 and frustration >= 0.5):
            s_class = "urgent"
            conf = 0.90
        elif frustration >= self.frustration_threshold or fru_matches >= 2:
            s_class = "frustrated"
            conf = 0.92
        elif pos_matches > neg_matches and frustration < 0.3:
            s_class = "positive"
            conf = 0.90
        elif neg_matches > 0 or frustration >= 0.35:
            s_class = "negative"
            conf = 0.85
        elif pos_matches > 0 and frustration < 0.3:
            s_class = "positive"
            conf = 0.90
        else:
            s_class = "neutral"
            conf = 0.80

        # Check escalation requirements
        requires_esc = False
        esc_reason = None

        if s_class == "urgent" or urgency >= self.urgency_threshold:
            requires_esc = True
            esc_reason = f"High urgency index detected ({urgency:.2f})"
        elif s_class == "frustrated" or frustration >= self.frustration_threshold:
            requires_esc = True
            esc_reason = f"Severe customer frustration detected (index: {frustration:.2f})"
        elif slope >= 0.30 and frustration >= 0.50:
            requires_esc = True
            esc_reason = f"Accelerating frustration trajectory slope ({slope:.2f})"

        return SentimentAnalysisResult(
            sentiment_class=s_class,
            confidence_score=conf,
            frustration_index=round(frustration, 3),
            urgency_index=round(urgency, 3),
            trajectory_slope=round(slope, 3),
            requires_escalation=requires_esc,
            escalation_reason=esc_reason,
        )

    def evaluate_conversation(
        self,
        messages: Sequence[Message],
        sentiment_logs: Optional[Sequence[SentimentLog]] = None,
    ) -> ConversationSentimentAssessment:
        """Evaluates multi-turn conversation history to detect repeated negative interactions (Rule A)."""
        customer_messages = [m for m in messages if m.sender_type == "CUSTOMER"]

        logs_by_message_id = {}
        if sentiment_logs:
            for l in sentiment_logs:
                logs_by_message_id[str(l.message_id)] = l

        frustrations: List[float] = []
        negative_turns = 0
        latest_class = "neutral"
        latest_frustration = 0.0
        latest_urgency = 0.0

        for msg in customer_messages:
            log_entry = logs_by_message_id.get(str(msg.id))
            if log_entry is not None:
                s_class = log_entry.sentiment_class
                f_idx = log_entry.frustration_index
                u_idx = log_entry.urgency_index
            else:
                eval_res = self.analyze_text(msg.content, prior_frustrations=frustrations)
                s_class = eval_res.sentiment_class
                f_idx = eval_res.frustration_index
                u_idx = eval_res.urgency_index

            frustrations.append(f_idx)
            latest_class = s_class
            latest_frustration = f_idx
            latest_urgency = u_idx

            if s_class in ("negative", "frustrated", "urgent", "sarcastic") or f_idx >= 0.45:
                negative_turns += 1

        # Calculate overall slope
        slope = 0.0
        if len(frustrations) >= 2:
            slope = (frustrations[-1] - frustrations[0]) / max(len(frustrations) - 1, 1)

        # Repeated negative threshold check (Rule A)
        repeated_negative = negative_turns >= self.repeated_negative_threshold
        requires_esc = False
        esc_reason = None

        if repeated_negative:
            requires_esc = True
            esc_reason = (
                f"Repeated negative sentiment across {negative_turns} turns "
                f"(threshold: {self.repeated_negative_threshold})"
            )
        elif latest_frustration >= self.frustration_threshold:
            requires_esc = True
            esc_reason = f"Customer frustration threshold exceeded ({latest_frustration:.2f})"
        elif latest_urgency >= self.urgency_threshold:
            requires_esc = True
            esc_reason = f"Customer urgency threshold exceeded ({latest_urgency:.2f})"

        return ConversationSentimentAssessment(
            repeated_negative=repeated_negative,
            negative_turn_count=negative_turns,
            latest_sentiment_class=latest_class,
            latest_frustration_index=round(latest_frustration, 3),
            latest_urgency_index=round(latest_urgency, 3),
            overall_slope=round(slope, 3),
            requires_escalation=requires_esc,
            escalation_reason=esc_reason,
        )


# Default singleton instance
default_sentiment_analyzer = SentimentAnalyzer()
