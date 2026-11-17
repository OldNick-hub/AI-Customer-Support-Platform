"""Citation Management and Grounded Claim Verification Engine.

Enforces zero-trust citation provenance, claim-to-passage alignment, numeric/date
consistency, and detection of fabricated or unauthorized source references.
"""

from datetime import date
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from app.modules.rag.retriever import RetrievedChunk

logger = logging.getLogger(__name__)

STOPWORDS: Set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but",
    "by", "can", "did", "do", "does", "doing", "don", "down", "during", "each", "few", "for",
    "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just",
    "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "s", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "you", "your", "yours", "yourself", "yourselves", "please", "according",
}

NON_FACTUAL_PHRASES: Set[str] = {
    "let me know if you need any further assistance",
    "let me know if you have any questions",
    "feel free to ask if you have more questions",
    "i hope this helps",
    "here is the information you requested",
    "here is the policy information",
    "based on the documentation",
    "please reach out if you have further inquiries",
    "have a great day",
    "i understand your frustration",
    "we understand your frustration",
    "i apologize for the inconvenience",
    "we apologize for the inconvenience",
    "i am sorry for your trouble",
    "i am sorry for the inconvenience",
    "thank you for your patience",
    "thank you for reaching out",
    "thank you for your inquiry",
    "immediate next step",
    "next steps",
}


class CitationMetadata(BaseModel):
    """Cryptographic and provenance metadata for an inline source citation."""

    citation_number: int
    document_code: str
    document_title: str
    version_number: int
    section_hierarchy: Optional[str] = None
    page_number: Optional[int] = None
    effective_from: date
    effective_until: Optional[date] = None
    similarity_score: float
    excerpt: str
    sha256_hash: str
    chunk_id: Optional[str] = None
    vector_id: Optional[int] = None


class ClaimVerificationResult(BaseModel):
    """Outcome of claim-to-passage alignment verification."""

    total_claims: int = 0
    verified_claims: int = 0
    unsupported_claims: List[str] = Field(default_factory=list)
    numeric_mismatches: List[str] = Field(default_factory=list)
    date_mismatches: List[str] = Field(default_factory=list)
    fabricated_citations: List[str] = Field(default_factory=list)
    unauthorized_citations: List[str] = Field(default_factory=list)
    missing_citations: List[str] = Field(default_factory=list)
    contradictions: List[str] = Field(default_factory=list)
    all_claims_verified: bool = True
    remediation_attempted: bool = False
    errors: List[str] = Field(default_factory=list)


class CitationManager:
    """Manages citation extraction, serialization, and provenance formatting."""

    @staticmethod
    def format_evidence_context(chunks: List[RetrievedChunk]) -> str:
        """Serializes authorized chunks into indexed, delimited context blocks for LLM inference."""
        if not chunks:
            return ""

        formatted_blocks = []
        for idx, chunk in enumerate(chunks, start=1):
            eff_until = chunk.effective_until.isoformat() if chunk.effective_until else "Indefinite"
            sec = chunk.metadata.get("section_hierarchy") or chunk.metadata.get("section")
            sec_str = f"\nSection: {sec}" if sec else ""
            pg = chunk.metadata.get("page_number")
            pg_str = f"\nPage: {pg}" if pg is not None else ""

            # Neutralize XML closing tags and delimiters in untrusted chunk text
            sanitized_text = re.sub(
                r"<\s*/?\s*(?:authoritative_data|context|system)\s*>",
                "[ESCAPED_TAG]",
                chunk.chunk_text,
                flags=re.IGNORECASE,
            )
            sanitized_text = re.sub(
                r"\[/?(?:INST|SYS|SYSTEM)\]",
                "[ESCAPED_TAG]",
                sanitized_text,
                flags=re.IGNORECASE,
            )
            sanitized_text = re.sub(
                r"<\|im_(?:start|end)\|>",
                "[ESCAPED_TAG]",
                sanitized_text,
                flags=re.IGNORECASE,
            )

            block = (
                f"[{idx}] Document: {chunk.document_code} (Title: \"{chunk.document_title}\", Version: {chunk.version_number})"
                f"\nEffective: {chunk.effective_from.isoformat()} to {eff_until}"
                f"{sec_str}{pg_str}"
                f"\nContent: {sanitized_text}"
            )
            formatted_blocks.append(block)

        return "\n\n".join(formatted_blocks)

    @staticmethod
    def extract_citation_indices(text: str) -> List[int]:
        """Extracts all integer citation references like [1], [2], [1, 2] from generated text."""
        # Match brackets containing digits separated by commas or spaces
        matches = re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", text)
        indices: Set[int] = set()
        for m in matches:
            parts = m.split(",")
            for p in parts:
                p_clean = p.strip()
                if p_clean.isdigit():
                    indices.add(int(p_clean))
        return sorted(list(indices))

    @staticmethod
    def extract_document_code_references(text: str) -> Set[str]:
        """Extracts document code patterns such as POL-RET-US-2026, SOP-AGENT-01, DOC-AUTH-01."""
        pattern = r"\b(?:POL|SOP|RUNBOOK|GUIDE|MAN|DOC|FAQ|REF|WARR)-[A-Z0-9_-]+\b"
        matches = re.findall(pattern, text)
        return {m.strip() for m in matches}

    @staticmethod
    def build_citation_metadata(chunk: RetrievedChunk, citation_number: int) -> CitationMetadata:
        """Constructs honest CitationMetadata without fabricating missing page or section fields."""
        sec = chunk.metadata.get("section_hierarchy") or chunk.metadata.get("section")
        pg = chunk.metadata.get("page_number")
        if pg is not None and not isinstance(pg, int):
            try:
                pg = int(pg)
            except (ValueError, TypeError):
                pg = None

        return CitationMetadata(
            citation_number=citation_number,
            document_code=chunk.document_code,
            document_title=chunk.document_title,
            version_number=chunk.version_number,
            section_hierarchy=sec if sec else None,
            page_number=pg,
            effective_from=chunk.effective_from,
            effective_until=chunk.effective_until,
            similarity_score=chunk.similarity_score,
            excerpt=chunk.chunk_text[:250].strip(),
            sha256_hash=chunk.sha256_hash,
            chunk_id=chunk.chunk_id,
            vector_id=chunk.vector_id,
        )


