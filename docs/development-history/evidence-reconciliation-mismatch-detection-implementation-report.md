# Engineering Implementation Report: Evidence Reconciliation & Intelligent Mismatch Detection

## 1. Executive Summary

This milestone implements the **Evidence Reconciliation & Intelligent Mismatch Detection Subsystem** (Day 10 milestone of the backend roadmap). The system reconciles customer conversational claims against structured OCR evidence extracted from uploaded receipts, invoices, screenshots, and PDFs.

The platform automatically identifies:
1. **Verifiable Matches**: Aligning customer assertions (order numbers, invoice IDs, payment amounts, dates, products, merchants) with document evidence.
2. **Factual Conflicts & Discrepancies**: Detecting and quantifying numerical differences (e.g., customer claimed ₹2,499 vs document total ₹2,999, calculating an exact diff of ₹-500.00).
3. **Currency Parity Violations**: Explicitly barring cross-currency matching (e.g. ₹2,499 INR vs $2,499 USD).
4. **Missing Information**: Discerning when assertions lack document support (`MISSING_FROM_EVIDENCE`) or when evidence provides extra detail unstated by the customer (`MISSING_FROM_MESSAGE`).
5. **OCR Uncertainty & Blurry Scans**: Categorizing low OCR confidence (< 0.70) as `UNCERTAIN` rather than false conflicts.
6. **Complex / Insufficient Claims**: Handling claims such as duplicate charges (`INTENT_DOUBLE_CHARGE`) where a single invoice cannot substantiate multiple debits, demanding second-transaction proof.
7. **Adversarial & Quarantined Containment**: Preventing files flagged `FLAGGED_UNSAFE` or `is_malicious=True` from verifying claims or executing prompt injection attacks.
8. **Clarification Lifecycle & Re-evaluation**: Dynamically generating targeted, privacy-preserving clarification queries, receiving customer responses, and re-evaluating claims to resolution.

All 31 focused verification tests and all 641 backend regression tests pass cleanly with exit code 0.

---

## 2. Architecture & Data Flow

```
                                  +------------------------------------+
                                  | Customer Conversational Utterance  |
                                  | "I paid ₹2,499 for ORD-12345..."   |
                                  +------------------------------------+
                                                     |
                                                     v
                                  +------------------------------------+
                                  | ClaimExtractionEngine              |
                                  | - Order IDs & Invoice #s           |
                                  | - Amounts & Currency Parity        |
                                  | - Dates & Tolerances               |
                                  | - Products, Quantities, Merchants  |
                                  | - Complex Intent (Charged Twice)   |
                                  | - PII Masking Engine               |
                                  +------------------------------------+
                                                     |
                                                     v
+-------------------------------+ +------------------------------------+
| Stored OCR Evidence File      | | Structured Customer Claims         |
| - EvidenceFile (Status/Safety)| | - ExtractedClaimsPayload           |
| - ExtractedEvidenceEntity     | | - Atomic CustomerClaim objects     |
| - StructuredInvoiceData       | | - PII-Sanitized Claim Text         |
+-------------------------------+ +------------------------------------+
               \                                    /
                \                                  /
                 v                                v
+----------------------------------------------------------------------+
| EvidenceReconciliationEngine                                         |
| - Order ID: Normalized alphanumeric token comparison                 |
| - Amount: Currency parity check & signed diff_amount calculation     |
| - Date: ISO date normalization within date_tolerance_days            |
| - Product & Merchant: Token intersection & fuzzy matching            |
| - Confidence Threshold (< 0.70 forces UNCERTAIN)                     |
| - Quarantined File Gate (is_malicious forces INSUFFICIENT_EVIDENCE)  |
| - Complex Intent Gate (Single invoice -> INSUFFICIENT_EVIDENCE)      |
+----------------------------------------------------------------------+
                                   |
                                   v
+----------------------------------------------------------------------+
| ClarificationEngine                                                  |
| If MISMATCH, UNCERTAIN, or MISSING_FROM_EVIDENCE:                    |
| - Synthesizes secure, non-PCI, human-readable clarification prompts  |
| - State machine: NONE -> PENDING_CUSTOMER -> RESOLVED / SUPERSEDED   |
| - Re-evaluation logic merging customer clarification response        |
+----------------------------------------------------------------------+
                                   |
                                   v
+----------------------------------------------------------------------+
| ReconciliationService & Database Persistence                         |
| - Alembic Migration 005 (evidence_verifications)                     |
| - Idempotency key deduplication (suppresses duplicate background jobs)|
| - Async background job tracking, polling, and crash recovery         |
| - Multi-tenant isolation (cross-customer access strictly denied)     |
+----------------------------------------------------------------------+
```

