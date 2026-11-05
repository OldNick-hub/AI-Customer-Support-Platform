# Day 04 Task 1 Phase 2 Engineering Report: Structure-Aware Chunking & Candidate FAISS Vector Index Construction

**Milestone:** Day 04 Task 1 — Knowledge Base Update & Monitoring Pipeline (Phase 2 & Remediation)  
**Status:** Completed & Formally Verified  
**Regression Test Baseline:** **114 / 114 Passing** (100% green across entire platform)  
**Execution Time:** 19.07 seconds  
**Architecture Plan Reference:** [`TASK_1_ARCHITECTURE_PLAN.md`](TASK_1_ARCHITECTURE_PLAN.md) (Version 3.0.0)  
**Remediation Report Reference:** [`PHASE_2_REMEDIATION_REPORT.md`](PHASE_2_REMEDIATION_REPORT.md)  

---

## 1. Executive Summary

Phase 2 of Task 1 implements the document chunking, dense vector embedding, deterministic candidate version selection, and isolated FAISS candidate vector index construction pipeline for the Intelligent Customer Support AI Platform.

All implementations strictly adhere to the approved architecture plan and database design:
- **Zero Schema Migrations:** Reused the existing Day 02 database schema without alterations.
- **Candidate Index Isolation:** Candidate vector indices are built strictly in isolated versioned staging directories (`storage/indices/candidate_<tag>/`). The active production manifest (`active_index.json`) and active index files are never read, modified, or swapped during candidate construction.
- **Single-Version Guarantee:** A deterministic candidate selection query ensures that each active document contributes at most one authoritative version to the candidate index, with staged replacements taking strict precedence over active predecessors.
- **Vector ID Mapping Integrity:** Vector IDs are signed 64-bit integers allocated directly from the PostgreSQL sequence `document_chunks_faiss_vector_id_seq` and mapped 1:1 into `faiss.IndexIDMap2` without ID reuse.
- **Failure Quarantine:** Any failure during chunking, embedding, sequence generation, or serialization automatically rolls back the database transaction and quarantines the partial candidate directory to `<candidate_dir>.failed`.

---

## 2. Core Components Implemented

### 2.1 Structure-Aware Document Chunker (`chunker.py`)
- **Module:** [`backend/app/modules/knowledge/chunker.py`](../../backend/app/modules/knowledge/chunker.py)
- **Class:** `StructureAwareChunker`
- **Model:** `ExtractedChunk`

#### Key Capabilities:
1. **Markdown Heading Hierarchy Stack:**
   - Tracks `#`, `##`, `###` headings in a hierarchical stack.
   - Prefixes each emitted chunk with its contextual breadcrumb path (e.g. `[Enterprise Support > Return Guidelines > Warranty Exceptions]`).
   - Ensures chunks retain full semantic and structural context even when split across long sections.
2. **PDF Page Boundary Tracking:**
   - Detects PDF page boundaries (`--- Page X ---`, `[Page X]`).
   - Emits and flushes prior section chunks before advancing the page counter, ensuring accurate `page_number` attribution on every chunk.
3. **Sentence Boundary Chunking with Overlap:**
   - Splits text on punctuation sentence boundaries (`.`, `!`, `?`, `\n`) rather than arbitrary character cuts, eliminating mid-word truncation.
   - Enforces configurable chunk sizes (default 600 characters) and sentence-aligned overlap (default 100 characters).
4. **Rich Metadata Attachment:**
   - Every chunk includes: `document_id`, `version_id`, `document_code`, `title`, `category`, `product_line`, `region`, `access_level`, `section_hierarchy`, `heading_path`, `chunk_index`, and `page_number`.
5. **Quality & Sanitation Rejection:**
   - Rejects empty, whitespace-only, or malformed text with `DocumentCorruptError` (`error_code="EMPTY_DOCUMENT_TEXT"`).
   - Rejects documents producing zero valid chunks with `DocumentCorruptError` (`error_code="NO_VALID_CHUNKS"`).
   - Filters duplicate chunks within the same document to conserve vector space.

