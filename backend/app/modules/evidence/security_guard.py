"""Advanced Multimodal Evidence Security Guard.

Provides defense-in-depth protection against adversarial visual and document attacks:
1. Unicode normalization and zero-width/homoglyph obfuscation detection.
2. Multilingual and safely bounded encoded injection detection (Base64, URL, Hex).
3. PDF metadata and Document Information dictionary inspection.
4. Image metadata (EXIF / IPTC / XMP) inspection.
5. Invisible and suspicious PDF text heuristics.
6. Markdown image exfiltration and model delimiter neutralization.
7. Customer conversational claim injection detection.
8. Calibrated, auditable security outcome policies without false-positive over-blocking.
"""

import base64
from enum import Enum
import io
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
import urllib.parse

from PIL import Image
from PIL.ExifTags import TAGS
import pypdf
from pydantic import BaseModel, Field

from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class EvidenceSecurityOutcome(str, Enum):
    """Categorized security verdict for evaluated evidence documents and claims."""

    CLEAN = "CLEAN"
    FLAGGED_SUSPICIOUS = "FLAGGED_SUSPICIOUS"
    FLAGGED_UNSAFE = "FLAGGED_UNSAFE"
    SECURITY_INJECTION_DETECTED = "SECURITY_INJECTION_DETECTED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class SecurityFinding(BaseModel):
    """Single atomic security risk finding detected during document or claim evaluation."""

    category: str
    severity: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    rule_name: str
    description: str
    context_sample: Optional[str] = None


class SecurityScanResult(BaseModel):
    """Comprehensive verdict returned by the AdversarialEvidenceGuard."""

    outcome: EvidenceSecurityOutcome
    is_safe: bool
    is_malicious: bool
    quarantine_reason: Optional[str] = None
    findings: List[SecurityFinding] = Field(default_factory=list)
    normalized_text: str = ""
    sanitized_text: str = ""
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)
    has_critical_finding: bool = False


# ==============================================================================
# OBFUSCATION & HOMOGLYPH DEFINITIONS
# ==============================================================================

ZERO_WIDTH_CHARS: Set[str] = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u2060",  # word joiner
    "\ufeff",  # byte order mark / zero-width no-break space
    "\u00ad",  # soft hyphen
}

# Common Cyrillic and Greek homoglyphs mapped to ASCII equivalents for inspection copy
HOMOGLYPH_MAP: Dict[str, str] = {
    "а": "a", "А": "A",
    "в": "b", "В": "B",
    "с": "c", "С": "C",
    "е": "e", "Е": "E",
    "н": "h", "Н": "H",
    "і": "i", "І": "I",
    "ј": "j", "Ј": "J",
    "к": "k", "К": "K",
    "м": "m", "М": "M",
    "о": "o", "О": "O",
    "р": "p", "Р": "P",
    "ѕ": "s", "Ѕ": "S",
    "т": "t", "Т": "T",
    "х": "x", "Х": "X",
    "у": "y", "У": "Y",
}


# ==============================================================================
# MULTILINGUAL & ADVERSARIAL PATTERNS
# ==============================================================================

# English
ENGLISH_INJECTION_PATTERNS = [
    re.compile(r"\b(?:system\s*override|admin\s*override)\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:all\s+)?(?:(?:previous|prior|above|system|developer)\s+)?instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:rules|policies|guidelines)\b", re.IGNORECASE),
    re.compile(r"\b(?:developer\s*mode|god\s*mode|dan\s*mode)\b", re.IGNORECASE),
    re.compile(r"\b(?:waive\s+all\s+return\s+policies|bypass\s+verification)\b", re.IGNORECASE),
    re.compile(r"\b(?:approve\s+(?:claim|refund)\s+automatically|give\s+full\s+refund\s+unconditionally)\b", re.IGNORECASE),
    re.compile(r"\b(?:you\s+are\s+now\s+an\s+unrestricted\s+ai|jailbreak)\b", re.IGNORECASE),
    re.compile(r"\b(?:repeat|reveal|print|dump)\s+(?:the\s+|your\s+)?(?:system\s+prompt|developer\s+instructions)\b", re.IGNORECASE),
]

