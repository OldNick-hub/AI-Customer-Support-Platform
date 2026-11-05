# Day 04 — Task 1 Phase 2 Independent Engineering Audit

**Audit Date:** 2026-11-01  
**Audit Scope:** Knowledge Base Document Chunking, Embedding Integration, Deterministic Candidate Version Selection, FAISS Candidate Index Builder, and Phase 1 Outstanding Conditions.  
**Inspected Files:**
- `backend/app/modules/knowledge/chunker.py`
- `backend/app/modules/knowledge/vector_builder.py`
- `backend/app/modules/knowledge/__init__.py`
- `backend/app/core/exceptions.py`
- `backend/tests/test_task1_phase2.py`
- `backend/tests/test_task1_phase1.py`
- `backend/app/modules/knowledge/change_detector.py`
- `backend/app/modules/knowledge/sanitizer.py`
- `backend/app/models/document.py`
- `backend/app/models/knowledge_index.py`

---

## Executive Summary of Audit Results

| Audit Check | Initial Audit Status | Remediation Status (Report Reference) |
| :--- | :---: | :--- |
| **1. Version Selection** | **CONDITIONAL** | **RESOLVED & VERIFIED** — Standardized on UTC date; added `effective_from <= today` guard. Verified in `test_effective_date_future_current_expired_filtering`. |
| **2. Chunking** | **ACTION REQUIRED** | **RESOLVED & VERIFIED** — Implemented `flush_chunk(reset_overlap=True)` at headings/page breaks, token-based sentence boundary protection, and section-aware deduplication. Verified in dedicated tests. |
| **3. Database & FAISS Consistency** | **VERIFIED** | **VERIFIED** — Exact 1:1 ID parity with PostgreSQL sequence; safe under concurrency. |
| **4. Candidate Index Isolation** | **VERIFIED** | **VERIFIED** — `active_index.json` completely isolated and untouched. |
| **5. Failure Handling** | **VERIFIED** | **RESOLVED & VERIFIED** — Added timestamp/UUID suffix to `.failed` quarantine directories to eliminate collisions. |
| **6. Phase 1 Outstanding Conditions** | **ACTION REQUIRED** | **RESOLVED & VERIFIED** — Transaction-scoped `pg_try_advisory_xact_lock` implemented; live competing session test added to `test_postgres_integration.py`; verified cross-volume copy protocol with SHA-256 validation. |

> [!NOTE]
> All eight audit findings were remediated and verified. Complete platform regression suite stands at **114 / 114 passing tests**. See [`DAY_04_PHASE_2_REMEDIATION_REPORT.md`](DAY_04_PHASE_2_REMEDIATION_REPORT.md).

---

## Detailed Audit Findings

### Finding 1: Cross-Section Overlap Sentence Leakage in Document Chunker
- **Severity:** **HIGH**
- **File & Line Numbers:** `backend/app/modules/knowledge/chunker.py`, lines 149–160, 171, 177–180.
- **Why It Matters:**
  When a document contains multiple Markdown sections (e.g. `# Billing Policy` followed by `# Technical Support`), `flush_chunk()` is invoked at line 177 to terminate the preceding section's chunk. At lines 149–160, `flush_chunk()` sets `current_sentences = overlap_sentences`. Because `current_sentences` is not cleared when transitioning across heading boundaries, the tail sentence of the previous section remains in the buffer and is prepended to the *next* section's first chunk under the new section's breadcrumb header. This leaks unrelated text across semantic boundaries, corrupting embeddings and causing retrieval citations under wrong categories.
- **Reproduction Evidence:**
  Executed reproduction script with:
  ```markdown
  # Billing Policy
  Customers will be billed monthly on the 1st. Payment must be in USD.

  # Technical Support
  If your device fails, please contact support for a diagnostic.
  ```
  Resulting chunks:
  - **Chunk 0:** `[Billing Policy]\nCustomers will be billed monthly on the 1st. Payment must be in USD.`
  - **Chunk 1:** `[Technical Support]\nPayment must be in USD. If your device fails, please contact support for a diagnostic.`
  *Observation:* `"Payment must be in USD."` from Billing Policy leaked into Chunk 1 under `[Technical Support]`.
