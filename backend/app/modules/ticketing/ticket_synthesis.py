"""Ticket Synthesis Engine.

Transforms a multi-message customer conversation into a structured support ticket
by extracting the core problem, identifiers, category, resolution sought, actions
already attempted, evidence references, and missing information.

Design principles:
- Explicit facts are distinguished from inferred conclusions in extraction_provenance.
- Missing information is never fabricated; unresolved fields are surfaced explicitly.
- LLM output is parsed defensively; any parse failure falls back to heuristic extraction.
- PII is masked before persisting extracted text.
- Tenant isolation is enforced by callers, not by this module.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.config import Settings, settings as default_settings
from app.core.logging import configure_safe_logger
from app.core.pii import PIIMaskingEngine
from app.services.llm_gateway import LLMGateway, LLMResponse, get_llm_gateway

logger = configure_safe_logger(logging.getLogger(__name__))

# ---------------------------------------------------------------------------
# Supported issue categories and their canonical labels
# ---------------------------------------------------------------------------
KNOWN_CATEGORIES: Dict[str, List[str]] = {
    "delivery_issue": ["delivery", "shipping", "courier", "package", "tracking", "not arrived", "late"],
    "payment_issue": ["payment", "charged", "billing", "refund", "double charge", "overcharged", "transaction"],
    "damaged_product": ["damaged", "broken", "defective", "faulty", "cracked", "not working"],
    "account_issue": ["account", "login", "password", "access", "locked", "suspended", "profile"],
    "product_quality": ["quality", "wrong item", "incorrect", "not as described", "counterfeit"],
    "cancellation_return": ["cancel", "return", "exchange", "replacement"],
    "technical_support": ["technical", "error", "bug", "crash", "app", "website", "software"],
    "general": [],
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SynthesisRequest(BaseModel):
    """Input to the ticket synthesis engine."""
    conversation_id: str
    ticket_id: str
    messages: List[Dict[str, Any]]  # [{role, content, created_at}]
    evidence_ids: List[str] = Field(default_factory=list)
    force_resynthesis: bool = False


class SynthesisResult(BaseModel):
    """Structured extraction result from the conversation synthesis."""
    problem_statement: str
    product_or_service: Optional[str] = None
    reference_identifiers: List[Dict[str, str]] = Field(default_factory=list)
    issue_category: str = "general"
    issue_subcategory: Optional[str] = None
    resolution_sought: Optional[str] = None
    actions_already_attempted: List[str] = Field(default_factory=list)
    linked_evidence_ids: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    contradictions: List[Dict[str, str]] = Field(default_factory=list)
    conversation_context_summary: Optional[str] = None
    extraction_confidence: float = 0.0
    extraction_provenance: Dict[str, str] = Field(default_factory=dict)
    llm_model_used: str = "fallback_heuristic"


# ---------------------------------------------------------------------------
# System instruction for LLM-based synthesis
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM_INSTRUCTION = """You are a support ticket analysis engine.
Your task is to extract structured information from a customer support conversation.

RULES:
1. Extract only information that is EXPLICITLY stated or STRONGLY implied by the customer messages.
2. Mark inferred information clearly in extraction_provenance as "inferred".
3. Mark explicitly stated information as "explicit".
4. NEVER invent order IDs, amounts, dates, or product names not present in the conversation.
5. If a field is missing, include its name in missing_fields.
6. If two messages contradict each other (e.g., different order IDs), include both in contradictions.
7. Summarize only customer messages — do not echo agent responses as the customer's problem.
8. Output valid JSON only. No prose, no markdown code fences."""

_SYNTHESIS_PROMPT_TEMPLATE = """Analyze the following customer support conversation and extract a structured ticket.

CONVERSATION:
{conversation_text}

EVIDENCE FILES REFERENCED: {evidence_ids}