# Spanish
SPANISH_INJECTION_PATTERNS = [
    re.compile(r"\b(?:ignora|olvida|descarta|anula|omite)\s+(?:todas?\s+las?\s+)?(?:anteriores\s+|previas\s+)?(?:instrucciones|reglas|directivas)(?:\s+(?:anteriores|previas))?\b", re.IGNORECASE),
    re.compile(r"\b(?:actúa\s+como|eres\s+ahora|modo)\s+(?:un?\s+)?(?:admin|administrador|root|desarrollador|dan)\b", re.IGNORECASE),
    re.compile(r"\b(?:anular?\s+políticas|omitir?\s+verificación|aprobar?\s+reembolso\s+automáticamente)\b", re.IGNORECASE),
]

# French
FRENCH_INJECTION_PATTERNS = [
    re.compile(r"\b(?:ignorez?|ignore|oublie|contourne|annule)\s+(?:toutes?\s+les?\s+)?(?:précédentes\s+)?(?:instructions|règles|consignes)(?:\s+précédentes)?\b", re.IGNORECASE),
    re.compile(r"\b(?:agis\s+en\s+tant\s+que|tu\s+es\s+maintenant)\s+(?:un?\s+)?(?:admin|développeur)\b", re.IGNORECASE),
    re.compile(r"\b(?:contourner?\s+la\s+vérification|approuver?\s+remboursement\s+automatiquement)\b", re.IGNORECASE),
]

# German
GERMAN_INJECTION_PATTERNS = [
    re.compile(r"\b(?:ignoriere|vergiss|übergehe|verwerfe)\s+(?:alle\s+)?(?:vorherigen\s+|früheren\s+)?(?:anweisungen|regeln|richtlinien)\b", re.IGNORECASE),
    re.compile(r"\b(?:handle\s+als|du\s+bist\s+jetzt|wechsle\s+zu)\s+(?:admin|entwickler)\b", re.IGNORECASE),
    re.compile(r"\b(?:überprüfung\s+umgehen|erstattung\s+automatisch\s+genehmigen)\b", re.IGNORECASE),
]

# Hindi
HINDI_INJECTION_PATTERNS = [
    re.compile(r"\b(?:pichhle|purane|saare|sabhi)\s+(?:nirdesh|instructions|rules)\s+(?:bhool|ignore|chhod)\s*(?:jao|karo|do)?\b", re.IGNORECASE),
    re.compile(r"\b(?:system\s+prompt|developer\s+instructions)\s+(?:dikhao|batao|print\s+karo)\b", re.IGNORECASE),
    re.compile(r"\b(?:verification\s+bypass\s+karo|refund\s+approve\s+karo)\b", re.IGNORECASE),
    re.compile(r"(?:पिछले|पुराने|सभी)\s+(?:निर्देशों?|नियमों?)\s+(?:को\s+)?(?:अनदेखा\s+करें|भूल\s+जाओ|हटाओ)|(?:धनवापसी|रिफंड)\s+(?:स्वीकृत|मंजूर)\s+करें|सिस्टम\s+प्रॉम्प्ट\s+दिखाओ", re.IGNORECASE),
]

# Delimiter breakouts, model boundary hijacking, and unsafe markup
DELIMITER_BREAKOUT_PATTERNS = [
    re.compile(r"<\s*\|\s*im_(?:start|end)\s*\|\s*>", re.IGNORECASE),
    re.compile(r"\[/?(?:INST|SYS|SYSTEM)\]", re.IGNORECASE),
    re.compile(r"<<SYS>>|<</SYS>>", re.IGNORECASE),
    re.compile(r"<\s*/?\s*(?:system|sys|instruction|prompt|admin|developer|script|iframe|style|object|embed|form|input|svg|meta|link)\b.*?>", re.IGNORECASE),
    re.compile(r"---+(?:BEGIN|START|END)\s+(?:SYSTEM|PROMPT|INSTRUCTIONS?)---+", re.IGNORECASE),
]