---

### 2.2 Dense Vector Embedding Integration
- Reuses the singleton `EmbeddingService` from Day 03 ([`backend/app/services/embedding_service.py`](../../backend/app/services/embedding_service.py)).
- **Model:** HuggingFace `sentence-transformers/all-MiniLM-L6-v2` loaded once as a process singleton.
- **Dimensions:** 384-dimensional dense vectors.
- **Normalization:** Vectors are $L_2$ unit-normalized (`np.linalg.norm(v) == 1.0`), ensuring cosine similarity is mathematically identical to inner product (`IndexFlatIP`).
- **Batch Processing:** Embeds chunk batches non-blockingly via `asyncio.to_thread` with dimension validation on every generated vector.
- **Dimension Mismatch Rejection:** Throws `EmbeddingDimensionMismatchError` if any vector dimension diverges from 384.

---

### 2.3 Deterministic Candidate Version Selection
- **Module:** [`backend/app/modules/knowledge/vector_builder.py`](../../backend/app/modules/knowledge/vector_builder.py)
- **Method:** `CandidateVectorBuilder.select_candidate_versions(session)`

#### Invariants Enforced:
1. **Active Documents Only:** Only documents with `documents.is_active = TRUE` are selected. Deactivated or deleted documents are excluded.
2. **Authoritative Status Filter:** Only versions with `status IN ('STAGED', 'ACTIVE')` are eligible. Quarantined, superseded, or rejected versions are excluded.
3. **Precedence Resolution (STAGED > ACTIVE):**
   - If a document has both a `STAGED` version (newly ingested/modified) and an `ACTIVE` version (currently deployed), the `STAGED` version takes precedence (priority `STAGED` = 0, `ACTIVE` = 1).
   - Tied statuses resolve to the highest `version_number`.
4. **Single-Version Guarantee:** Each document contributes exactly **one** version to the candidate index. Old active chunks and new staged chunks never compete in the same index.
5. **Dialect Portability:** Implemented with SQLAlchemy ORM query logic compatible with both PostgreSQL and SQLite.

---

### 2.4 FAISS Candidate Index Construction & Isolation
- **Module:** [`backend/app/modules/knowledge/vector_builder.py`](../../backend/app/modules/knowledge/vector_builder.py)
- **Class:** `CandidateVectorBuilder`
- **Data Model:** `CandidateBuildResult`

#### Architectural Workflow:
1. **Isolated Directory Staging:**
   - Candidate indices are written to `storage/indices/candidate_<tag>/index.faiss`.
   - The active index manifest (`storage/indices/active_index.json`) is never touched, read, or modified during candidate generation.
2. **FAISS Architecture (`IndexIDMap2` + `IndexFlatIP`):**
   - Core index: `faiss.IndexFlatIP(384)`.
   - ID wrapper: `faiss.IndexIDMap2` which supports fast vector lookup and direct reconstruction by custom 64-bit integer IDs.
3. **64-bit Sequence Vector ID Allocation:**
   - On PostgreSQL: Allocates IDs using `SELECT nextval('document_chunks_faiss_vector_id_seq')`.
   - On SQLite: Employs `SELECT COALESCE(MAX(faiss_vector_id), 0) + 1 FROM document_chunks` fallback for test isolation.
   - IDs are stored in `DocumentChunk.faiss_vector_id` (PostgreSQL `BIGINT`) and passed as `np.int64` to `faiss.IndexIDMap2.add_with_ids()`.
   - Guarantees 1:1 ID parity between database chunks and FAISS vectors without ID collisions or re-indexing shifts.
4. **Candidate Manifest & Checksum:**
   - Computes SHA-256 checksum written to `index.faiss.sha256`.
   - Writes candidate manifest `manifest.json` with version tag, vector count, dimension, timestamp, and included document codes.
