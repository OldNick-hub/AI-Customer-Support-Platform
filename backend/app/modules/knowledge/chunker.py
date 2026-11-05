"""Knowledge Base Structure-Aware Document Chunker.

Splits sanitized Markdown, TXT, and PDF documents into semantically coherent,
sentence-boundary-aligned chunks while preserving heading hierarchies, page numbers,
and stable access-control metadata.
"""

from dataclasses import dataclass, field
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import DocumentCorruptError
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


@dataclass
class ExtractedChunk:
    """Represents a single tokenized/sentence-split document chunk."""

    chunk_index: int
    chunk_text: str
    token_count: int
    heading_path: List[str]
    section_hierarchy: str
    page_number: Optional[int]
    metadata_: Dict[str, Any] = field(default_factory=dict)


class StructureAwareChunker:
    """Splits structured text into semantic chunks respecting headings, sentences, and page numbers."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        chunk_size_chars: int = 600,
        chunk_overlap_chars: int = 100,
    ) -> None:
        self.settings = settings or default_settings
        self.chunk_size_chars = chunk_size_chars
        self.chunk_overlap_chars = chunk_overlap_chars

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        """Estimates token count conservatively (average ~4 chars per token or whitespace split)."""
        words = text.split()
        return max(1, len(words))

    @staticmethod
    def _split_into_sentences(text: str) -> List[str]:
        """Splits text into complete sentences, respecting abbreviations, decimals, and titles."""
        if not text or not text.strip():
            return []

        # Common abbreviations, titles, months, and Latin terms that do not end a sentence
        abbr_pattern = re.compile(
            r'\b(?:'
            r'Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Inc|Ltd|Corp|Co|vs|No|'
            r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|'
            r'e\.g|i\.e|etc|al|approx|est|dept|div|fig|sec|v|vol'
            r')\.',
            re.IGNORECASE,
        )

        t = text
        placeholder_map: Dict[str, str] = {}

        # 1. Protect abbreviations (e.g. Dr., e.g., Jan.)
        def repl_abbr(match: re.Match) -> str:
            key = f"__ABBR_{len(placeholder_map)}__"
            placeholder_map[key] = match.group(0)
            return key
        t = abbr_pattern.sub(repl_abbr, t)

        # 2. Protect single initials and acronyms (e.g. "U.S.", "A.", "B.")
        def repl_init(match: re.Match) -> str:
            key = f"__INIT_{len(placeholder_map)}__"
            placeholder_map[key] = match.group(0)
            return key
        t = re.sub(r'\b[A-Za-z]\.', repl_init, t)

        # 3. Protect decimals, currency, and version numbers (e.g. "2.0", "$19.99", "1.2.3")
        def repl_dec(match: re.Match) -> str:
            key = f"__DEC_{len(placeholder_map)}__"
            placeholder_map[key] = match.group(0)
            return key
        t = re.sub(r'\b\d+(?:\.\d+)+\b', repl_dec, t)

        # 4. Protect email / domain extensions (e.g. "support@example.com")
        def repl_domain(match: re.Match) -> str:
            key = f"__DOM_{len(placeholder_map)}__"
            placeholder_map[key] = match.group(0)
            return key
        t = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', repl_domain, t)

        # 5. Split on sentence boundaries: punctuation (. ! ?) followed by whitespace
        raw_parts = re.split(r'(?<=[.!?])\s+', t)

        # 6. Restore placeholders and clean up
        sentences: List[str] = []
        for part in raw_parts:
            restored = part
            for key, val in placeholder_map.items():
                restored = restored.replace(key, val)
            restored = restored.strip()
            if restored:
                sentences.append(restored)

        return sentences

    def chunk_document(
        self,
        text: str,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        document_code: str,
        title: Optional[str] = None,
        category: str = "POLICY",
        product_line: str = "GLOBAL",
        region: str = "GLOBAL",
        access_level: str = "PUBLIC",
    ) -> List[ExtractedChunk]:
        """Chunks a document into sentence-aligned pieces preserving hierarchical structure."""
        if text is None or not isinstance(text, str) or not text.strip():
            raise DocumentCorruptError(
                message=f"Cannot chunk document '{document_code}': content is empty or malformed.",
                detail={"error_code": "EMPTY_DOCUMENT_TEXT", "document_code": document_code},
                error_code="EMPTY_DOCUMENT_TEXT",
            )

        lines = text.splitlines()
        chunks: List[ExtractedChunk] = []
        heading_stack: List[Tuple[str, int]] = []  # List of (heading_text, heading_level)
        current_page: Optional[int] = None
        current_sentences: List[str] = []
        current_length: int = 0
        chunk_counter = 0
        seen_chunk_keys: Set[str] = set()

        def get_hierarchy_path() -> Tuple[List[str], str]:
            path = [h[0] for h in heading_stack]
            hierarchy = " > ".join(path) if path else (title or document_code)
            return path, hierarchy

        def flush_chunk(reset_overlap: bool = False):
            nonlocal chunk_counter, current_sentences, current_length
            if not current_sentences:
                return

            chunk_body = " ".join(current_sentences).strip()
            h_path, h_str = get_hierarchy_path()
            dedup_key = f"{h_str}::{chunk_body}"

            if not chunk_body or dedup_key in seen_chunk_keys:
                if reset_overlap:
                    current_sentences = []
                    current_length = 0
                return

            # Prefix heading hierarchy context if chunk does not already begin with it
            if h_str and not chunk_body.startswith(h_str):
                context_prefix = f"[{h_str}]\n"
                full_chunk_text = context_prefix + chunk_body
            else:
                full_chunk_text = chunk_body

            seen_chunk_keys.add(dedup_key)
            token_count = self._estimate_token_count(full_chunk_text)

            meta = {
                "document_id": str(document_id),
                "version_id": str(version_id),
                "document_code": document_code,
                "title": title or document_code,
                "category": category,
                "product_line": product_line,
                "region": region,
                "access_level": access_level,
                "section_hierarchy": h_str,
                "heading_path": h_path,
                "chunk_index": chunk_counter,
                "page_number": current_page,
            }

            chunks.append(
                ExtractedChunk(
                    chunk_index=chunk_counter,
                    chunk_text=full_chunk_text,
                    token_count=token_count,
                    heading_path=h_path,
                    section_hierarchy=h_str,
                    page_number=current_page,
                    metadata_=meta,
                )
            )
            chunk_counter += 1

            if reset_overlap:
                current_sentences = []
                current_length = 0
            else:
                # Prepare overlap for next chunk within the same section and page
                overlap_sentences: List[str] = []
                overlap_len = 0
                for s in reversed(current_sentences):
                    if overlap_len + len(s) + 1 <= self.chunk_overlap_chars:
                        overlap_sentences.insert(0, s)
                        overlap_len += len(s) + 1
                    else:
                        break

                current_sentences = overlap_sentences
                current_length = overlap_len

        # Process line by line
        for line in lines:
            trimmed = line.strip()
            if not trimmed:
                continue

            # Check for PDF page markers (e.g., "--- Page 2 ---" or "[Page 2]" or "\f")
            page_match = re.match(r"^(?:---\s*Page\s*(\d+)\s*---|\[Page\s*(\d+)\])$", trimmed, re.IGNORECASE)
            if page_match:
                flush_chunk(reset_overlap=True)
                current_page = int(page_match.group(1) or page_match.group(2))
                continue

            # Check for Markdown heading (# Heading)
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", trimmed)
            if heading_match:
                # Flush existing chunk before changing section context (strictly reset overlap across headings)
                flush_chunk(reset_overlap=True)
                hashes, heading_text = heading_match.groups()
                level = len(hashes)
                clean_heading = heading_text.strip()

                # Pop higher or equal level headings from stack
                while heading_stack and heading_stack[-1][1] >= level:
                    heading_stack.pop()
                heading_stack.append((clean_heading, level))
                continue

            # Regular text line: split into sentences
            line_sentences = self._split_into_sentences(trimmed)
            for sentence in line_sentences:
                s_len = len(sentence)
                if current_length + s_len + 1 > self.chunk_size_chars and current_sentences:
                    flush_chunk(reset_overlap=False)

                current_sentences.append(sentence)
                current_length += s_len + 1

        # Flush final remaining sentences
        flush_chunk(reset_overlap=True)

        if not chunks:
            raise DocumentCorruptError(
                message=f"Document '{document_code}' produced 0 valid chunks after parsing.",
                detail={"error_code": "NO_VALID_CHUNKS", "document_code": document_code},
            )

        logger.info(
            "Document '%s' successfully split into %d structure-aware chunks.",
            document_code,
            len(chunks),
        )
        return chunks