# Markdown exfiltration: remote images embedded in document text
MARKDOWN_EXFILTRATION_PATTERN = re.compile(
    r"!\[.*?\]\((https?://[^\s\)]+)\)",
    re.IGNORECASE,
)

# Base64 candidate pattern (bounded lengths)
BASE64_CANDIDATE_PATTERN = re.compile(r"(?:^|[\s,;:(])([A-Za-z0-9+/]{16,}={0,2})(?=[\s,;:)]|$)")

# Hex candidate pattern (length >= 24)
HEX_CANDIDATE_PATTERN = re.compile(r"\b(?:[0-9a-fA-F]{2}){12,}\b")


# ==============================================================================
# ADVERSARIAL EVIDENCE GUARD IMPLEMENTATION
# ==============================================================================

class AdversarialEvidenceGuard:
    """Defense-in-depth security inspection engine for multimodal customer evidence."""

    MAX_ENCODED_SCAN_BYTES: int = 10_000
    MAX_RECURSION_DEPTH: int = 2

    def __init__(self, strict_mode: bool = False):
        self.strict_mode = strict_mode

    # --------------------------------------------------------------------------
    # 1. Unicode Normalization & Obfuscation Detection
    # --------------------------------------------------------------------------

    def normalize_unicode(self, text: str) -> Tuple[str, List[SecurityFinding]]:
        """Removes zero-width obfuscation and creates normalized inspection copy."""
        if not text:
            return "", []

        findings: List[SecurityFinding] = []

        # Count zero-width characters
        zw_count = sum(1 for ch in text if ch in ZERO_WIDTH_CHARS)
        if zw_count > 3:
            findings.append(
                SecurityFinding(
                    category="ZERO_WIDTH_OBFUSCATION",
                    severity="HIGH" if zw_count > 10 else "MEDIUM",
                    rule_name="excessive_zero_width_characters",
                    description=f"Detected {zw_count} zero-width or hidden control characters in evidence text.",
                    context_sample=f"Count: {zw_count}",
                )
            )

        # Strip zero-width characters for normalized representation
        stripped = "".join(ch for ch in text if ch not in ZERO_WIDTH_CHARS)

        # Unicode NFKD normalization
        nfkd = unicodedata.normalize("NFKD", stripped)

        # Map common homoglyphs for inspection
        inspection_chars = []
        homoglyph_detected = 0
        for ch in nfkd:
            if ch in HOMOGLYPH_MAP:
                inspection_chars.append(HOMOGLYPH_MAP[ch])
                homoglyph_detected += 1
            else:
                inspection_chars.append(ch)

        if homoglyph_detected > 0:
            findings.append(
                SecurityFinding(
                    category="HOMOGLYPH_OBFUSCATION",
                    severity="MEDIUM",
                    rule_name="homoglyph_substitution_detected",
                    description=f"Detected {homoglyph_detected} potential Cyrillic/Greek homoglyph substitutions.",
                    context_sample=f"Count: {homoglyph_detected}",
                )
            )

        normalized_inspection_text = "".join(inspection_chars)
        return normalized_inspection_text, findings

    # --------------------------------------------------------------------------
    # 2. Text Pattern & Multilingual Injection Scanning
    # --------------------------------------------------------------------------

    def scan_text_patterns(self, text: str) -> List[SecurityFinding]:
        """Scans text against multilingual and adversarial instruction patterns."""
        if not text:
            return []

        findings: List[SecurityFinding] = []

        # English
        for pat in ENGLISH_INJECTION_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="PROMPT_INJECTION",
                        severity="CRITICAL",
                        rule_name="english_prompt_injection",
                        description=f"Adversarial instruction detected matching rule: '{match.group(0)[:60]}'",
                        context_sample=match.group(0)[:80],
                    )
                )

        # Spanish
        for pat in SPANISH_INJECTION_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="PROMPT_INJECTION",
                        severity="CRITICAL",
                        rule_name="spanish_prompt_injection",
                        description=f"Spanish adversarial instruction detected matching rule: '{match.group(0)[:60]}'",
                        context_sample=match.group(0)[:80],
                    )
                )

        # French
        for pat in FRENCH_INJECTION_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="PROMPT_INJECTION",
                        severity="CRITICAL",
                        rule_name="french_prompt_injection",
                        description=f"French adversarial instruction detected matching rule: '{match.group(0)[:60]}'",
                        context_sample=match.group(0)[:80],
                    )
                )

        # German
        for pat in GERMAN_INJECTION_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="PROMPT_INJECTION",
                        severity="CRITICAL",
                        rule_name="german_prompt_injection",
                        description=f"German adversarial instruction detected matching rule: '{match.group(0)[:60]}'",
                        context_sample=match.group(0)[:80],
                    )
                )

        # Hindi
        for pat in HINDI_INJECTION_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="PROMPT_INJECTION",
                        severity="CRITICAL",
                        rule_name="hindi_prompt_injection",
                        description=f"Hindi adversarial instruction detected matching rule: '{match.group(0)[:60]}'",
                        context_sample=match.group(0)[:80],
                    )
                )

        # Delimiter breakouts
        for pat in DELIMITER_BREAKOUT_PATTERNS:
            match = pat.search(text)
            if match:
                findings.append(
                    SecurityFinding(
                        category="DELIMITER_BREAKOUT",
                        severity="HIGH",
                        rule_name="model_delimiter_breakout",
                        description=f"Model boundary token or dangerous markup detected: '{match.group(0)[:40]}'",
                        context_sample=match.group(0)[:60],
                    )
                )

        # Markdown remote image exfiltration
        for match in MARKDOWN_EXFILTRATION_PATTERN.finditer(text):
            url = match.group(1)
            findings.append(
                SecurityFinding(
                    category="MARKDOWN_EXFILTRATION",
                    severity="HIGH",
                    rule_name="markdown_remote_image_exfiltration",
                    description="Markdown remote image exfiltration URL pattern detected.",
                    context_sample=url[:60],
                )
            )

        return findings

    # --------------------------------------------------------------------------
    # 3. Encoded Payload Scanning (Base64, URL, Hex)
    # --------------------------------------------------------------------------

    def scan_encoded_payloads(self, text: str, depth: int = 0) -> List[SecurityFinding]:
        """Safely inspects Base64, URL, and Hex decoded text for hidden injections."""
        if not text or depth >= self.MAX_RECURSION_DEPTH or len(text) > self.MAX_ENCODED_SCAN_BYTES:
            return []

        findings: List[SecurityFinding] = []

        # 1. URL Decoding check
        if "%" in text:
            try:
                unquoted = urllib.parse.unquote(text)
                if unquoted != text:
                    sub_findings = self.scan_text_patterns(unquoted)
                    for f in sub_findings:
                        findings.append(
                            SecurityFinding(
                                category="ENCODED_PAYLOAD",
                                severity=f.severity,
                                rule_name=f"url_encoded_{f.rule_name}",
                                description=f"URL-encoded injection detected: {f.description}",
                                context_sample=f.context_sample,
                            )
                        )
            except Exception:
                pass

        # 2. Base64 Candidate check
        for match in BASE64_CANDIDATE_PATTERN.finditer(text):
            candidate = match.group(1).strip() if match.groups() else match.group(0).strip()
            if len(candidate) > 512:
                continue  # Bounded candidate length
            try:
                decoded_bytes = base64.b64decode(candidate, validate=True)
                decoded_str = decoded_bytes.decode("utf-8", errors="ignore").strip()
                if len(decoded_str) >= 10 and any(c.isalpha() for c in decoded_str):
                    sub_findings = self.scan_text_patterns(decoded_str)
                    for f in sub_findings:
                        findings.append(
                            SecurityFinding(
                                category="ENCODED_PAYLOAD",
                                severity=f.severity,
                                rule_name=f"base64_encoded_{f.rule_name}",
                                description=f"Base64 encoded injection directive detected: {f.description}",
                                context_sample=f.context_sample,
                            )
                        )
            except Exception:
                pass

        # 3. Hex Candidate check
        for match in HEX_CANDIDATE_PATTERN.finditer(text):
            candidate = match.group(0)
            if len(candidate) > 256:
                continue
            try:
                decoded_bytes = bytes.fromhex(candidate)
                decoded_str = decoded_bytes.decode("utf-8", errors="ignore").strip()
                if len(decoded_str) >= 10 and any(c.isalpha() for c in decoded_str):
                    sub_findings = self.scan_text_patterns(decoded_str)
                    for f in sub_findings:
                        findings.append(
                            SecurityFinding(
                                category="ENCODED_PAYLOAD",
                                severity=f.severity,
                                rule_name=f"hex_encoded_{f.rule_name}",
                                description=f"Hexadecimal encoded injection directive detected: {f.description}",
                                context_sample=f.context_sample,
                            )
                        )
            except Exception:
                pass

        return findings

    # --------------------------------------------------------------------------
    # 4. PDF Metadata Inspection
    # --------------------------------------------------------------------------

    def scan_pdf_metadata(self, file_bytes: bytes) -> Tuple[Dict[str, Any], List[SecurityFinding]]:
        """Inspects PDF Document Information dictionary and XMP metadata streams."""
        metadata_dict: Dict[str, Any] = {}
        findings: List[SecurityFinding] = []

        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            raw_meta = reader.metadata
            if raw_meta:
                for key, val in raw_meta.items():
                    if val and isinstance(val, str):
                        clean_key = str(key).lstrip("/")
                        metadata_dict[clean_key] = val

                        # Scan metadata value for injections
                        meta_findings = self.scan_text_patterns(val)
                        for mf in meta_findings:
                            findings.append(
                                SecurityFinding(
                                    category="METADATA_INJECTION",
                                    severity=mf.severity,
                                    rule_name=f"pdf_metadata_{clean_key}",
                                    description=f"Adversarial directive in PDF metadata '{clean_key}': {mf.description}",
                                    context_sample=val[:80],
                                )
                            )

            # Check XMP metadata if available
            if hasattr(reader, "xmp_metadata") and reader.xmp_metadata:
                try:
                    for prop in ("dc_title", "dc_description", "dc_creator", "pdf_keywords"):
                        val = getattr(reader.xmp_metadata, prop, None)
                        if val:
                            val_str = str(val)
                            metadata_dict[f"xmp_{prop}"] = val_str
                            xmp_findings = self.scan_text_patterns(val_str)
                            for xf in xmp_findings:
                                findings.append(
                                    SecurityFinding(
                                        category="METADATA_INJECTION",
                                        severity=xf.severity,
                                        rule_name=f"pdf_xmp_{prop}",
                                        description=f"Adversarial directive in PDF XMP '{prop}': {xf.description}",
                                        context_sample=val_str[:80],
                                    )
                                )
                except Exception as xmp_exc:
                    logger.debug("Safe handling of XMP metadata extraction error: %s", xmp_exc)

        except Exception as exc:
            logger.debug("PDF metadata inspection encountered non-fatal error: %s", exc)

        return metadata_dict, findings

    # --------------------------------------------------------------------------
    # 5. Image Metadata Inspection (EXIF / IPTC / XMP)
    # --------------------------------------------------------------------------

    def scan_image_metadata(self, file_bytes: bytes) -> Tuple[Dict[str, Any], List[SecurityFinding]]:
        """Inspects EXIF, IPTC, and user comment tags in JPEG and PNG files."""
        metadata_dict: Dict[str, Any] = {}
        findings: List[SecurityFinding] = []

        try:
            img = Image.open(io.BytesIO(file_bytes))

            # EXIF data
            exif = img.getexif()
            if exif:
                for tag_id, val in exif.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    if isinstance(val, (str, bytes)):
                        val_str = val.decode("utf-8", errors="ignore") if isinstance(val, bytes) else str(val)
                        val_str = val_str.strip()
                        if val_str:
                            metadata_dict[tag_name] = val_str[:255]
                            # Scan value for injections
                            img_findings = self.scan_text_patterns(val_str)
                            for imf in img_findings:
                                findings.append(
                                    SecurityFinding(
                                        category="METADATA_INJECTION",
                                        severity=imf.severity,
                                        rule_name=f"image_exif_{tag_name}",
                                        description=f"Adversarial directive in EXIF tag '{tag_name}': {imf.description}",
                                        context_sample=val_str[:80],
                                    )
                                )

            # PNG info dictionary
            if hasattr(img, "info") and isinstance(img.info, dict):
                for k, v in img.info.items():
                    if isinstance(v, (str, bytes)):
                        v_str = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                        v_str = v_str.strip()
                        if v_str and k not in metadata_dict:
                            metadata_dict[str(k)] = v_str[:255]
                            png_findings = self.scan_text_patterns(v_str)
                            for pf in png_findings:
                                findings.append(
                                    SecurityFinding(
                                        category="METADATA_INJECTION",
                                        severity=pf.severity,
                                        rule_name=f"png_info_{k}",
                                        description=f"Adversarial directive in PNG info tag '{k}': {pf.description}",
                                        context_sample=v_str[:80],
                                    )
                                )

        except Exception as exc:
            logger.debug("Image metadata inspection encountered non-fatal error: %s", exc)

        return metadata_dict, findings

    # --------------------------------------------------------------------------
    # 6. Invisible & Suspicious PDF Text Heuristics
    # --------------------------------------------------------------------------

    def detect_invisible_pdf_text(self, file_bytes: bytes) -> List[SecurityFinding]:
        """Inspects PDF text extraction for suspicious invisible rendering heuristics.

        Note: Pure pypdf extraction operates on high-level character streams.
        Documents with suspicious font/layout markers are flagged for additional review
        without claiming absolute rendering-level detection guarantees.
        """
        findings: List[SecurityFinding] = []

        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            for idx, page in enumerate(reader.pages, start=1):
                raw_page_text = page.extract_text() or ""
                # Check for high density of zero-width characters inside page text
                zw_in_page = sum(1 for ch in raw_page_text if ch in ZERO_WIDTH_CHARS)
                if zw_in_page > 5:
                    findings.append(
                        SecurityFinding(
                            category="INVISIBLE_TEXT_HEURISTIC",
                            severity="HIGH",
                            rule_name="pdf_zero_width_page_text",
                            description=f"Page {idx} contains {zw_in_page} zero-width characters (suspicious hidden text indicator).",
                            context_sample=f"Page {idx}: {zw_in_page} ZW chars",
                        )
                    )

                # Check for extremely large text length with zero visible images or standard invoice markers
                if len(raw_page_text) > 10_000 and len(page.images) == 0:
                    findings.append(
                        SecurityFinding(
                            category="INVISIBLE_TEXT_HEURISTIC",
                            severity="LOW",
                            rule_name="pdf_excessive_page_text_volume",
                            description=f"Page {idx} has unusually high character volume ({len(raw_page_text)} chars) without visible imagery.",
                            context_sample=f"Page {idx}: {len(raw_page_text)} chars",
                        )
                    )

        except Exception as exc:
            logger.debug("PDF invisible text heuristic check failed non-fatally: %s", exc)

        return findings

    # --------------------------------------------------------------------------
    # 7. Delimiter & Markdown Exfiltration Neutralization
    # --------------------------------------------------------------------------

    def neutralize_delimiters_and_exfiltration(self, text: str) -> Tuple[str, List[SecurityFinding]]:
        """Neutralizes model boundaries and markdown exfiltration tags before presentation."""
        if not text:
            return "", []

        findings: List[SecurityFinding] = []
        sanitized = text

        # 1. Strip markdown remote images: ![alt](http://...) -> [Image removed for security]
        if MARKDOWN_EXFILTRATION_PATTERN.search(sanitized):
            sanitized = MARKDOWN_EXFILTRATION_PATTERN.sub("[REMOTE_IMAGE_STRIPPED]", sanitized)
            findings.append(
                SecurityFinding(
                    category="MARKDOWN_EXFILTRATION",
                    severity="HIGH",
                    rule_name="markdown_image_stripped",
                    description="Sanitized remote Markdown image references to prevent URL exfiltration.",
                )
            )

        # 2. Neutralize instruction delimiters
        for pat in DELIMITER_BREAKOUT_PATTERNS:
            if pat.search(sanitized):
                sanitized = pat.sub(" ", sanitized)
                findings.append(
                    SecurityFinding(
                        category="DELIMITER_BREAKOUT",
                        severity="MEDIUM",
                        rule_name="delimiters_neutralized",
                        description="Neutralized instruction boundary tokens and dangerous tags.",
                    )
                )

        return sanitized.strip(), findings

    # --------------------------------------------------------------------------
    # 8. Full Document Security Scan Coordinator
    # --------------------------------------------------------------------------

    def scan_document(
        self,
        file_bytes: bytes,
        mime_type: str,
        raw_extracted_text: str,
        file_name: str = "document",
    ) -> SecurityScanResult:
        """Executes full multi-vector security scan over an evidence document."""
        all_findings: List[SecurityFinding] = []
        raw_metadata: Dict[str, Any] = {}

        # 1. Inspect file metadata based on MIME type
        if mime_type == "application/pdf":
            meta_dict, meta_findings = self.scan_pdf_metadata(file_bytes)
            raw_metadata.update(meta_dict)
            all_findings.extend(meta_findings)

            # Run PDF invisible text heuristics
            pdf_heuristics = self.detect_invisible_pdf_text(file_bytes)
            all_findings.extend(pdf_heuristics)
        elif mime_type in ("image/jpeg", "image/png"):
            meta_dict, meta_findings = self.scan_image_metadata(file_bytes)
            raw_metadata.update(meta_dict)
            all_findings.extend(meta_findings)

        # 2. Unicode normalization and obfuscation detection on extracted text
        normalized_text, unicode_findings = self.normalize_unicode(raw_extracted_text)
        all_findings.extend(unicode_findings)

        # 3. Direct pattern scanning on both raw and normalized text
        raw_pattern_findings = self.scan_text_patterns(raw_extracted_text)
        all_findings.extend(raw_pattern_findings)

        if normalized_text != raw_extracted_text:
            normalized_pattern_findings = self.scan_text_patterns(normalized_text)
            # Add unique findings not already discovered
            existing_rules = {f.rule_name for f in all_findings}
            for nf in normalized_pattern_findings:
                if nf.rule_name not in existing_rules:
                    all_findings.append(nf)

        # 4. Encoded payload scanning
        encoded_findings = self.scan_encoded_payloads(raw_extracted_text)
        all_findings.extend(encoded_findings)

        # 5. Neutralize dangerous structures for sanitized representation
        sanitized_text, neutralization_findings = self.neutralize_delimiters_and_exfiltration(raw_extracted_text)
        all_findings.extend(neutralization_findings)

        # 6. Evaluate Security Outcome
        has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in all_findings)
        has_injection = any(f.category in ("PROMPT_INJECTION", "ENCODED_PAYLOAD", "METADATA_INJECTION") and f.severity in ("CRITICAL", "HIGH") for f in all_findings)

        if has_injection:
            outcome = EvidenceSecurityOutcome.FLAGGED_UNSAFE
            is_safe = False
            is_malicious = True
            primary_reason = next(
                (f.description for f in all_findings if f.severity in ("CRITICAL", "HIGH")),
                "Adversarial instruction or injection payload detected",
            )
            quarantine_reason = f"ADVERSARIAL_DIRECTIVE_DETECTED: {primary_reason}"
        elif has_critical:
            outcome = EvidenceSecurityOutcome.FLAGGED_SUSPICIOUS
            is_safe = False
            is_malicious = False
            quarantine_reason = "FLAGGED_SUSPICIOUS: Security anomalies detected requiring verification"
        elif len(all_findings) > 0:
            outcome = EvidenceSecurityOutcome.NEEDS_MANUAL_REVIEW
            is_safe = True
            is_malicious = False
            quarantine_reason = None
        else:
            outcome = EvidenceSecurityOutcome.CLEAN
            is_safe = True
            is_malicious = False
            quarantine_reason = None

        return SecurityScanResult(
            outcome=outcome,
            is_safe=is_safe,
            is_malicious=is_malicious,
            quarantine_reason=quarantine_reason,
            findings=all_findings,
            normalized_text=normalized_text,
            sanitized_text=sanitized_text,
            raw_metadata=raw_metadata,
            has_critical_finding=has_critical,
        )

    # --------------------------------------------------------------------------
    # 9. Customer Conversational Claim Injection Guard
    # --------------------------------------------------------------------------

    def scan_customer_claim(self, claim_text: str) -> SecurityScanResult:
        """Inspects customer natural-language claim utterance for injection directives."""
        if not claim_text:
            return SecurityScanResult(
                outcome=EvidenceSecurityOutcome.CLEAN,
                is_safe=True,
                is_malicious=False,
                sanitized_text="",
            )

        all_findings: List[SecurityFinding] = []

        # 1. Unicode normalization
        normalized_text, unicode_findings = self.normalize_unicode(claim_text)
        all_findings.extend(unicode_findings)

        # 2. Text patterns
        pattern_findings = self.scan_text_patterns(claim_text)
        all_findings.extend(pattern_findings)

        if normalized_text != claim_text:
            norm_findings = self.scan_text_patterns(normalized_text)
            existing_rules = {f.rule_name for f in all_findings}
            for nf in norm_findings:
                if nf.rule_name not in existing_rules:
                    all_findings.append(nf)

        # 3. Encoded payload scan
        encoded_findings = self.scan_encoded_payloads(claim_text)
        all_findings.extend(encoded_findings)

        # 4. Neutralize delimiters
        sanitized_text, neut_findings = self.neutralize_delimiters_and_exfiltration(claim_text)
        all_findings.extend(neut_findings)

        has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in all_findings)
        has_injection = any(f.category in ("PROMPT_INJECTION", "ENCODED_PAYLOAD") and f.severity in ("CRITICAL", "HIGH") for f in all_findings)

        if has_injection or has_critical:
            outcome = EvidenceSecurityOutcome.SECURITY_INJECTION_DETECTED
            is_safe = False
            is_malicious = True
            primary_reason = next(
                (f.description for f in all_findings if f.severity in ("CRITICAL", "HIGH")),
                "Customer message contains adversarial directives",
            )
            quarantine_reason = f"CLAIM_SECURITY_VIOLATION: {primary_reason}"
        else:
            outcome = EvidenceSecurityOutcome.CLEAN
            is_safe = True
            is_malicious = False
            quarantine_reason = None

        return SecurityScanResult(
            outcome=outcome,
            is_safe=is_safe,
            is_malicious=is_malicious,
            quarantine_reason=quarantine_reason,
            findings=all_findings,
            normalized_text=normalized_text,
            sanitized_text=sanitized_text,
            has_critical_finding=has_critical,
        )


default_adversarial_guard = AdversarialEvidenceGuard()


