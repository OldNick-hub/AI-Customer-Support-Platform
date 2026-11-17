"""Multi-Intent Query Decomposition Engine (Task 6 Criterion 9).

Deconstructs compound customer messages containing multiple distinct inquiries or requests
into atomic sub-queries while preserving entity tokens and customer context.
Synthesizes structured multi-part responses addressing all individual requests coherently.
"""

from dataclasses import dataclass
import logging
import re
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class SubQuery(BaseModel):
    """An individual atomic sub-query extracted from a compound message."""

    index: int
    text: str
    intent_category: Optional[str] = None


class DecompositionResult(BaseModel):
    """Result of multi-intent analysis and query decomposition."""

    original_query: str
    is_multi_intent: bool
    sub_queries: List[SubQuery] = Field(default_factory=list)


# Words that typically introduce a new inquiry
_QUESTION_STARTERS = (
    "what", "how", "can", "could", "where", "why", "when", "is", "are",
    "do", "does", "will", "would", "please", "i need", "i want", "tell me"
)

# Connectors that split distinct intents
_MULTI_INTENT_CONNECTORS = [
    r"\b(?:also|additionally|furthermore|moreover|plus)\b",
    r"\band\s+(?:also|can\s+you|could\s+you|how\s+do|what\s+is|where\s+can|how\s+can)\b",
    r"\bas\s+well\s+as\b",
]


class QueryDecomposer:
    """Decomposes compound, multi-intent customer messages into atomic sub-queries."""

    def __init__(self, min_sub_query_words: int = 3) -> None:
        self.min_sub_query_words = min_sub_query_words

    def decompose(self, query: str) -> DecompositionResult:
        """Analyzes query text and decomposes into sub-queries if multiple intents are present."""
        if not query or not query.strip():
            return DecompositionResult(original_query="", is_multi_intent=False, sub_queries=[])

        text = query.strip()

        # 1. Numbered or bulleted list patterns (e.g., '1. ... 2. ...' or '- ... \n - ...')
        numbered_parts = self._split_numbered_or_bulleted(text)
        if len(numbered_parts) > 1:
            valid_subs = [
                SubQuery(index=i, text=p)
                for i, p in enumerate(numbered_parts)
                if len(p.split()) >= self.min_sub_query_words
            ]
            if len(valid_subs) > 1:
                return DecompositionResult(
                    original_query=text,
                    is_multi_intent=True,
                    sub_queries=valid_subs,
                )

        # 2. Multiple question marks with inquiry phrases
        question_parts = self._split_by_question_marks(text)
        if len(question_parts) > 1:
            valid_subs = [
                SubQuery(index=i, text=p)
                for i, p in enumerate(question_parts)
                if len(p.split()) >= self.min_sub_query_words
            ]
            if len(valid_subs) > 1:
                return DecompositionResult(
                    original_query=text,
                    is_multi_intent=True,
                    sub_queries=valid_subs,
                )

        # 3. Explicit connector boundaries ('also', 'and can you', 'furthermore')
        connector_parts = self._split_by_connectors(text)
        if len(connector_parts) > 1:
            valid_subs = [
                SubQuery(index=i, text=p)
                for i, p in enumerate(connector_parts)
                if len(p.split()) >= self.min_sub_query_words
            ]
            if len(valid_subs) > 1:
                return DecompositionResult(
                    original_query=text,
                    is_multi_intent=True,
                    sub_queries=valid_subs,
                )

        # Single intent fallback
        return DecompositionResult(
            original_query=text,
            is_multi_intent=False,
            sub_queries=[SubQuery(index=0, text=text)],
        )

    def _split_numbered_or_bulleted(self, text: str) -> List[str]:
        """Splits text on numbered items ('1. ', '2) ') or bullet points ('- ', '* ')."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1 and all(re.match(r"^(?:\d+[\.\)]|[-*•])\s+", l) for l in lines):
            cleaned = [re.sub(r"^(?:\d+[\.\)]|[-*•])\s+", "", l).strip() for l in lines]
            return [c for c in cleaned if c]

        # Inline numbered patterns: '1. ... 2. ...'
        inline_splits = re.split(r"(?<=[.!?\s])\b(?:\d+[\.\)]|[-*•])\s+", text)
        if len(inline_splits) > 1:
            cleaned = [re.sub(r"^(?:\d+[\.\)]|[-*•])\s+", "", s).strip() for s in inline_splits]
            return [c for c in cleaned if c]

        return []

    def _split_by_question_marks(self, text: str) -> List[str]:
        """Splits text on question marks followed by new inquiry clauses."""
        parts = re.split(r"\?\s*", text)
        results = []
        for p in parts:
            p_clean = p.strip()
            if not p_clean:
                continue
            # Restore question mark if not ending in punctuation
            if not p_clean.endswith((".", "!", "?")):
                p_clean = f"{p_clean}?"
            results.append(p_clean)

        return results if len(results) > 1 else []

    def _split_by_connectors(self, text: str) -> List[str]:
        """Splits sentences across coordinating multi-intent markers like 'also' or 'additionally'."""
        pattern = r"(?:[.,;]|\band\b)?\s+(?:also|additionally|furthermore|moreover|plus|and\s+how\s+can\s+i|and\s+where\s+can\s+i|and\s+what\s+is)\b[,:]?\s*"
        splits = re.split(pattern, text, flags=re.IGNORECASE)
        cleaned = [s.strip(" ,.;:") for s in splits if s.strip()]
        return cleaned if len(cleaned) > 1 else []

    def synthesize_combined_response(self, sub_responses: List[Tuple[SubQuery, str]]) -> str:
        """Combines answers for individual sub-queries into a unified coherent response."""
        if not sub_responses:
            return "No information found for your request."

        if len(sub_responses) == 1:
            return sub_responses[0][1]

        sections = []
        for i, (sub_q, answer) in enumerate(sub_responses, start=1):
            clean_ans = answer.strip()
            sections.append(f"### Question {i}: {sub_q.text}\n{clean_ans}")

        return "\n\n".join(sections)