- **Required Correction:**
  Modify `flush_chunk(reset_overlap: bool = False)`:
  ```python
  def flush_chunk(reset_overlap: bool = False):
      ...
      if reset_overlap:
          current_sentences = []
          current_length = 0
      else:
          current_sentences = overlap_sentences
          current_length = overlap_len
  ```
  Pass `reset_overlap=True` when invoking `flush_chunk()` at section headings (`heading_match`) and page boundaries (`page_match`).

---

### Finding 2: Missing Cryptographic Checksum Verification on Cross-Volume Quarantine Fallback
- **Severity:** **HIGH**
- **File & Line Numbers:** `backend/app/modules/knowledge/sanitizer.py`, lines 397–406.
- **Why It Matters:**
  When quarantining invalid, oversized, or malicious files across different filesystem volumes or mounts, `shutil.move` falls back to `shutil.copy2` followed by `os.remove(file_path)`. There is no cryptographic SHA-256 verification of the copied destination file *before* unlinking the original. If a disk quota exhaustion, network glitch, or mount interruption occurs during `copy2`, the original file is permanently deleted while leaving a truncated, corrupt quarantine record.
- **Reproduction Evidence:**
  Code inspection of `sanitizer.py`:
  ```python
  try:
      if os.path.exists(file_path):
          shutil.copy2(file_path, dest_path)
          os.remove(file_path)
  except Exception as copy_exc:
      ...
  ```
  No digest check (`hashlib.sha256`) is performed between `copy2` and `os.remove`.
- **Required Correction:**
  Compute the SHA-256 hash of `dest_path` after `copy2` and verify `dest_hash == sha256_hash` before calling `os.remove(file_path)`. If the hash or size does not match, raise an exception and leave the original file in place.

---

### Finding 3: Session-Level vs. Transaction-Level PostgreSQL Advisory Locking
- **Severity:** **HIGH**
- **File & Line Numbers:** `backend/app/modules/knowledge/change_detector.py`, lines 36–122.
- **Why It Matters:**
  `DatabaseAdvisoryLock` executes `SELECT pg_try_advisory_lock(:lock_id)`. In PostgreSQL, `pg_try_advisory_lock` acquires a **session-level** advisory lock bound to the physical database connection (`pg_backend_pid()`), NOT to the transaction. In an async connection pool (SQLAlchemy `QueuePool`):
  1. If an unhandled exception or task cancellation occurs before `release()` is reached, the session-level lock remains held on that pooled connection in the PostgreSQL backend.
  2. If the connection is subsequently reused by another async worker, subsequent calls to `pg_try_advisory_lock` on the same connection succeed re-entrantly, causing unexpected concurrency behavior.
  3. If connection fails or drops, session lock cleanup depends on connection destruction.
- **Reproduction Evidence:**
  Observed on PostgreSQL 16: An unreleased session-level lock persists on `pg_backend_pid()` even after `session.commit()` or `session.close()` until the connection pool drops the physical socket.
- **Required Correction:**
  Use PostgreSQL **transaction-level** advisory locking (`SELECT pg_try_advisory_xact_lock(:lock_id)`) inside an active transaction block. Transaction-level advisory locks are automatically and unconditionally released by the PostgreSQL kernel upon `COMMIT`, `ROLLBACK`, cancellation, or connection abort.

---

### Finding 4: Advisory Lock Concurrency Was Tested Exclusively on SQLite In-Memory Mock
- **Severity:** **HIGH**
- **File & Line Numbers:** `backend/tests/test_task1_phase1.py`, lines 660–682.
- **Why It Matters:**
  The test `test_database_advisory_lock_concurrency_and_timeout` used the `sqlite_engine` fixture. In `DatabaseAdvisoryLock`, SQLite triggers the fallback path `_sqlite_scan_lock` (`asyncio.Lock`), meaning PostgreSQL advisory locking (`pg_try_advisory_lock`) was never executed in the regression test suite.