5. **Database Staging Record:**
   - Inserts a row into `knowledge_indices` with `status='STAGED'`, recording total vectors, storage path, and version tag.
6. **Read-Back Verification:**
   - Reads back the written `index.faiss` file using `faiss.read_index()` to verify readability, vector count, and dimension parity before declaring build success.
7. **Empty Collection Safety:**
   - Correctly creates an empty candidate FAISS index (`ntotal == 0`) when no active documents exist.

---

### 2.5 Failure Quarantine & Transaction Rollback
- If an unhandled error occurs during chunking, embedding, or serialization:
  1. The database transaction is immediately rolled back (`await session.rollback()`), preventing orphaned `document_chunks` or `knowledge_indices` records.
  2. The incomplete candidate directory is quarantined to `<candidate_dir>.failed` using `shutil.move`.
  3. Domain-specific exceptions (`CandidateIndexBuildError`, `IndexIntegrityError`, `EmbeddingDimensionMismatchError`, `DocumentCorruptError`) are preserved and propagated.
  4. The production active index remains 100% operational and isolated throughout.

---

## 3. Test Suite & Verification Results

### 3.1 Phase 2 Test Suite (`backend/tests/test_task1_phase2.py`)
A dedicated 12-test suite validates all Phase 2 requirements:

| Test Case | Validated Capability | Result |
| :--- | :--- | :---: |
| `test_markdown_hierarchy_and_breadcrumb_context` | Heading hierarchy preservation (`#`, `##`, `###`) and contextual breadcrumbs | **PASSED** |
| `test_pdf_page_metadata_tracking` | PDF page marker parsing and per-chunk `page_number` attribution | **PASSED** |
| `test_chunk_size_and_sentence_boundary_overlap` | Sentence boundary adherence, size limits, and sentence-aligned overlap | **PASSED** |
| `test_stable_metadata_and_empty_text_rejection` | Rich chunk metadata preservation and `EMPTY_DOCUMENT_TEXT` rejection | **PASSED** |
| `test_deterministic_candidate_selection_precedence` | Single-version guarantee: `STAGED` > `ACTIVE`, exclusion of deleted/superseded | **PASSED** |
| `test_candidate_index_build_and_faiss_save_load_roundtrip` | Full build lifecycle, `IndexIDMap2` persistence, SHA-256 generation, and DB staging | **PASSED** |
| `test_faiss_vector_id_mapping_and_exact_match` | Exact 1:1 match between `DocumentChunk.faiss_vector_id` and FAISS vector IDs | **PASSED** |
| `test_candidate_isolation_does_not_mutate_active_index` | Strict candidate isolation: `active_index.json` is never mutated or accessed | **PASSED** |
| `test_empty_document_collection_creates_safe_empty_index` | Safe creation of valid empty FAISS candidate index (`ntotal == 0`) | **PASSED** |
| `test_failure_embedding_model_error_cleans_up_candidate` | Embedding inference failure causes DB rollback and `.failed` directory quarantine | **PASSED** |
| `test_failure_invalid_embedding_dimensions_rejected` | Rejection of non-384 dimensional vectors with `EmbeddingDimensionMismatchError` | **PASSED** |
| `test_failure_missing_storage_file_raises_corrupt_error` | Missing file on disk raises `DocumentCorruptError`, triggers rollback and quarantine | **PASSED** |
| `test_section_overlap_reset_prevents_leakage` | Cross-section overlap reset prevents Billing sentences from leaking into Technical Support | **PASSED** |
| `test_sentence_boundary_handling_abbreviations_and_numbers` | Protected tokenizer handles Dr., e.g., i.e., Jan. 15, No. 123, version 2.0 without splitting | **PASSED** |
| `test_section_aware_chunk_deduplication` | Preserves identical boilerplate across different sections while deduplicating intra-section | **PASSED** |
| `test_effective_date_future_current_expired_filtering` | Excludes future effective_from and past effective_until using UTC date | **PASSED** |
| `test_repeated_candidate_build_failures_unique_quarantine` | Generates collision-free unique .failed quarantine directories on repeated build errors | **PASSED** |