Respond with a JSON object matching EXACTLY this schema (no extra keys):
{{
  "problem_statement": "<concise statement of the customer's core problem>",
  "product_or_service": "<product or service name, or null>",
  "reference_identifiers": [
    {{"type": "<ORDER_ID|INVOICE_NUMBER|TRACKING_NUMBER|TRANSACTION_REF|TICKET_NUMBER|OTHER>", "value": "<value>"}}
  ],
  "issue_category": "<delivery_issue|payment_issue|damaged_product|account_issue|product_quality|cancellation_return|technical_support|general>",
  "issue_subcategory": "<more specific sub-label, or null>",
  "resolution_sought": "<what the customer wants to happen, or null>",
  "actions_already_attempted": ["<action1>", "<action2>"],
  "missing_fields": ["<field1>", "<field2>"],
  "contradictions": [
    {{"field": "<field_name>", "description": "<description of contradiction>"}}
  ],
  "conversation_context_summary": "<2-3 sentence summary of conversation history>",
  "extraction_confidence": <0.0 to 1.0>,
  "extraction_provenance": {{
    "problem_statement": "explicit",
    "product_or_service": "inferred",
    "issue_category": "inferred"
  }}
}}"""


# ---------------------------------------------------------------------------
# Heuristic fallback extractor (no LLM required)
# ---------------------------------------------------------------------------

_ORDER_ID_RE = re.compile(
    r"\b(ORD-[A-Z0-9\-]{3,30})\b|\b(?:order|ord)\b(?:\s*(?:id|num|number|#))?\s*(?:is|:)?\s*([A-Z0-9\-]{4,30})\b",
    re.IGNORECASE,
)
_INVOICE_RE = re.compile(
    r"\b(INV-[A-Z0-9\-]{3,30})\b|\b(?:invoice|inv)\b(?:\s*(?:id|num|number|#))?\s*(?:is|:)?\s*([A-Z0-9\-]{4,30})\b",
    re.IGNORECASE,
)
_TRACKING_RE = re.compile(
    r"\b(TRK-[A-Z0-9\-]{3,30})\b|\b(?:tracking|track)\b(?:\s*(?:id|num|number|#))?\s*(?:is|:)?\s*([A-Z0-9\-]{4,30})\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:USD|INR|EUR|GBP|Rs\.?|₹|\$|€|£)\s?[\d,]+(?:\.\d{1,2})?", re.IGNORECASE)


def _heuristic_extract(messages: List[Dict[str, Any]], evidence_ids: List[str]) -> SynthesisResult:
    """Deterministic fallback extraction using regex heuristics."""
    pii_masker = PIIMaskingEngine()
    customer_texts: List[str] = []
    for msg in messages:
        role = msg.get("role", msg.get("sender_type", ""))
        content = msg.get("content", "")
        if role.upper() in ("CUSTOMER",):
            customer_texts.append(content)

    combined = " ".join(customer_texts)
    masked_combined = pii_masker.mask(combined)

    # Extract identifiers
    identifiers: List[Dict[str, str]] = []
    for m in _ORDER_ID_RE.finditer(combined):
        val = (m.group(1) or m.group(2) or "").strip(" -#:")
        if len(val) >= 4 and val.lower() not in ("id", "num", "number", "issue"):
            identifiers.append({"type": "ORDER_ID", "value": val.upper()})
    for m in _INVOICE_RE.finditer(combined):
        val = (m.group(1) or m.group(2) or "").strip(" -#:")
        if len(val) >= 4 and val.lower() not in ("id", "num", "number"):
            identifiers.append({"type": "INVOICE_NUMBER", "value": val.upper()})
    for m in _TRACKING_RE.finditer(combined):
        val = (m.group(1) or m.group(2) or "").strip(" -#:")
        if len(val) >= 4 and val.lower() not in ("id", "num", "number"):
            identifiers.append({"type": "TRACKING_NUMBER", "value": val.upper()})

    # Deduplicate and reconcile identifiers with customer slot corrections
    from app.modules.multilingual.correction_handler import SlotCorrectionEngine
    slot_engine = SlotCorrectionEngine()
    slots = slot_engine.process_conversation_messages(messages)
    unique_identifiers, contradictions = slot_engine.resolve_authoritative_identifiers(
        identifiers, slots
    )

    # Determine category
    text_lower = combined.lower()
    detected_category = "general"
    for cat, keywords in KNOWN_CATEGORIES.items():
        if any(kw in text_lower for kw in keywords):
            detected_category = cat
            break

    # Build problem statement from first substantial customer message
    problem = masked_combined[:500].strip() if masked_combined else "Customer reported an issue."

    # Build provenance
    provenance: Dict[str, str] = {
        "problem_statement": "explicit",
        "issue_category": "inferred",
    }
    if unique_identifiers:
        provenance["reference_identifiers"] = "explicit"

    missing: List[str] = []
    if detected_category == "delivery_issue" and not any(
        i["type"] in ("ORDER_ID", "TRACKING_NUMBER") for i in unique_identifiers
    ):
        missing.append("order_id_or_tracking_number")
    if detected_category == "payment_issue" and not any(
        i["type"] in ("ORDER_ID", "INVOICE_NUMBER") for i in unique_identifiers
    ):
        missing.append("order_id_or_invoice_number")

    confidence = 0.4 if unique_identifiers else 0.2

    return SynthesisResult(
        problem_statement=problem,
        reference_identifiers=unique_identifiers,
        issue_category=detected_category,
        linked_evidence_ids=evidence_ids,
        missing_fields=missing,
        contradictions=contradictions,
        extraction_confidence=confidence,
        extraction_provenance=provenance,
        llm_model_used="fallback_heuristic",
    )


def _parse_llm_response(raw: str, evidence_ids: List[str], model: str) -> Optional[SynthesisResult]:
    """Parse and validate LLM JSON response into SynthesisResult."""
    # Strip markdown fences if LLM wraps output
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("LLM synthesis JSON parse failure: %s", exc)
        return None

    # Validate required fields
    if not isinstance(data.get("problem_statement"), str) or not data["problem_statement"].strip():
        return None

    # Normalize category
    category = str(data.get("issue_category", "general")).lower().strip()
    if category not in KNOWN_CATEGORIES:
        category = "general"

    # Merge evidence IDs
    llm_evidence = data.get("linked_evidence_ids", []) or []
    merged_evidence = list(set(evidence_ids + [str(e) for e in llm_evidence]))

    return SynthesisResult(
        problem_statement=str(data["problem_statement"])[:2000],
        product_or_service=str(data["product_or_service"])[:500] if data.get("product_or_service") else None,
        reference_identifiers=data.get("reference_identifiers") or [],
        issue_category=category,
        issue_subcategory=str(data["issue_subcategory"])[:100] if data.get("issue_subcategory") else None,
        resolution_sought=str(data["resolution_sought"])[:2000] if data.get("resolution_sought") else None,
        actions_already_attempted=data.get("actions_already_attempted") or [],
        linked_evidence_ids=merged_evidence,
        missing_fields=data.get("missing_fields") or [],
        contradictions=data.get("contradictions") or [],
        conversation_context_summary=(
            str(data["conversation_context_summary"])[:1000]
            if data.get("conversation_context_summary") else None
        ),
        extraction_confidence=float(data.get("extraction_confidence", 0.5)),
        extraction_provenance=data.get("extraction_provenance") or {},
        llm_model_used=model,
    )


def _format_conversation(messages: List[Dict[str, Any]]) -> str:
    """Format conversation messages into a readable text block for the LLM prompt."""
    pii_masker = PIIMaskingEngine()
    lines: List[str] = []
    for msg in messages:
        role = msg.get("role", msg.get("sender_type", "UNKNOWN")).upper()
        content = msg.get("content", "")
        # Mask PII before sending to LLM
        safe_content = pii_masker.mask(content)
        ts = msg.get("created_at", "")
        ts_str = f" [{ts}]" if ts else ""
        lines.append(f"{role}{ts_str}: {safe_content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ticket Synthesis Engine
# ---------------------------------------------------------------------------

class TicketSynthesisEngine:
    """LLM-backed engine that extracts structured ticket information from conversations.

    Falls back gracefully to heuristic extraction on LLM unavailability, timeout,
    or parse failure. The fallback is deterministic and never fabricates information.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        llm_gateway: Optional[LLMGateway] = None,
    ) -> None:
        self._settings = settings or default_settings
        self._gateway: Optional[LLMGateway] = llm_gateway

    def _get_gateway(self) -> LLMGateway:
        if self._gateway is None:
            self._gateway = get_llm_gateway(self._settings)
        return self._gateway

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Extract structured ticket information from a conversation.

        Always returns a result — LLM failures fall back to heuristic extraction.
        """
        messages = request.messages
        evidence_ids = request.evidence_ids

        if not messages:
            logger.warning(
                "Ticket synthesis requested for conversation %s with zero messages.",
                request.conversation_id,
            )
            return SynthesisResult(
                problem_statement="No conversation messages available for synthesis.",
                issue_category="general",
                extraction_confidence=0.0,
                extraction_provenance={"problem_statement": "explicit"},
                llm_model_used="fallback_heuristic",
            )

        conversation_text = _format_conversation(messages)
        prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
            conversation_text=conversation_text,
            evidence_ids=", ".join(evidence_ids) if evidence_ids else "none",
        )

        # --- Attempt LLM synthesis ---
        llm_result: Optional[SynthesisResult] = None
        try:
            gateway = self._get_gateway()
            response: LLMResponse = await gateway.generate(
                prompt=prompt,
                system_instruction=_SYNTHESIS_SYSTEM_INSTRUCTION,
                temperature=0.0,
                max_output_tokens=1024,
                timeout_seconds=self._settings.LLM_TIMEOUT_SECONDS,
            )
            llm_result = _parse_llm_response(response.content, evidence_ids, response.model)
            if llm_result is None:
                logger.warning(
                    "LLM synthesis parse failed for conversation %s. Using heuristic fallback.",
                    request.conversation_id,
                )
        except Exception as exc:
            logger.warning(
                "LLM synthesis failed for conversation %s (%s). Using heuristic fallback.",
                request.conversation_id,
                type(exc).__name__,
            )

        if llm_result is not None:
            # Reconcile customer corrections against LLM extracted identifiers
            from app.modules.multilingual.correction_handler import SlotCorrectionEngine
            slot_engine = SlotCorrectionEngine()
            slots = slot_engine.process_conversation_messages(messages)
            if slots:
                clean_ids, contradictions = slot_engine.resolve_authoritative_identifiers(
                    llm_result.reference_identifiers, slots
                )
                llm_result.reference_identifiers = clean_ids
                if contradictions:
                    llm_result.contradictions.extend(contradictions)
            return llm_result

        # --- Heuristic fallback ---
        return _heuristic_extract(messages, evidence_ids)


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_default_synthesis_engine: Optional[TicketSynthesisEngine] = None


def get_synthesis_engine(settings: Optional[Settings] = None) -> TicketSynthesisEngine:
    """Returns the shared TicketSynthesisEngine singleton."""
    global _default_synthesis_engine
    if _default_synthesis_engine is None:
        _default_synthesis_engine = TicketSynthesisEngine(settings=settings)
    return _default_synthesis_engine

