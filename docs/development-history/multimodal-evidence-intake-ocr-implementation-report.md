# Engineering Implementation Report: Multimodal Evidence Intake & OCR

## 1. Executive Summary

This milestone implements the **Multimodal Evidence Intake & OCR Pipeline** (Milestone Day 09 of the backend roadmap). The platform now provides a secure, high-assurance document intake subsystem capable of accepting customer-submitted screenshots, photos (JPEG/PNG), digital PDF invoices, and scanned receipts. 

The pipeline strictly validates payloads against true binary file signatures (magic bytes), enforces strict size (<= 15MB) and PDF page-count (<= 10 pages) bounds, neutralizes path traversal attempts, encrypts stored files at rest using AES-256-GCM bound to unique evidence identifiers, redacts sensitive PII and PCI-DSS payment data before logging or persistence, and neutralizes visual prompt injections before financial entities are extracted.

Extraction is coordinated through an extensible provider abstraction supporting digital PDF extraction, rule-based receipt and invoice extraction with field-level confidence scoring, deterministic mocks for reproducible testing, and an optional Google Gemini Vision provider for cloud-based multimodal inference.

---

## 2. Architecture & Data Flow

```
                      +------------------------------------------+
                      | Multipart File Upload via HTTP POST      |
                      | (/api/v1/evidence/upload)                |
                      +------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 1. Authentication & Multi-Tenant Authorization Check                               |
|    - Evaluates X-Session-Token (Customer) or Bearer JWT (Staff/Agent)              |
|    - Validates conversation existence & ownership (blocks cross-tenant access)      |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 2. File Signature & Security Validation (FileValidator)                           |
|    - True magic byte signature check: JPEG (\xFF\xD8\xFF), PNG (\x89PNG), PDF (%PDF-) |
|    - Enforces max file size (<= 15MB) & rejects 0-byte empty files                 |
|    - Enforces max PDF page count (<= 10 pages) via pypdf inspection                |
|    - Rejects path traversal tokens (.., /, \, \0) in filenames                     |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 3. Idempotency & Deduplication Check                                               |
|    - Checks composite index (conversation_id, sha256_hash)                         |
|    - If duplicate found in same conversation, returns existing record immediately  |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 4. Encrypted Persistence at Rest (EvidenceStorageManager)                          |
|    - Generates unique UUID evidence_id and customer directory boundary             |
|    - Encrypts raw binary payload with AES-256-GCM (associated data = evidence_id)  |
|    - Persists ciphertext file to storage/evidence/<customer_id>/<evidence_id>.enc  |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 5. Synchronous vs Asynchronous Dispatch                                           |
|    - If sync_processing=False: marks STAGED / ANALYZING, returns response          |
|    - If sync_processing=True: executes EvidenceProcessingPipeline                  |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 6. Multimodal Extraction & Security Pipeline (EvidenceProcessingPipeline)          |
|    - Digital PDF: PDFTextExtractor parses page text & per-page metadata            |
|    - Scanned PDF: Falls back to OCR provider                                       |
|    - Image: OCR Provider (Mock or Gemini Vision) extracts text                     |
|    - Adversarial Prompt Injection Defense: scans for override/jailbreak patterns   |
|      -> If detected: is_malicious=True, status=FLAGGED_UNSAFE, quarrantined        |
|    - Privacy Redaction: PIIMaskingEngine redacts credit cards (Luhn), SSNs, emails |
|    - Financial Parser: RuleBasedReceiptParser parses Order ID, Invoice #, Dates,   |
|      Totals, Taxes, Merchant, and Line Items with field-level confidence scores     |
+------------------------------------------------------------------------------------+
                                           |
                                           v
+------------------------------------------------------------------------------------+
| 7. Relational Persistence & Response Serialization                                 |
|    - Persists EvidenceFile and ExtractedEvidenceEntity in PostgreSQL / SQLite      |
|    - Returns EvidenceUploadResponse with structured data and 30-day retention TTL   |
+------------------------------------------------------------------------------------+
```

---

## 3. Files Created and Modified