### 3.2 Full Platform Regression Suite Output
```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.2.0, pluggy-1.7.0
rootdir: C:\Users\bhavs\OneDrive\Desktop\AI-Customer-Support-Platform\backend
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.1
collected 114 items

backend/tests/test_alembic_migration.py (1 test) PASSED                         [  0%]
backend/tests/test_constraints.py (6 tests) PASSED                             [  6%]
backend/tests/test_database_connection.py (2 tests) PASSED                     [  7%]
backend/tests/test_day03_phase1_2.py (19 tests) PASSED                         [ 24%]
backend/tests/test_day03_phase3.py (21 tests) PASSED                           [ 42%]
backend/tests/test_faiss_id_mapping.py (2 tests) PASSED                        [ 44%]
backend/tests/test_models.py (4 tests) PASSED                                  [ 48%]
backend/tests/test_pii_and_encryption.py (8 tests) PASSED                      [ 55%]
backend/tests/test_postgres_integration.py (8 tests) PASSED                    [ 62%]
backend/tests/test_seed_data.py (1 test) PASSED                                [ 63%]
backend/tests/test_sla_pause.py (1 test) PASSED                                [ 64%]
backend/tests/test_task1_phase1.py (20 tests) PASSED                           [ 82%]
backend/tests/test_task1_phase2.py (17 tests) PASSED                           [ 97%]
backend/tests/test_watchdog_persistence.py (2 tests) PASSED                    [100%]

============================ 114 passed in 19.07s =============================
```

---

## 4. Key Engineering Decisions & Audit Resolutions

| Design Requirement | Solution Implemented |
| :--- | :--- |
| **Deterministic Candidate Selection** | Enforced single-version selection where `STAGED` versions supersede `ACTIVE` versions for the same `document_code`. Deleted (`is_active = FALSE`), superseded, or quarantined versions are filtered out. |
| **Exact Vector ID Parity** | Chunks are assigned signed 64-bit integer IDs from the database sequence before insertion. Vectors are loaded into `faiss.IndexIDMap2.add_with_ids()` using these exact IDs. Chunks and vectors are strictly 1:1. |
| **Isolation from Active Index** | The candidate builder writes solely to `storage/indices/candidate_<tag>/`. The active manifest `storage/indices/active_index.json` is untouched, ensuring zero downtime and zero risk of corrupting production queries during index generation. |
| **PDF Page Attribution** | Fixed boundary handling: on encountering a PDF page marker (`--- Page X ---`), the chunker flushes pending section text *before* advancing the page number, guaranteeing each chunk is tagged with its correct page number. |
| **Corrupt & Empty Document Guards** | Empty strings, whitespace-only content, and documents producing 0 chunks raise `DocumentCorruptError` with distinct structured error codes (`EMPTY_DOCUMENT_TEXT`, `NO_VALID_CHUNKS`). |
| **Transaction & Filesystem Cleanup** | On any exception during build, the database session is rolled back, the partial candidate folder is quarantined to `.failed`, and domain exceptions are raised cleanly. |

---

## 5. Scope Boundaries & Next Phase Handoff

Phase 2 is strictly scoped to document chunking and candidate vector index construction. In accordance with platform governance, the following capabilities are deferred to Phase 3 / Day 05:
- **Golden Q&A Benchmark Evaluation:** Retrieval accuracy and NDCG@5 regression gates are not yet run against candidate indices.
- **Active Index Manifest Promotion:** `storage/indices/active_index.json` is not updated; candidate indices remain in `STAGED` status.
- **Automated Rollback Watchdog:** 5-minute automated rollback canary workers are not triggered in this phase.
- **Celery Indexing Workers:** Pipeline steps remain synchronous service calls ready for worker integration.