---

## 3. Core Components Implemented

### 3.1 Claim Extraction Engine (`app/modules/evidence/claims.py`)
- **`ClaimExtractionEngine`**: Parses conversational natural language without requiring LLM overhead for predictable, deterministic claim resolution.
- **Pattern Matchers**:
  - `ORDER_ID`: Regex patterns capturing `ORD-XXXXX`, `ORDER-XXXXX`, and generic alphanumeric identifiers.
  - `INVOICE_NUMBER`: Regex patterns capturing `INV-XXXXX` and invoice references.
  - `PAYMENT_AMOUNT` & `CURRENCY`: Symbol-to-ISO mappings (`₹`/`rs`/`rupees` -> `INR`, `$` -> `USD`, `€` -> `EUR`, `£` -> `GBP`, `¥` -> `JPY`). Formats amounts with decimal and comma separation.
  - `TRANSACTION_DATE`: Standard ISO `YYYY-MM-DD`, `DD-MM-YYYY`, and natural month strings (`Jan 15, 2026`).
  - `INTENT_DOUBLE_CHARGE`: Detects phrasing such as "charged twice", "double charge", "billed two times", "duplicate deduction".
- **Privacy Sanitization**: Automatically scrubs credit cards, emails, and phone numbers via `PIIMaskingEngine` before constructing `ExtractedClaimsPayload`.

### 3.2 Evidence Reconciliation Engine (`app/modules/evidence/reconciler.py`)
- **`EvidenceReconciliationEngine`**: Compares `ExtractedClaimsPayload` against `StructuredInvoiceData`.
- **Authoritative Outcomes**:
  - `MATCH`: All customer claims align with document evidence.
  - `MISMATCH`: Factual contradiction detected; records exact difference (`diff_amount`).
  - `MISSING_FROM_EVIDENCE`: Customer asserted a field not present on document.
  - `MISSING_FROM_MESSAGE`: Document contained detail not mentioned by customer.
  - `UNCERTAIN`: Extraction confidence for compared field is below threshold (< 0.70).
  - `INSUFFICIENT_EVIDENCE`: Quarantined document, empty evidence, or complex intent (double charge) lacking multi-transaction proof.
- **Rules Enforced**:
  - *Quarantine Gate*: If `is_malicious=True` or `status=="FLAGGED_UNSAFE"`, reconciliation immediately aborts with `INSUFFICIENT_EVIDENCE`.
  - *Currency Parity*: If customer claims `$100` and document is `₹100`, flags `currency` and `total_amount` as `MISMATCH`.
  - *Numeric Differences*: Calculates `diff = claimed_amount - evidence_amount` and unit.

### 3.3 Clarification Management & Re-evaluation (`app/modules/evidence/clarification.py`)
- **`ClarificationEngine`**: Drives interactive resolution of discrepancies.
- **PCI Security**: Strictly forbids requesting full credit card numbers, CVV, passwords, PINs, or tokens.
- **State Lifecycle**:
  - `NONE_REQUIRED`: Clean match or no discrepancies.
  - `PENDING_CUSTOMER`: Clarification inquiry dispatched to customer.
  - `RESOLVED`: Customer clarification supplied missing or corrected facts; re-evaluation yielded `MATCH`.
  - `SUPERSEDED`: Subsequent evidence uploaded or superseded by new claim.
  - `FAILED`: Customer provided invalid responses repeatedly or timed out.
- **Re-evaluation**: Merges clarification claims into original assertions and re-runs reconciliation.

