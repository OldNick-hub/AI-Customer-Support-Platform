# Phase 2.1 — Zero-Cost Storage Abstraction Report

**Project:** AI Customer Support Platform  
**Branch:** `main`  
**Execution Mode:** Controlled Implementation with Full Verification  
**Completed Date:** 2026-11-07  
**Verdict:** **COMPLETE**

---

## 1. Executive Summary

Phase 2.1 designs, implements, and verifies the **Zero-Cost Unified Storage Abstraction Layer** for the AI Customer Support Platform.

This phase strictly upholds the **₹0.00 lifetime operating cost invariant**:
1. **Local Filesystem Provider (`local`)**: Dedicated to local offline development and automated test suites (`ENVIRONMENT in ("development", "testing")`). Uses atomic temporary writes, sidecar `.meta.json` records, and directory containment checks.
2. **PostgreSQL BYTEA Provider (`postgres`)**: Dedicated to staging persistence (`ENVIRONMENT == "staging"`), utilizing Neon's free 512 MB PostgreSQL tier without requiring object storage subscriptions, credit cards, or external services.
3. **No External Object Stores**: Explicitly eliminates Cloudflare R2 (metered credit card requirement), Supabase Storage, AWS S3, and GCP Cloud Storage from staging architecture.
4. **Tenant Isolation & Security**: Every storage operation enforces tenant partitioning (`tenant_id`), canonical path traversal prevention (`.is_relative_to()`), null-byte stripping, and absolute path rejection.
5. **Payload Limits & Quotas**: Application-enforced 15 MB single-file limit (`STORAGE_MAX_FILE_SIZE_BYTES`) and 400 MB tenant quota limit (`STORAGE_MAX_QUOTA_BYTES`) protecting database boundaries.
6. **Integrity & Authenticated Encryption**: Cryptographic SHA-256 verification on all reads and AES-256-GCM encryption helpers binding `tenant_id:key` as authenticated associated data (AAD) for tamper detection.
7. **Production & Staging Lockdown**: Configuration validators strictly reject `STORAGE_PROVIDER="local"` in staging and production.

All **904 backend tests** and **34 frontend tests** pass with 100% green status, and the frontend production build succeeds with zero errors.

---

## 2. Backup Verification (Step 0)

Before any file modifications, complete workspace archives and Git bundle backups were generated outside the repository:

- **Backup Directory:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups`
- **Workspace ZIP Archive:** `ai_customer_support_pre_phase2_1_workspace.zip`
  - **Files Packed:** 369 project files (tracked, modified, and untracked)
  - **Archive Size:** 1.63 MB
  - **Integrity Verification:** Tested via `zipfile.testzip()`, returning `None` (0 corrupted files).
- **Git History Bundle:** `ai_customer_support_pre_phase2_1.bundle`
  - **Ref Count:** 4 refs (`refs/heads/main`, `refs/heads/backup/day-14-pre-day-15`, `refs/remotes/origin/main`, `HEAD`)
  - **Integrity Verification:** Tested via `git bundle verify`, verified cleanly.

---

## 3. Architectural Design Decisions

### 3.1 Elimination of Cloudflare R2
Cloudflare R2 was evaluated during Phase 2.0.1 research. While Cloudflare offers a generous free tier of 10 GB/month, creating an account requires a credit card and activates metered billing with no hard automated spend cap. Any unexpected spike, runaway script, or compromised token would incur automatic, non-refundable charges. Under the mandatory ₹0.00 hard budget, R2 presents an unacceptable financial risk and was eliminated.

### 3.2 Selection of PostgreSQL BYTEA (`LargeBinary`) for Staging
Neon provides a free serverless PostgreSQL database with 512 MB storage and 0.25 vCPU compute. It does **not** require a credit card and suspends compute when idle without billing.
- By storing staged evidence and knowledge artifacts as encrypted `BYTEA` records directly inside PostgreSQL, the application eliminates all external storage dependencies.
- To prevent database bloat, an application-layer quota of **400 MB** (`STORAGE_MAX_QUOTA_BYTES`) is strictly enforced before writes, reserving 112 MB for relational tables, indexes, and write-ahead logs.
- Memory consumption during metadata lookups is kept near-zero by explicitly excluding the `payload` column in `head_object` and `list_objects` queries.

---

## 4. Database Model & Migration

### 4.1 Database Model (`backend/app/models/storage.py`)
A dedicated model was created for binary object storage:

```python
class StorageObject(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "storage_objects"

    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, default="default", index=True)
    key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/octet-stream")
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)

    __table_args__ = (
        Index("uq_storage_objects_tenant_key", "tenant_id", "key", unique=True),
        Index("ix_storage_objects_tenant_size", "tenant_id", "file_size_bytes"),
    )