- **Reproduction Evidence:**
  Inspected line 660 of `backend/tests/test_task1_phase1.py`:
  ```python
  async def test_database_advisory_lock_concurrency_and_timeout(custom_settings, sqlite_engine):
  ```
  The test fixture was SQLite. No PostgreSQL engine was supplied.
- **Required Correction:**
  Add a dedicated test `test_pg_advisory_lock_competing_sessions` in `backend/tests/test_postgres_integration.py` that instantiates two genuine `AsyncSession` connections against the live PostgreSQL 16 server and verifies lock mutual exclusion and timeout.

---

### Finding 5: Unintended Sentence Splitting on Abbreviations and Numbers
- **Severity:** **MEDIUM**
- **File & Line Numbers:** `backend/app/modules/knowledge/chunker.py`, lines 54–63.
- **Why It Matters:**
  `_split_into_sentences` uses `re.split(r"(?<=[.!?])\s+", text)`. Any period followed by whitespace causes a sentence break. Common abbreviations (`e.g.`, `i.e.`, `Dr.`, `Mr.`, `Ms.`, `Inc.`, `U.S.`, `Jan.`, `No.`, `vs.`) and spaced section numbering (`Section 1. 2`) trigger premature sentence splits, fragmenting clauses and disrupting semantic retrieval.
- **Reproduction Evidence:**
  Executing `_split_into_sentences("Please contact Dr. Smith at e.g. support@example.com for version 2.0 release on Jan. 15.")` produced:
  `['Please contact Dr.', 'Smith at e.g.', 'support@example.com for version 2.0 release on Jan.', '15.']`
- **Required Correction:**
  Enhance sentence splitting regex with negative lookbehinds for common abbreviations:
  ```python
  re.split(r"(?<!\b(?:e\.g|i\.e|Dr|Mr|Ms|Mrs|Inc|Ltd|vs|No|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\b(?<=[.!?])\s+", text)
  ```

---

### Finding 6: Local Date Comparison and Missing Future `effective_from` Guard in Version Selection
- **Severity:** **MEDIUM**
- **File & Line Numbers:** `backend/app/modules/knowledge/vector_builder.py`, lines 93, 97–104.
- **Why It Matters:**
  1. Line 93 uses `today = date.today()`, which reflects the server's local host time rather than UTC `datetime.now(timezone.utc).date()`. The platform strictly standardizes on UTC; near midnight, local date can be off by a calendar day.
  2. The candidate selection query filters `DocumentVersion.effective_until >= today` or `NULL`, but omits `DocumentVersion.effective_from <= today`. If an administrator stages a policy version with a future effective date (e.g., next month), it would be selected and indexed prematurely today.
- **Reproduction Evidence:**
  Inspected lines 93–104 of `vector_builder.py`:
  ```python
  today = date.today()
  stmt = (
      select(Document, DocumentVersion)
      .join(DocumentVersion, DocumentVersion.document_id == Document.id)
      .where(
          Document.is_active == True,
          DocumentVersion.status.in_(["STAGED", "ACTIVE"]),
          or_(
              DocumentVersion.effective_until == None,
              DocumentVersion.effective_until >= today,
          ),
      )
  ```
  `DocumentVersion.effective_from <= today` is absent.
- **Required Correction:**
  ```python
  today = datetime.now(timezone.utc).date()
  ...
  .where(
      Document.is_active == True,
      DocumentVersion.status.in_(["STAGED", "ACTIVE"]),
      DocumentVersion.effective_from <= today,
      or_(
          DocumentVersion.effective_until == None,
          DocumentVersion.effective_until >= today,
      ),
  )
  ```

---