### 3.4 Reconciliation Domain Service (`app/modules/evidence/reconciliation_service.py`)
- **`ReconciliationService`**: Production coordinator providing:
  - `evaluate_reconciliation`: Synchronous verification fast-path and asynchronous background queuing (`async_job=True` returns immediately with `job_status="PENDING"`).
  - `get_verification`: Multi-tenant authorized retrieval of verification verdicts.
  - `get_job_status`: Polling endpoint for background processing jobs enforcing conversation-level customer ownership (cross-customer access strictly yields 403 Forbidden).
  - `get_conversation_clarifications`: Active pending clarification inquiries for a conversation.
  - `respond_to_clarification`: Customer reply submission and automated re-evaluation.
  - `recover_stale_jobs`: Crash-recovery scanner handling timed-out or crashed background jobs with backoff retry count.
  - *Idempotency*: Composite index `(conversation_id, idempotency_key)` returns cached results on duplicate submissions.

### 3.5 Relational Database Schema & Migration 005
- **Migration**: `backend/alembic/versions/005_evidence_reconciliation.py`.
- **Database Table Enhanced**: `evidence_verifications`:
  - `created_at`: Non-nullable creation timestamp (`DateTime(timezone=True)`, server_default=now()).
  - `processing_started_at`: Nullable timestamp recording when a worker claimed the job.
  - `verified_at`: Nullable completion timestamp (`None` while `PENDING` or `PROCESSING`).
  - `conversation_id`: Foreign key to `conversations.id` (Indexed).
  - `message_id`: Foreign key to `messages.id` (Indexed).
  - `reconciliation_status`: Categorical outcome enum string (`MATCH`, `MISMATCH`, etc.).
  - `clarification_status`: Clarification lifecycle string (`NONE`, `PENDING_CUSTOMER`, `RESOLVED`, etc.).
  - `clarification_prompt`: Rendered customer inquiry.
  - `extracted_claims`: JSONB structured customer claims.
  - `field_comparisons`: JSONB field-by-field outcomes and calculated numeric differences.
  - `overall_confidence`: Aggregate confidence score.
  - `job_id`: Stable UUID string for async jobs (Indexed).
  - `job_status`: Background processing state (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`).
  - `retry_count`: Integer tracking retry attempts for crash recovery.
  - `idempotency_key`: Client-supplied deduplication token (Indexed).
  - `resolved_at`: Timestamp when clarification was resolved.
  - Composite Index: `ix_evidence_verifications_conv_idem` on `(conversation_id, idempotency_key)`.

### 3.6 Background Reconciliation Worker (`app/modules/evidence/worker.py`)
- **`ReconciliationWorker`**: Dedicated asynchronous background processor integrated into the platform worker lifecycle (`lifecycle.py` and `main.py`).
  - **Concurrency Safety**: Employs `with_for_update(skip_locked=True)` on PostgreSQL to guarantee multiple worker processes cannot double-claim or race on pending jobs.
  - **Batch Scanning (`scan_and_process_batch`)**: Claims `PENDING` jobs, sets `processing_started_at`, transitions status to `PROCESSING`, performs natural-language claim extraction and invoice reconciliation, records comparisons, and sets `COMPLETED` with `verified_at = utc_now()`.
  - **Stale Job Recovery (`recover_stale_jobs`)**: Periodic scanner identifying stuck `PENDING` and `PROCESSING` jobs (including those with NULL `verified_at` or NULL `processing_started_at`) exceeding timeout thresholds. Increments `retry_count`, resets to `PENDING` for retries (< 3), or transitions to terminal `FAILED` state (>= 3).
  - **Graceful Lifecycle Management**: Registered in `WorkerLifecycleManager` and FastAPI lifespan with non-blocking periodic background task and clean cooperative shutdown via `asyncio.Event`.

---

## 4. API Endpoints Contract

All endpoints are mounted under `/api/v1/evidence/reconcile`:

| Method | Path | Summary | Auth |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/evidence/reconcile` | Evaluates claim text against uploaded evidence record | Session / JWT |
| `GET` | `/api/v1/evidence/reconcile/{id}` | Fetches verification verdict and field comparisons | Session / JWT |
| `GET` | `/api/v1/evidence/reconcile/status/{job_id}` | Polls async reconciliation job progress and errors | Session / JWT |
| `GET` | `/api/v1/evidence/reconcile/clarifications/{conv_id}` | Lists active clarification requests for conversation | Session / JWT |
| `POST` | `/api/v1/evidence/reconcile/clarifications/{id}/respond` | Submits customer clarification and triggers re-evaluation | Session / JWT |

---

## 5. Security & Multi-Tenant Isolation

1. **Multi-Tenant Authorization**: Customers can only reconcile evidence within conversations they own. Cross-customer access attempts fail closed with `403 Forbidden`.
2. **Visual & Adversarial Prompt Injection Containment**: Evidence flagged as `FLAGGED_UNSAFE` or `is_malicious=True` during Day 09 intake is immediately blocked from verifying customer claims. Adversarial text embedded in images cannot spoof claim approval.
3. **PII & PCI Protection**:
   - Customer claim text is scrubbed via `PIIMaskingEngine.mask()` prior to database persistence.
   - Clarification prompts never ask for sensitive cardholder data (CVV, full card numbers, passwords, PINs).
   - Structured logging utilizes `configure_safe_logger` with `SafeLoggingFilter` and `PIISanitizingFormatter`.
4. **Idempotency & Resource Abuse Prevention**: Idempotency keys prevent duplicate background job creation and replay attacks.

---

## 6. Verification & Test Suite

A dedicated test suite was authored in `backend/tests/test_evidence_reconciliation.py` containing 31 comprehensive test cases:

```
backend/tests/test_evidence_reconciliation.py:
  test_exact_match_claim_and_evidence                          PASSED [  3%]
  test_amount_mismatch_with_calculated_difference             PASSED [  6%]
  test_order_id_mismatch                                      PASSED [  9%]
  test_date_mismatch_outside_tolerance                         PASSED [ 12%]
  test_currency_mismatch_parity                               PASSED [ 16%]
  test_missing_field_from_evidence                            PASSED [ 19%]
  test_missing_field_from_message                             PASSED [ 22%]
  test_low_ocr_confidence_forces_uncertain                    PASSED [ 25%]
  test_multiple_evidence_field_aggregation                     PASSED [ 29%]
  test_insufficient_evidence_complex_claim_double_charge       PASSED [ 32%]
  test_quarantined_evidence_rejects_reconciliation             PASSED [ 35%]
  test_visual_prompt_injection_defense                        PASSED [ 38%]
  test_clarification_generation_amount_mismatch               PASSED [ 41%]
  test_clarification_generation_low_confidence                PASSED [ 45%]
  test_clarification_response_handling_missing_order_id       PASSED [ 48%]
  test_clarification_re_evaluation_resolves_to_match         PASSED [ 51%]
  test_duplicate_reconciliation_prevention_idempotency        PASSED [ 54%]
  test_job_status_tracking_and_completion                    PASSED [ 58%]
  test_crash_recovery_stale_jobs                              PASSED [ 61%]
  test_processing_timeout_handling                            PASSED [ 64%]
  test_cross_customer_data_isolation                         PASSED [ 67%]
  test_pii_masking_in_claims_and_persistence                 PASSED [ 70%]
  test_database_persistence_and_relationships                 PASSED [ 74%]
  test_reconciliation_api_endpoints                          PASSED [ 77%]
  test_unauthorized_access_fail_closed                        PASSED [ 80%]
  test_regression_chat_endpoint_still_functional              PASSED [ 83%]
  test_reconciliation_worker_processes_pending_batch         PASSED [ 87%]
  test_reconciliation_worker_concurrency_safe                 PASSED [ 90%]
  test_stale_job_recovery_with_null_verified_at               PASSED [ 93%]
  test_job_status_endpoint_authorization                     PASSED [ 96%]
  test_async_job_submission_and_worker_completion            PASSED [100%]
```

### Full Backend Regression Suite Results:
- **Command**: `.venv/Scripts/pytest.exe backend/tests -q`
- **Total Tests Passed**: **641 passed**
- **Failed**: 0
- **Errors**: 0
- **Duration**: 123.98s
- **Exit Code**: 0

### Alembic Migration Verification:
- **Command**: `python -m alembic current`
- **Output**: `005_evidence_reconciliation (head)`
- **Heads**: `005_evidence_reconciliation (head)`
- **Downgrade/Upgrade Round-Trip**: Verified clean execution of `alembic downgrade -1` and `alembic upgrade head`.

---

## 7. Permanent Naming Rule Compliance

In strict adherence to the project standards:
- No file names contain day-number references.
- Dedicated test file: `backend/tests/test_evidence_reconciliation.py`.
- Implementation report: `docs/development-history/evidence-reconciliation-mismatch-detection-implementation-report.md`.
- No frontend files were modified.
- No changes staged, committed, or pushed without user approval.