```

### 4.2 Alembic Migration (`backend/alembic/versions/011_storage_objects.py`)
- Revision ID: `011_storage_objects` (down revision: `010_ticket_resolution`).
- Creates table `storage_objects` with unique composite index `(tenant_id, key)` and composite index `(tenant_id, file_size_bytes)`.
- Fully reversible downgrade dropping indexes and table.
- Verified in `backend/tests/test_alembic_migration.py`.

---

## 5. Storage Abstraction Architecture

Implemented in [`backend/app/services/storage_service.py`](file:///C:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/services/storage_service.py):

### 5.1 Interface Contract (`BaseStorageAdapter`)
Defines the required asynchronous interface:
- `put_object(tenant_id, key, payload, content_type, metadata, session) -> StorageObjectMetadata`
- `get_object(tenant_id, key, session) -> Tuple[bytes, StorageObjectMetadata]`
- `head_object(tenant_id, key, session) -> StorageObjectMetadata`
- `delete_object(tenant_id, key, session) -> bool`
- `object_exists(tenant_id, key, session) -> bool`
- `list_objects(tenant_id, prefix, limit, session) -> List[StorageObjectMetadata]`
- `get_tenant_usage_bytes(tenant_id, session) -> int`

### 5.2 Local Storage Adapter (`LocalStorageAdapter`)
- **Directory Structure:** `{STORAGE_LOCAL_ROOT}/{tenant_id}/{key}`.
- **Atomic Persistence:** Writes to temporary file `{key}.tmp.{uuid}` in the target directory and replaces via `os.replace`.
- **Sidecar Metadata:** Persists `{key}.meta.json` with hash, size, content type, timestamps, and custom JSON metadata.
- **Path Traversal Defense:** Verifies `tenant_dir.is_relative_to(root)` and `target_path.is_relative_to(tenant_dir)`.
- **Integrity Check:** On `get_object`, computes SHA-256 of read bytes and asserts equality with sidecar metadata, raising `StorageIntegrityError` if tampered.

### 5.3 PostgreSQL BYTEA Adapter (`PostgresByteaStorageAdapter`)
- **Tenant Isolation:** All operations filter strictly by `StorageObject.tenant_id == safe_tenant`.
- **Low Memory Overhead:** `head_object()` and `list_objects()` project only metadata columns (`tenant_id`, `key`, `file_size_bytes`, `sha256_hash`, `content_type`, `metadata_json`, `created_at`, `updated_at`), avoiding loading large byte arrays into memory.
- **Transaction Session Management:** Accepts optional caller `session: AsyncSession`. If supplied, executes within the caller's transaction and calls `await session.flush()` without committing, enabling rollback support. If omitted, manages an independent session via `AsyncSessionLocal`.
- **Integrity Check:** On `get_object`, computes SHA-256 of binary payload and validates against stored `sha256_hash`.

### 5.4 Authenticated AES-256-GCM Encryption Helpers
- `put_encrypted_object(...)`: Encrypts raw bytes using `ApplicationLayerEncryption.encrypt_bytes(...)`. Binds `tenant_id:key` as authenticated associated data (AAD) and flags metadata with `{"encrypted": True, "encryption_algorithm": "AES-256-GCM", "assoc_data_binding": f"{tenant_id}:{key}"}`.
- `get_decrypted_object(...)`: Retrieves ciphertext and verifies tag using `tenant_id:key` associated data. If ciphertext has been tampered with or moved across tenants/keys, AES-GCM tag verification fails, raising `StorageIntegrityError`.

### 5.5 Facade & Singleton Management
- `StorageService`: Facade exposing all adapter methods and encryption helpers.
- `get_storage_service(provider: Optional[str] = None) -> StorageService`: Thread-safe singleton accessor.
- `reset_storage_service() -> None`: Clears singleton for fixture isolation in tests.

---

## 6. Configuration Safeguards & Exceptions

### 6.1 Configuration Parameters (`backend/app/core/config.py`)
- `STORAGE_PROVIDER: str = "local"` (validated against `("local", "postgres")`).
- `STORAGE_LOCAL_ROOT: str = "./storage/local"`.
- `STORAGE_MAX_FILE_SIZE_BYTES: int = 15 * 1024 * 1024` (15 MB).
- `STORAGE_MAX_QUOTA_BYTES: int = 400 * 1024 * 1024` (400 MB).
- Defaults: Automatically resolves to `postgres` in `staging` and `production`, and `local` in `development` and `testing`.

### 6.2 Lockdown Invariants
- `STORAGE_PROVIDER="local"` in `staging` or `production` raises `ValueError` at Settings initialization and is flagged by `validate_production_environment()`.
- Unsupported providers (`s3`, `r2`, `supabase`) are rejected immediately by `@field_validator("STORAGE_PROVIDER")`.

### 6.3 Standardized Exceptions (`backend/app/core/exceptions.py`)
- `StorageError` (500, `STORAGE_ERROR`)
- `StorageSecurityError` (403, `STORAGE_SECURITY_VIOLATION`)
- `StorageNotFoundError` (404, `STORAGE_OBJECT_NOT_FOUND`)
- `StoragePayloadTooLargeError` (413, `STORAGE_PAYLOAD_TOO_LARGE`)
- `StorageQuotaExceededError` (507, `STORAGE_QUOTA_EXCEEDED`)
- `StorageIntegrityError` (500, `STORAGE_INTEGRITY_MISMATCH`)
- `StorageConfigurationError` (500, `STORAGE_CONFIGURATION_ERROR`)

---

## 7. Verification Results

### 7.1 Dedicated Storage Abstraction Tests
Executed: `.venv\Scripts\pytest backend/tests/test_storage_abstraction.py -v`  
**Result:** **24 passed in 3.56s**

- `TestLocalStorageAdapter`:
  - `test_put_and_get_object_success` PASSED
  - `test_head_object_and_not_found` PASSED
  - `test_idempotent_delete_and_exists` PASSED
  - `test_list_objects_and_prefix_filtering` PASSED
  - `test_tenant_isolation` PASSED
  - `test_path_traversal_defense` PASSED
  - `test_single_file_size_limit` PASSED
  - `test_tenant_quota_limit` PASSED
  - `test_tamper_detection_checksum_mismatch` PASSED
- `TestPostgresByteaStorageAdapter`:
  - `test_put_and_get_object_success` PASSED
  - `test_head_object_and_not_found` PASSED
  - `test_idempotent_delete_and_exists` PASSED
  - `test_overwrite_object_updates_record` PASSED
  - `test_tenant_isolation` PASSED
  - `test_caller_transaction_rollback` PASSED
  - `test_quota_and_size_limits` PASSED
  - `test_tamper_detection_checksum_mismatch` PASSED
- `TestStorageEncryption`:
  - `test_put_and_get_encrypted_object_roundtrip` PASSED
  - `test_tamper_detection_on_ciphertext` PASSED
  - `test_tenant_associated_data_binding` PASSED
- `TestStorageServiceFacade`:
  - `test_storage_service_instantiation_defaults` PASSED
  - `test_storage_service_postgres_provider` PASSED
  - `test_storage_service_unsupported_provider_raises` PASSED
  - `test_singleton_get_and_reset` PASSED

### 7.2 Production Configuration Lockdown Tests
Executed: `.venv\Scripts\pytest backend/tests/test_production_configuration.py -v`  
**Result:** **38 passed in 8.35s** (including 6 new tests in `TestStorageConfigurationSafeguards`).

### 7.3 Alembic Migration Verification
Executed: `.venv\Scripts\pytest backend/tests/test_alembic_migration.py -v`  
**Result:** **1 passed in 0.70s** (verifying `011_storage_objects` DDL generation).

### 7.4 Full Platform Regression Test Suite
Executed: `.venv\Scripts\pytest backend/tests`  
**Result:** **904 passed, 10 warnings in 100.94s (0 regressions)**

### 7.5 Frontend Tests & Production Build
Executed: `npm test -- --run` and `npm run build` in `frontend/`  
**Result:** **34 passed in 1.17s; production build compiled in 2.31s with zero errors**.

---

## 8. Preserved Boundaries & Deferred Scope

In strict accordance with Phase 2.1 specifications:
1. **No Migration of Existing Evidence Files**: Existing evidence records and disk storage remain untouched.
2. **Evidence Service Integration Deferred**: `EvidenceStorageManager` and `EvidenceService` in `backend/app/modules/evidence/` continue using their existing paths until Phase 2.3.
3. **No FAISS Index Migration**: FAISS index persistence and loading remain unmodified.
4. **Git Workspace Preserved**: Zero `git add`, `git commit`, `git push`, `git reset`, `git clean`, or `git stash` operations performed.

---

## 9. Conclusion

Phase 2.1 is complete. The application now possesses a clean, zero-cost, tenant-isolated storage abstraction layer with local filesystem and PostgreSQL BYTEA adapters, robust configuration safeguards, authenticated encryption, and 100% test pass rate across 904 backend and 34 frontend tests.