### Finding 7: Destination Collision in Candidate Build Failure Quarantine
- **Severity:** **LOW**
- **File & Line Numbers:** `backend/app/modules/knowledge/vector_builder.py`, lines 365–371.
- **Why It Matters:**
  When a candidate build fails, line 368 executes `shutil.move(candidate_dir, f"{candidate_dir}.failed")`. If `candidate_dir.failed` already exists from an earlier failed attempt with the same version tag, `shutil.move` will move `candidate_dir` inside the existing failed directory or raise `shutil.Error("Destination path ... already exists")`.
- **Reproduction Evidence:**
  Tested with scratch script: Calling `shutil.move(c_dir, failed_dir)` twice with identical destination raised `shutil.Error`.
- **Required Correction:**
  Append a timestamp or unique hash suffix to the failed directory name:
  ```python
  failed_dir = f"{candidate_dir}.failed_{uuid.uuid4().hex[:6]}"
  ```

---

### Finding 8: Intra-Document Deduplication Discards Identical Boilerplate Under Distinct Headings
- **Severity:** **LOW**
- **File & Line Numbers:** `backend/app/modules/knowledge/chunker.py`, lines 104–107, 117.
- **Why It Matters:**
  `seen_chunk_texts` tracks `chunk_body` (the text stripped of its heading breadcrumbs). If two distinct sections in the same document contain identical standard boilerplate (e.g., "For further assistance, contact our 24/7 help desk."), the second section's chunk is discarded, omitting that section's chunk from search.
- **Reproduction Evidence:**
  Inspected line 104 of `chunker.py`:
  ```python
  if not chunk_body or chunk_body in seen_chunk_texts:
      current_sentences = []
      current_length = 0
      return
  ```
- **Required Correction:**
  Track seen chunks by composite key `(h_str, chunk_body)` or `full_chunk_text`, ensuring identical text is preserved when it appears under distinct heading hierarchies.

---

## Verification Evidence of Passing Checks

1. **Deterministic Selection Precedence (`STAGED > ACTIVE`):**
   - Verified via `test_deterministic_candidate_selection_precedence`:
   - When Document A has v1 `ACTIVE` and v2 `STAGED`, v2 `STAGED` is deterministically selected.
   - When Document B has only v1 `ACTIVE`, v1 `ACTIVE` is retained.
   - Deactivated documents (`is_active = FALSE`) and quarantined/superseded versions are excluded.
2. **FAISS Vector ID 1:1 Parity:**
   - Verified via `test_faiss_vector_id_mapping_and_exact_match`:
   - Every `DocumentChunk` has a positive, unique `faiss_vector_id` allocated sequentially.
   - Vector IDs match `faiss.IndexIDMap2` entries 1:1.
   - Deserialization via `faiss.read_index()` preserves exact 64-bit vector IDs and dimensions (384).
3. **Candidate Isolation:**
   - Verified via `test_candidate_isolation_does_not_mutate_active_index`:
   - Candidate build writes exclusively to `storage/indices/candidate_<tag>/`.
   - `storage/indices/active_index.json` is never accessed, read, or modified.
4. **Empty-Collection Safety:**
   - Verified via `test_empty_document_collection_creates_safe_empty_index`:
   - Creates valid FAISS index with `ntotal == 0` and `d == 384`.
5. **Transactional Rollback on Failure:**
   - Verified via `test_failure_embedding_model_error_cleans_up_candidate` and `test_failure_missing_storage_file_raises_corrupt_error`:
   - Uncommitted `KnowledgeIndex` and `DocumentChunk` records are rolled back; candidate directory is quarantined.
6. **Live PostgreSQL Competing Session Execution:**
   - Ran live Python test against PostgreSQL 16 database (`ai_customer_support`):
   - Confirmed Session 1 acquires lock `888888`, Session 2 times out after 0.5s with `KnowledgeLockTimeoutError`, and Session 2 acquires lock immediately upon Session 1 release.