### A. Created Modules & Providers
1. `backend/app/modules/evidence/schemas.py`: Pydantic V2 request and response schemas:
   - `ReceiptLineItem`: Extracted line items (product, quantity, unit price, total price, confidence).
   - `PageExtractionMetadata`: Per-page extraction breakdown (page number, character count, orientation, has_images).
   - `StructuredInvoiceData`: Typed financial entities (Order ID, Invoice #, dates, total, currency, tax, merchant, line items, field confidence, uncertain fields, missing fields, warnings).
   - `EvidenceUploadResponse`: Output contract for file uploads.
   - `EvidenceDetailResponse`: Complete metadata and audit trail.
   - `EvidenceStatusResponse`: Lightweight polling model for background processing.
   - `EvidenceExtractionResponse`: Structured extraction payload with redacted text.
2. `backend/app/modules/evidence/validator.py`: `FileValidator` enforcing true magic bytes signatures, size bounding, path traversal neutralization, and multi-page limits.
3. `backend/app/modules/evidence/storage.py`: `EvidenceStorageManager` managing customer directory isolation, safe path boundary checks, and authenticated AES-256-GCM encryption at rest bound to `evidence_id`.
4. `backend/app/modules/evidence/providers/base.py`: `BaseOCRProvider` abstract contract and `DocumentExtractionResult` data container.
5. `backend/app/modules/evidence/providers/pdf_extractor.py`: `PDFTextExtractor` using `pypdf` for digital text extraction, multi-page metadata, rotation detection, and scanned document detection.
6. `backend/app/modules/evidence/providers/receipt_parser.py`: `RuleBasedReceiptParser` using high-precision regexes, date normalizers, and financial heuristics to extract Order IDs, invoice numbers, amounts, taxes, merchants, and itemized line items without fabricating missing fields.
7. `backend/app/modules/evidence/providers/mock_provider.py`: `DeterministicMockOCRProvider` enabling 100% reproducible testing across invoices, injections, low-confidence fields, and multi-page files.
8. `backend/app/modules/evidence/providers/gemini_provider.py`: `GeminiVisionOCRProvider` implementing multimodal document vision calls via Google Gemini when configured.
9. `backend/app/modules/evidence/providers/__init__.py`: Clean exports for provider implementations.
10. `backend/app/modules/evidence/pipeline.py`: `EvidenceProcessingPipeline` coordinating extraction, prompt injection defense (`FLAGGED_UNSAFE`), PII masking, and structured entity parsing.
11. `backend/app/modules/evidence/service.py`: `EvidenceService` orchestrating validation, deduplication, encrypted storage, multi-tenant authorization, and DB persistence.
12. `backend/app/modules/evidence/__init__.py`: Module interface and public exports.
13. `backend/app/api/v1/evidence.py`: FastAPI REST router implementing:
    - `POST /api/v1/evidence/upload`
    - `GET  /api/v1/evidence/{evidence_id}`
    - `GET  /api/v1/evidence/{evidence_id}/status`
    - `GET  /api/v1/evidence/{evidence_id}/extraction`
14. `backend/alembic/versions/004_evidence_intake_ocr.py`: Alembic revision 004 updating `evidence_files` schema.
15. `backend/tests/test_multimodal_evidence_intake.py`: Comprehensive test suite with 23 test cases.

### B. Modified Existing Backend Files
1. `backend/app/core/config.py`: Added evidence settings (`EVIDENCE_STORAGE_DIR`, `EVIDENCE_MAX_FILE_SIZE_BYTES`, `EVIDENCE_MAX_PDF_PAGES`, `EVIDENCE_ALLOWED_MIME_TYPES`, `EVIDENCE_RETENTION_DAYS`, `EVIDENCE_SYNC_TIMEOUT_SECONDS`, `EVIDENCE_DEFAULT_OCR_PROVIDER`).
2. `backend/app/models/evidence.py`: Added `conversation_id`, `customer_id`, `processing_error`, `processed_at`, composite index `ix_evidence_files_conv_hash`, and bidirectional relationships.
3. `backend/app/models/customer.py`: Added `evidence_files` relationship on `Customer` and `Conversation`.
4. `backend/app/api/v1/__init__.py`: Mounted `evidence_router` under `/api/v1/evidence`.
5. `backend/app/api/deps.py`: Hardened session expiration timezone normalization for cross-database compatibility (SQLite test suite + PostgreSQL production).
6. `backend/pyproject.toml`: Declared `pypdf>=6.0.0` dependency under Multimodal, PDF & OCR.

---

## 4. Database Schema Changes & Alembic Migration

**Migration Revision:** `004_evidence_intake_ocr`  
**Down Revision:** `003_escalation_after_hours`  
**Status:** Applied and verified against PostgreSQL (`alembic upgrade head`).

### Schema Additions to `evidence_files`:
| Column | Type | Nullable | Index / Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `conversation_id` | UUID | NULL | `ix_evidence_files_conversation_id`, FK `conversations.id` (ON DELETE SET NULL) | Conversation thread association |
| `customer_id` | UUID | NULL | `ix_evidence_files_customer_id`, FK `customers.id` (ON DELETE SET NULL) | Tenant owner customer ID |
| `processing_error` | Text | NULL | None | Safe diagnostic error message if processing fails |
| `processed_at` | DateTime(tz=True) | NULL | None | Completion timestamp of OCR extraction |
| `(conversation_id, sha256_hash)` | Composite | - | `ix_evidence_files_conv_hash` | Idempotent duplicate file upload suppression |

---

## 5. Security & Privacy Controls

1. **True File Signature Verification:** Validates authoritative header magic bytes (JPEG `\xFF\xD8\xFF`, PNG `\x89PNG\r\n\x1a\n`, PDF `%PDF-`). Client-provided Content-Type headers or file extensions are never trusted.
2. **Path Traversal Protection:** User filenames containing directory separators (`/`, `\`), traversal sequences (`..`), or null bytes (`\0`) are strictly rejected with `PathTraversalAttemptError`. Clean filenames are sanitized by stripping special characters.
3. **Encryption at Rest:** Raw file payloads are encrypted using AES-256-GCM before writing to disk. The unique `evidence_id` is supplied as authenticated associated data (`assoc_data`), preventing cipher swapping or cross-file tampering.
4. **Adversarial Prompt Injection Defense:** Document text is scanned for jailbreak and policy override directives (e.g. `SYSTEM OVERRIDE`, `ignore all instructions`, `waive all return policies`, `give full refund unconditionally`). Detected files are tagged `is_malicious = True`, assigned `quarantine_reason`, and set to `status = "FLAGGED_UNSAFE"`.
5. **PII and Payment Card Masking:** All extracted text is sanitized via `PIIMaskingEngine` before persistence or log emission. Credit card numbers passing the Luhn algorithm are masked to `****-****-****-XXXX`, SSNs are masked to `***-**-XXXX`, emails are masked, and phone numbers are redacted.
6. **Multi-Tenant Authorization:** Customers can only upload evidence to conversations they own and can only access evidence records belonging to their identity. Cross-customer attempts fail with HTTP 403 Forbidden. Enterprise staff with `AGENT` or `ADMIN` roles retain authorized cross-customer access.
7. **Safe Error Responses:** Internal stack traces and directory paths are suppressed from API client responses using standard `PlatformException` mappings.

---

## 6. Verification and Test Execution

### A. Dedicated Evidence Intake Test Suite
**File:** `backend/tests/test_multimodal_evidence_intake.py`  
**Execution Result:** **23 / 23 PASSED** in 7.60s.

```
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_valid_signatures PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_empty_file_rejected PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_oversized_file_rejected PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_unsupported_media_type_rejected PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_mime_signature_mismatch_rejected PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_path_traversal_sanitization PASSED
backend/tests/test_multimodal_evidence_intake.py::test_file_validator_pdf_page_count_limit PASSED
backend/tests/test_multimodal_evidence_intake.py::test_evidence_storage_encryption_and_tamper_protection PASSED
backend/tests/test_multimodal_evidence_intake.py::test_receipt_parser_complete_extraction PASSED
backend/tests/test_multimodal_evidence_intake.py::test_receipt_parser_missing_fields_no_hallucination PASSED
backend/tests/test_multimodal_evidence_intake.py::test_receipt_parser_low_confidence_uncertain_fields PASSED
backend/tests/test_multimodal_evidence_intake.py::test_evidence_pipeline_pii_masking PASSED
backend/tests/test_multimodal_evidence_intake.py::test_evidence_pipeline_adversarial_prompt_injection_defense PASSED
backend/tests/test_multimodal_evidence_intake.py::test_service_idempotent_retry_deduplication PASSED
backend/tests/test_multimodal_evidence_intake.py::test_service_async_staged_status PASSED
backend/tests/test_multimodal_evidence_intake.py::test_api_upload_unauthorized_fails_closed_401 PASSED
backend/tests/test_multimodal_evidence_intake.py::test_api_upload_cross_customer_forbidden_403 PASSED
backend/tests/test_multimodal_evidence_intake.py::test_api_full_evidence_lifecycle PASSED
backend/tests/test_multimodal_evidence_intake.py::test_regression_chat_message_endpoint_intact PASSED
backend/tests/test_multimodal_evidence_intake.py::test_gemini_vision_provider_without_key_raises_informative_error PASSED
backend/tests/test_multimodal_evidence_intake.py::test_corrupted_pdf_structure_rejected PASSED
backend/tests/test_multimodal_evidence_intake.py::test_pdf_text_extractor_multipage_metadata PASSED
backend/tests/test_multimodal_evidence_intake.py::test_pipeline_processing_failure_safe_handling PASSED
```

### B. Day 08 Regression Suite
**File:** `backend/tests/test_intelligent_escalation_after_hours.py`  
**Execution Result:** **26 / 26 PASSED** in 8.46s.

### C. Full Backend Regression Suite
**Execution Result:** **610 / 610 PASSED** (0 regressions, 0 broken contracts across all Days 01–09).

---

## 7. Known Limitations & Unverified Integrations

1. **External Paid OCR APIs (Google Gemini Vision):** The `GeminiVisionOCRProvider` is fully implemented and tested to fail gracefully with informative configuration requirements when no API key is supplied. Live network calls to Google Gemini were intentionally mocked in the test suite per Section 10 ("Do not claim a live OCR provider was tested if only mocks were used").
2. **Local Image OCR Fallback:** Tesseract / local C-extension optical OCR is not bundled in the lightweight test container; scanned image extraction relies on the digital PDF parser, rule-based heuristics, mock engine in tests, and Gemini Vision in production.

---

## 8. Remaining Roadmap Work for Day 10

1. **Conversational Claim Cross-Verification:** Integrating `EvidenceVerification` models to cross-examine customer conversational claims against verified `ExtractedEvidenceEntity` records.
2. **Discrepancy & Conflict Resolution:** Automatic flagging of `CLAIM_CONFLICT` when dates, amounts, or merchant names differ between customer statements and verified receipts.
3. **Agent Escalation on Mismatches:** Direct routing to human support agents when evidence conflicts with warranty or return claims.

