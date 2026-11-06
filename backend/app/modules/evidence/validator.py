"""File Signature, MIME-Type, and Path Traversal Validation Engine.

Strictly enforces:
1. True file signature (magic bytes) validation — never trusts client-provided headers or extensions.
2. File-size bounding (rejects 0-byte empty files and files exceeding 15MB).
3. MIME type consistency (rejects MIME/signature discrepancies).
4. Path traversal protection (strips directory separators, null bytes, and traversal tokens).
5. PDF page count enforcement (limits multi-page PDFs to maximum 10 pages).
"""

from dataclasses import dataclass
import hashlib
import io
import os
import re
from typing import List, Optional, Tuple
import uuid

import pypdf

from app.core.config import settings


# Authoritative Magic Byte Signatures
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PDF_SIGNATURE = b"%PDF-"


class EvidenceValidationError(Exception):
    """Base exception for evidence validation failures."""

    def __init__(self, message: str, error_code: str = "VALIDATION_ERROR", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code


class EmptyFileError(EvidenceValidationError):
    def __init__(self, message: str = "Uploaded file is empty (0 bytes)."):
        super().__init__(message, error_code="EMPTY_FILE", status_code=400)


class OversizedFileError(EvidenceValidationError):
    def __init__(self, message: str = "Uploaded file exceeds maximum allowed size (15 MB)."):
        super().__init__(message, error_code="FILE_OVERSIZED", status_code=413)


class UnsupportedMediaTypeError(EvidenceValidationError):
    def __init__(self, message: str = "Unsupported file format. Only JPEG, PNG, and PDF files are permitted."):
        super().__init__(message, error_code="UNSUPPORTED_MEDIA_TYPE", status_code=415)


class SignatureMismatchError(EvidenceValidationError):
    def __init__(self, message: str = "Client MIME type does not match actual detected file signature."):
        super().__init__(message, error_code="MIME_SIGNATURE_MISMATCH", status_code=400)


class CorruptedFileError(EvidenceValidationError):
    def __init__(self, message: str = "File payload is corrupted or malformed."):
        super().__init__(message, error_code="CORRUPTED_FILE", status_code=400)


class PageCountExceededError(EvidenceValidationError):
    def __init__(self, message: str = "PDF document exceeds maximum allowed page count (10 pages)."):
        super().__init__(message, error_code="PAGE_COUNT_EXCEEDED", status_code=400)


class PathTraversalAttemptError(EvidenceValidationError):
    def __init__(self, message: str = "Illegal file path or path traversal characters detected."):
        super().__init__(message, error_code="PATH_TRAVERSAL_DETECTED", status_code=400)


@dataclass(frozen=True)
class ValidatedFileMetadata:
    """Immutable container of verified file properties."""

    file_bytes: bytes
    detected_mime_type: str
    canonical_extension: str
    sha256_hash: str
    file_size_bytes: int
    sanitized_client_name: str
    internal_storage_filename: str
    page_count: int
    is_pdf: bool
    is_image: bool


class FileValidator:
    """Validates uploaded evidence payloads against security and structure policies."""

    def __init__(
        self,
        max_size_bytes: Optional[int] = None,
        max_pdf_pages: Optional[int] = None,
    ):
        self.max_size_bytes = max_size_bytes or getattr(settings, "EVIDENCE_MAX_FILE_SIZE_BYTES", 15 * 1024 * 1024)
        self.max_pdf_pages = max_pdf_pages or getattr(settings, "EVIDENCE_MAX_PDF_PAGES", 10)

    @classmethod
    def sanitize_client_filename(cls, raw_filename: Optional[str]) -> str:
        """Sanitizes user-provided filename, neutralizing path traversal tokens."""
        if not raw_filename or not isinstance(raw_filename, str):
            return "unnamed_evidence.bin"

        # Check for path traversal attempts
        if ".." in raw_filename or "/" in raw_filename or "\\" in raw_filename or "\0" in raw_filename:
            # Traversal attempt detected
            # Extract only the base name
            base = os.path.basename(raw_filename.replace("\\", "/"))
            cleaned = re.sub(r"[^\w\.\-\_]", "_", base).strip("._")
            return cleaned or "sanitized_evidence.bin"

        # Remove control characters, null bytes, and non-printable tokens
        cleaned = re.sub(r"[^\w\.\-\_]", "_", raw_filename).strip("._")
        return cleaned or "sanitized_evidence.bin"

    @classmethod
    def detect_signature(cls, file_bytes: bytes) -> Optional[Tuple[str, str]]:
        """Detects true MIME type and extension from byte header magic numbers."""
        if len(file_bytes) < 4:
            return None

        # Check JPEG (FF D8 FF)
        if file_bytes.startswith(JPEG_SIGNATURE):
            return ("image/jpeg", ".jpg")

        # Check PNG (89 50 4E 47 0D 0A 1A 0A)
        if file_bytes.startswith(PNG_SIGNATURE):
            return ("image/png", ".png")

        # Check PDF (%PDF-)
        if file_bytes.startswith(PDF_SIGNATURE):
            return ("application/pdf", ".pdf")

        return None

    def validate(
        self,
        file_bytes: bytes,
        client_filename: Optional[str] = None,
        client_content_type: Optional[str] = None,
        client_file_name: Optional[str] = None,
        client_mime_type: Optional[str] = None,
    ) -> ValidatedFileMetadata:
        """Alias for validate_file supporting both parameter naming conventions."""
        filename = client_filename or client_file_name
        mime_type = client_content_type or client_mime_type
        return self.validate_file(file_bytes, filename, mime_type)

    def validate_file(
        self,
        file_bytes: bytes,
        client_filename: Optional[str] = None,
        client_content_type: Optional[str] = None,
    ) -> ValidatedFileMetadata:
        """Comprehensive verification of an uploaded file."""
        # 1. Check for empty payload
        if not file_bytes or len(file_bytes) == 0:
            raise EmptyFileError("Uploaded evidence payload is empty (0 bytes).")

        # 2. Check maximum size limit
        file_size = len(file_bytes)
        if file_size > self.max_size_bytes:
            raise OversizedFileError(
                f"File size ({file_size} bytes) exceeds limit of {self.max_size_bytes} bytes (15 MB)."
            )

        # 3. Check for path traversal in filename
        if client_filename and ("../" in client_filename or "..\\" in client_filename or "\0" in client_filename):
            raise PathTraversalAttemptError(
                f"Path traversal characters detected in filename: '{client_filename}'"
            )

        # 4. Authoritative signature detection
        detected = self.detect_signature(file_bytes)
        if not detected:
            raise UnsupportedMediaTypeError(
                "Unrecognized or unsupported file signature. Only valid JPEG, PNG, and PDF files are allowed."
            )

        detected_mime, canonical_ext = detected

        # 5. Client MIME cross-validation
        if client_content_type:
            normalized_client_mime = client_content_type.strip().lower()
            # Standard aliases
            if normalized_client_mime == "image/jpg":
                normalized_client_mime = "image/jpeg"

            if normalized_client_mime != detected_mime:
                raise SignatureMismatchError(
                    f"MIME type mismatch: declared '{client_content_type}', "
                    f"but file signature indicates '{detected_mime}'."
                )

        # 6. PDF Structural & Page Count Verification
        page_count = 1
        is_pdf = detected_mime == "application/pdf"
        is_image = detected_mime.startswith("image/")

        if is_pdf:
            try:
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                # Validate PDF is not encrypted with a password
                if reader.is_encrypted:
                    raise CorruptedFileError("Password-protected or encrypted PDF documents cannot be processed.")

                page_count = len(reader.pages)
                if page_count == 0:
                    raise CorruptedFileError("PDF document contains 0 valid pages.")

                if page_count > self.max_pdf_pages:
                    raise PageCountExceededError(
                        f"PDF document contains {page_count} pages, exceeding the maximum allowed limit of {self.max_pdf_pages}."
                    )
            except EvidenceValidationError:
                raise
            except pypdf.errors.PyPdfError as exc:
                raise CorruptedFileError(f"Malformed or unreadable PDF document: {exc}")
            except Exception as exc:
                raise CorruptedFileError(f"Failed to inspect PDF structure: {exc}")

        # 7. Compute SHA-256 fingerprint & generate safe internal filename
        sha256 = hashlib.sha256(file_bytes).hexdigest()
        sanitized_name = self.sanitize_client_filename(client_filename)
        internal_name = f"{uuid.uuid4()}{canonical_ext}"

        return ValidatedFileMetadata(
            file_bytes=file_bytes,
            detected_mime_type=detected_mime,
            canonical_extension=canonical_ext,
            sha256_hash=sha256,
            file_size_bytes=file_size,
            sanitized_client_name=sanitized_name,
            internal_storage_filename=internal_name,
            page_count=page_count,
            is_pdf=is_pdf,
            is_image=is_image,
        )


default_file_validator = FileValidator()

