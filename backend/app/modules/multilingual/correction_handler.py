"""Deterministic Customer Correction & Slot Context Engine (Task 6 Criterion 10).

Detects customer corrections (e.g. 'Actually, my order is 9999, not 1234',
'Correction: tracking number is TRK-8888, please ignore 1234'), maintains an authoritative
slot state with complete revision history, and reconciles downstream ticket synthesis identifiers.
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class SlotValue(BaseModel):
    """Authoritative representation of a domain slot with revision history."""

    slot_name: str
    current_value: str
    superseded_values: List[str] = Field(default_factory=list)
    is_corrected: bool = False
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provenance: str = "explicit"


class CorrectionDetection(BaseModel):
    """Detection outcome for an explicit customer correction utterance."""

    has_correction: bool
    slot_name: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    raw_pattern: Optional[str] = None


# Entity extraction patterns
_SLOT_PATTERNS = {
    "order_id": [
        re.compile(r"\b(ORD-[A-Za-z0-9\-]+)\b", re.IGNORECASE),
        re.compile(r"(?i)\border\b(?:\s*(?:id|number|#))?\s*[:#-]?\s*([A-Za-z0-9#\-_]{4,20})\b"),
    ],
    "tracking_number": [
        re.compile(r"\b(TRK-[A-Za-z0-9\-]+)\b", re.IGNORECASE),
        re.compile(r"(?i)\btracking\b(?:\s*(?:number|id|code|#))?\s*[:#-]?\s*([A-Za-z0-9#\-_]{4,25})\b"),
    ],
    "invoice_number": [
        re.compile(r"\b(INV-[A-Za-z0-9\-]+)\b", re.IGNORECASE),
        re.compile(r"(?i)\binvoice\b(?:\s*(?:number|id|#))?\s*[:#-]?\s*([A-Za-z0-9#\-_]{4,20})\b"),
    ],
    "email": [
        re.compile(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b"),
    ],
}

# Explicit customer correction patterns
_CORRECTION_PATTERNS = [
    # "not 1234, but 9999" or "not 1234, it is 9999" or "not 1234, use 9999"
    (
        re.compile(r"(?i)\bnot\s+([A-Za-z0-9#\-_]+),?\s+(?:but|use|it(?:'s| is)|rather)\s+([A-Za-z0-9#\-_]+)\b"),
        lambda m: (m.group(1).strip(" #"), m.group(2).strip(" #")),
    ),
    # "instead of 1234, use 9999" or "instead of 1234, it's 9999"
    (
        re.compile(r"(?i)\binstead of\s+([A-Za-z0-9#\-_]+),?\s+(?:use\s+|it(?:'s| is)\s+)?([A-Za-z0-9#\-_]+)\b"),
        lambda m: (m.group(1).strip(" #"), m.group(2).strip(" #")),
    ),
    # "use 9999 instead of 1234" or "TRK-8888 instead of TRK-1111"
    (
        re.compile(r"(?i)\b(?:use\s+|it(?:'s| is)\s+|tracking\s+|order\s+)?([A-Za-z0-9#\-_]+)\s+instead of\s+([A-Za-z0-9#\-_]+)\b"),
        lambda m: (m.group(2).strip(" #"), m.group(1).strip(" #")),
    ),
    # "actually my order is 9999, not 1234"
    (
        re.compile(r"(?i)\b(?:actually|correction|wait|sorry|my bad)[,:]?\s+(?:my\s+)?(\w+)\s+(?:is|was)\s+([A-Za-z0-9#\-_]+),?\s+not\s+([A-Za-z0-9#\-_]+)\b"),
        lambda m: (m.group(3).strip(" #"), m.group(2).strip(" #")),
    ),
    # "actually, order is 9999" / "correction: order 9999"
    (
        re.compile(r"(?i)\b(?:actually|correction|wait|sorry)[,:]?\s+(?:my\s+)?(?:order|tracking|invoice)?\s*(?:is|was|should be|#)?\s*([A-Za-z0-9#\-_]{4,20})\b"),
        lambda m: (None, m.group(1).strip(" #")),
    ),
]


class SlotCorrectionEngine:
    """Manages deterministic slot extraction, correction tracking, and context reconciliation."""

    def __init__(self) -> None:
        pass

    def detect_correction(
        self,
        text: str,
        current_slots: Optional[Dict[str, SlotValue]] = None,
    ) -> CorrectionDetection:
        """Inspects text for explicit customer correction signals."""
        if not text or not text.strip():
            return CorrectionDetection(has_correction=False)

        cleaned = text.strip()

        for pattern, extractor in _CORRECTION_PATTERNS:
            match = pattern.search(cleaned)
            if match:
                old_val, new_val = extractor(match)

                # Infer slot name
                slot_name = self._infer_slot_type(new_val, old_val, current_slots)

                return CorrectionDetection(
                    has_correction=True,
                    slot_name=slot_name,
                    old_value=old_val,
                    new_value=new_val,
                    raw_pattern=match.group(0),
                )

        return CorrectionDetection(has_correction=False)

    def _infer_slot_type(
        self,
        new_val: Optional[str],
        old_val: Optional[str],
        current_slots: Optional[Dict[str, SlotValue]],
    ) -> str:
        """Determines whether value represents order_id, tracking_number, or invoice_number."""
        check_val = (new_val or old_val or "").upper()
        if "TRK" in check_val:
            return "tracking_number"
        if "INV" in check_val:
            return "invoice_number"
        if "ORD" in check_val:
            return "order_id"

        # Check existing slots
        if current_slots:
            for sname, sval in current_slots.items():
                if old_val and (sval.current_value.upper() == old_val.upper() or old_val.upper() in [v.upper() for v in sval.superseded_values]):
                    return sname

        return "order_id"

    def extract_slots(self, text: str) -> Dict[str, str]:
        """Extracts candidate slots using regex patterns."""
        extracted: Dict[str, str] = {}
        for slot_name, patterns in _SLOT_PATTERNS.items():
            for p in patterns:
                m = p.search(text)
                if m:
                    extracted[slot_name] = m.group(1).strip(" #").upper()
                    break
        return extracted

    def process_turn(
        self,
        message: str,
        active_slots: Optional[Dict[str, SlotValue]] = None,
    ) -> Tuple[Dict[str, SlotValue], Optional[CorrectionDetection]]:
        """Processes a customer message turn, detecting corrections and updating active slots."""
        slots = dict(active_slots or {})
        now = datetime.now(timezone.utc)

        # 1. Check for explicit correction
        corr = self.detect_correction(message, slots)
        if corr.has_correction and corr.new_value:
            sname = corr.slot_name or "order_id"
            old_v = corr.old_value

            if sname in slots:
                prev_slot = slots[sname]
                superseded = list(prev_slot.superseded_values)
                if prev_slot.current_value and prev_slot.current_value != corr.new_value:
                    superseded.append(prev_slot.current_value)
                if old_v and old_v not in superseded and old_v != corr.new_value:
                    superseded.append(old_v)

                slots[sname] = SlotValue(
                    slot_name=sname,
                    current_value=corr.new_value,
                    superseded_values=superseded,
                    is_corrected=True,
                    last_updated_at=now,
                    provenance="explicit_correction",
                )
            else:
                superseded = [old_v] if old_v else []
                slots[sname] = SlotValue(
                    slot_name=sname,
                    current_value=corr.new_value,
                    superseded_values=superseded,
                    is_corrected=True,
                    last_updated_at=now,
                    provenance="explicit_correction",
                )

            return slots, corr

        # 2. Extract standard slots if no correction
        newly_extracted = self.extract_slots(message)
        for sname, sval in newly_extracted.items():
            if sname not in slots:
                slots[sname] = SlotValue(
                    slot_name=sname,
                    current_value=sval,
                    superseded_values=[],
                    is_corrected=False,
                    last_updated_at=now,
                    provenance="explicit",
                )

        return slots, None

    def process_conversation_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, SlotValue]:
        """Processes an ordered list of messages ({role, content}) chronologically."""
        slots: Dict[str, SlotValue] = {}
        for msg in messages:
            content = msg.get("content", "")
            if content:
                slots, _ = self.process_turn(content, slots)
        return slots

    def resolve_authoritative_identifiers(
        self,
        raw_identifiers: List[Dict[str, str]],
        slots: Dict[str, SlotValue],
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Reconciles raw extracted identifiers with authoritative slots, returning (clean_identifiers, contradictions)."""
        clean_identifiers: List[Dict[str, str]] = []
        contradictions: List[Dict[str, str]] = []

        # Build map of superseded values across all slots
        superseded_map: Dict[str, Tuple[str, str]] = {}  # old_val -> (slot_name, active_value)
        active_map: Dict[str, str] = {}  # slot_name -> active_value

        for sname, sval in slots.items():
            active_map[sname] = sval.current_value.upper()
            for sup in sval.superseded_values:
                superseded_map[sup.upper()] = (sname, sval.current_value.upper())

        seen_keys: Set[str] = set()
        seen_contradiction_keys: Set[str] = set()

        for ident in raw_identifiers:
            itype = ident.get("type", "UNKNOWN")
            ival = ident.get("value", "").upper()

            if ival in superseded_map:
                sname, active_val = superseded_map[ival]
                contra_key = f"{sname}:{ival}:{active_val}"
                if contra_key not in seen_contradiction_keys:
                    seen_contradiction_keys.add(contra_key)
                    contradictions.append({
                        "field": sname,
                        "description": f"Customer corrected {sname} from '{ival}' to '{active_val}' (authoritative: '{active_val}')",
                    })
                # Replace with active value if not already added
                key = f"{itype}:{active_val}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    clean_identifiers.append({"type": itype, "value": active_val})
            else:
                key = f"{itype}:{ival}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    clean_identifiers.append(ident)

        return clean_identifiers, contradictions