class ClaimCitationVerifier:
    """Verifies that generated answers are grounded in retrieved evidence with correct citations."""

    @staticmethod
    def split_sentences_and_citations(text: str) -> List[Tuple[str, List[int]]]:
        """Splits generated answer into proposition sentences and extracts their associated citations."""
        # Split text by sentence terminators (. ! ? or newline)
        raw_sentences = re.split(r"(?<=[.!?\n])\s+", text)
        results: List[Tuple[str, List[int]]] = []

        for s in raw_sentences:
            s_clean = s.strip()
            if not s_clean:
                continue

            # Extract citations in this sentence
            citations = CitationManager.extract_citation_indices(s_clean)

            # Strip citation tags to inspect proposition text
            text_without_cites = re.sub(r"\[\d+(?:\s*,\s*\d+)*\]", "", s_clean).strip()
            if not text_without_cites:
                continue

            # Check if sentence is purely non-factual greeting/closing
            lower_clean = text_without_cites.lower().strip(".,!?")
            if any(phrase in lower_clean for phrase in NON_FACTUAL_PHRASES):
                continue

            results.append((text_without_cites, citations))

        return results

    @staticmethod
    def extract_numbers_and_quantities(text: str) -> Set[float]:
        """Extracts numerical digits, amounts, currencies, and percentages from text as floats.

        Eliminates substring vulnerabilities by converting values to numeric floats.
        """
        # Strip masked PII patterns (e.g. ****-****-****-5678 or [REDACTED])
        clean = re.sub(r"\*{2,}[0-9\-*]*", "", text)
        clean = re.sub(r"\[REDACTED\]", "", clean, flags=re.IGNORECASE)
        # Strip citation brackets like [1], [2], [1, 2]
        clean = re.sub(r"\[\d+(?:\s*,\s*\d+)*\]", "", clean)
        # Strip ISO and natural dates and standalone years so date numbers are handled by date validation
        clean = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", clean)
        clean = re.sub(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}\b",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        clean = re.sub(r"\b(?:19|20)\d{2}\b", "", clean)

        # Match numbers: e.g. $50, 15%, 30 days, 25.5
        tokens = re.findall(r"\b\d+(?:,\d{3})*(?:\.\d+)?\b", clean)
        values: Set[float] = set()
        for t in tokens:
            t_clean = t.replace(",", "")
            try:
                values.add(float(t_clean))
            except ValueError:
                pass
        return values

    @staticmethod
    def parse_date(date_str: str) -> Optional[Tuple[int, int, int]]:
        """Parses an ISO date or natural language date into (YYYY, MM, DD)."""
        s = date_str.strip().lower()
        m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if m_iso:
            return (int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))

        months = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        m_nat = re.match(r"^([a-z]+)\s+(\d{1,2}),?\s+(\d{4})$", s)
        if m_nat and m_nat.group(1) in months:
            return (int(m_nat.group(3)), months[m_nat.group(1)], int(m_nat.group(2)))
        return None

    @staticmethod
    def extract_dates(text: str) -> Set[str]:
        """Extracts date strings such as 2026-01-01, January 1, 2026, Jan 2026."""
        # ISO format: YYYY-MM-DD
        iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
        # Natural dates: January 1, 2026 or Jan 1, 2026
        nat_dates = re.findall(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}\b",
            text,
            re.IGNORECASE,
        )
        # Years: 2024, 2025, 2026
        years = re.findall(r"\b(?:19|20)\d{2}\b", text)

        found = set(iso_dates) | {d.lower() for d in nat_dates}
        # Only add standalone years if no longer date matches
        if not iso_dates and not nat_dates:
            found.update(years)
        return found

    @classmethod
    def verify(
        cls,
        generated_text: str,
        authorized_chunks: List[RetrievedChunk],
        remediation_attempted: bool = False,
    ) -> ClaimVerificationResult:
        """Performs rigorous multi-check claim verification on generated text."""
        errors: List[str] = []
        unsupported_claims: List[str] = []
        numeric_mismatches: List[str] = []
        date_mismatches: List[str] = []
        fabricated_cites: List[str] = []
        unauthorized_cites: List[str] = []
        missing_cites: List[str] = []
        contradictions: List[str] = []

        if not authorized_chunks:
            # If no authorized evidence provided, any factual claim fails verification
            claims = cls.split_sentences_and_citations(generated_text)
            for claim_text, _ in claims:
                err = f"UNSUPPORTED_CLAIM: No authorized evidence available to support '{claim_text}'"
                errors.append(err)
                unsupported_claims.append(claim_text)
            return ClaimVerificationResult(
                total_claims=len(claims),
                verified_claims=0,
                unsupported_claims=unsupported_claims,
                all_claims_verified=False,
                remediation_attempted=remediation_attempted,
                errors=errors,
            )

        # 1. Check for unauthorized document code references
        authorized_doc_codes = {c.document_code.upper() for c in authorized_chunks}
        text_doc_codes = CitationManager.extract_document_code_references(generated_text)
        for code in text_doc_codes:
            if code.upper() not in authorized_doc_codes:
                err = f"UNAUTHORIZED_CITATION: Referenced document code '{code}' is not among authorized evidence."
                errors.append(err)
                unauthorized_cites.append(code)

        # 2. Extract all citation markers and check bounds
        all_cited_indices = CitationManager.extract_citation_indices(generated_text)
        max_valid_index = len(authorized_chunks)
        for c_idx in all_cited_indices:
            if c_idx < 1 or c_idx > max_valid_index:
                err = f"FABRICATED_CITATION: Citation [{c_idx}] does not exist in authorized evidence (valid: 1..{max_valid_index})."
                errors.append(err)
                fabricated_cites.append(f"[{c_idx}]")

        # 3. Sentence-level claim verification
        claims_with_cites = cls.split_sentences_and_citations(generated_text)
        total_claims = len(claims_with_cites)
        verified_claims_count = 0

        for claim_text, cites in claims_with_cites:
            claim_failed = False

            # Check 3a: Every substantive proposition requires a citation
            if not cites:
                err = f"MISSING_CITATION: Factual claim '{claim_text}' lacks a source citation [X]."
                errors.append(err)
                missing_cites.append(claim_text)
                claim_failed = True

            # If citations exist, verify alignment against cited chunks
            valid_cites = [c for c in cites if 1 <= c <= max_valid_index]
            if valid_cites:
                cited_chunks = [authorized_chunks[c - 1] for c in valid_cites]
                combined_chunk_text = " ".join([c.chunk_text for c in cited_chunks])

                # Check 3b: Numerical consistency (exact float equality)
                claim_nums = cls.extract_numbers_and_quantities(claim_text)
                chunk_nums = cls.extract_numbers_and_quantities(combined_chunk_text)

                for num_val in claim_nums:
                    if num_val not in chunk_nums:
                        err = (
                            f"NUMERIC_MISMATCH: Claim asserted quantity '{num_val}' not found in cited evidence "
                            f"{[c for c in valid_cites]}."
                        )
                        errors.append(err)
                        numeric_mismatches.append(f"{claim_text} (number: {num_val})")
                        claim_failed = True

                # Check 3c: Date consistency (normalized date tuple comparison)
                claim_dates = cls.extract_dates(claim_text)
                chunk_dates = cls.extract_dates(combined_chunk_text)
                parsed_chunk_dates = {
                    cls.parse_date(cd) for cd in chunk_dates if cls.parse_date(cd) is not None
                }

                for d in claim_dates:
                    parsed_claim_d = cls.parse_date(d)
                    date_matched = False
                    if parsed_claim_d and parsed_claim_d in parsed_chunk_dates:
                        date_matched = True
                    elif d.lower() in [cd.lower() for cd in chunk_dates] or any(d in c.chunk_text for c in cited_chunks):
                        date_matched = True

                    if not date_matched:
                        err = f"DATE_MISMATCH: Claim asserted date '{d}' not found in cited evidence."
                        errors.append(err)
                        date_mismatches.append(f"{claim_text} (date: {d})")
                        claim_failed = True

                # Check 3d: Substantive token overlap (Semantic Grounding)
                claim_words = {
                    w.lower()
                    for w in re.findall(r"\b[a-zA-Z]{3,}\b", claim_text)
                    if w.lower() not in STOPWORDS
                }
                chunk_words = {
                    w.lower()
                    for w in re.findall(r"\b[a-zA-Z]{3,}\b", combined_chunk_text)
                    if w.lower() not in STOPWORDS
                }

                if claim_words:
                    overlap = claim_words.intersection(chunk_words)
                    overlap_ratio = len(overlap) / len(claim_words)
                    # Require at least 25% non-stopword overlap with cited chunk
                    if overlap_ratio < 0.25:
                        err = (
                            f"UNSUPPORTED_CLAIM: Claim '{claim_text}' has insufficient overlap ({overlap_ratio:.2f}) "
                            f"with cited passage."
                        )
                        errors.append(err)
                        unsupported_claims.append(claim_text)
                        claim_failed = True

                # Check 3e: Contradiction and Negation Inversion Detection
                claim_lower = claim_text.lower()
                chunk_lower = combined_chunk_text.lower()

                # Inversion 1: Free vs Fee
                if "free return" in claim_lower and any(w in chunk_lower for w in ["fee", "deducted", "restocking", "charge"]):
                    err = "CONTRADICTION: Claim asserts free return but evidence mentions return fees or deductions."
                    errors.append(err)
                    contradictions.append(claim_text)
                    claim_failed = True

                # Inversion 2: Non-refundable / No refunds vs Permitted refunds
                elif any(w in chunk_lower for w in ["non-refundable", "no refunds", "all sales are final", "not eligible for return", "final sale"]) and any(w in claim_lower for w in ["refund is available", "full refund", "refund eligible", "refunds are permitted", "eligible for return", "customers can return"]):
                    err = "CONTRADICTION: Evidence states items are non-refundable, but claim asserted refund eligibility."
                    errors.append(err)
                    contradictions.append(claim_text)
                    claim_failed = True

                # Inversion 3: Unopened vs Opened
                elif ("unopened" in chunk_lower or "original condition" in chunk_lower) and any(w in claim_lower for w in ["opened items can be returned", "opened merchandise is accepted", "opened products are accepted"]):
                    err = "CONTRADICTION: Evidence requires unopened condition, but claim asserts opened items are accepted."
                    errors.append(err)
                    contradictions.append(claim_text)
                    claim_failed = True

                # Inversion 4: Exclusions (e.g. software, gift cards)
                elif any(w in chunk_lower for w in ["excluding software", "software is excluded", "software is non-refundable", "gift cards are non-refundable"]) and any(w in claim_lower for w in ["software can be returned", "software is eligible", "gift cards can be returned"]):
                    err = "CONTRADICTION: Evidence explicitly excludes item category, but claim asserts return eligibility."
                    errors.append(err)
                    contradictions.append(claim_text)
                    claim_failed = True

            if not claim_failed and cites:
                verified_claims_count += 1

        all_verified = (
            len(errors) == 0
            and len(unsupported_claims) == 0
            and len(numeric_mismatches) == 0
            and len(date_mismatches) == 0
            and len(fabricated_cites) == 0
            and len(unauthorized_cites) == 0
            and len(missing_cites) == 0
            and len(contradictions) == 0
        )

        return ClaimVerificationResult(
            total_claims=total_claims,
            verified_claims=verified_claims_count,
            unsupported_claims=unsupported_claims,
            numeric_mismatches=numeric_mismatches,
            date_mismatches=date_mismatches,
            fabricated_citations=fabricated_cites,
            unauthorized_citations=unauthorized_cites,
            missing_citations=missing_cites,
            contradictions=contradictions,
            all_claims_verified=all_verified,
            remediation_attempted=remediation_attempted,
            errors=errors,
        )
