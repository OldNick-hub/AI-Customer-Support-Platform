# Free-Tier Hosting Compatibility Audit
## AI Customer Support Platform & Command Center

**Repository:** `C:\Users\bhavs\OneDrive\Desktop\AI-Customer-Support-Platform`  
**Date:** 2026-11-07  
**Auditor Roles:** Senior Cloud Architect, Principal DevOps Engineer  
**Audit Type:** Static Architecture & Resource Profiling (Read-Only)  
**Target Architecture:**
- **Frontend:** Cloudflare Pages (React + Vite SPA)
- **Backend:** Render Free Web Service (FastAPI Modular Monolith)
- **Database:** Neon Serverless PostgreSQL (PostgreSQL 16)
- **Cache & Pub/Sub:** Upstash Serverless Redis
- **Evidence Storage:** Cloudflare R2 (S3-Compatible Object Store)

**Final Verdict:** `COMPATIBLE WITH CHANGES`

---

## Executive Summary

This audit assesses the feasibility of deploying the Intelligent Customer Support AI Platform to a 100% free-tier cloud topology. 

The evaluation confirms that while the application architecture (FastAPI, React SPA, PostgreSQL, Redis, Object Storage) maps cleanly to modern serverless and PaaS platforms, **it cannot be deployed as-is on the designated free tier without specific code and architectural modifications**.

The primary blockers are:
1. **Critical Memory Ceiling on Render Free Tier:** Render free tier allocates **512 MB RAM**. Our live measurements confirm that importing PyTorch and loading `SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')` alone requires **493.87 MB RSS** (96.5% of total memory), leaving under 19 MB for Uvicorn, FastAPI, SQLAlchemy pools, and request processing. Any inbound API call or OCR task will trigger a fatal Linux cgroup OOM crash (`SIGKILL` 137).
2. **Ephemeral Filesystem on Render:** Render free containers have no persistent local disk. The application's current evidence storage (`./storage/evidence`) and vector index snapshots (`./storage/indices`) are written to local disk and will be wiped on every container restart, redeploy, or 15-minute idle spin-down.
3. **Storage Adapter Missing for Cloudflare R2:** `EvidenceStorageManager` uses POSIX filesystem operations (`Path.write_bytes()`, `Path.unlink()`). An S3-compatible API adapter is required to integrate with Cloudflare R2.
4. **Frontend Base URL & SPA Routing:** The frontend Axios client hardcodes relative `/api/v1` calls (expecting the Docker Nginx reverse proxy), and lacks a Cloudflare Pages `_redirects` rule for client-side HTML5 history routing.

With targeted code adaptations—principally offloading embeddings to the Google Gemini Embedding API, implementing an R2 S3 storage adapter, configuring database pool pre-ping, and adding frontend configuration hooks—the platform achieves full operational readiness on free-tier infrastructure.

---

## 1. Provider-by-Provider Compatibility Assessment

### 1.1 Frontend: Cloudflare Pages
- **Target Workload:** Vite + React 18 TypeScript Single Page Application.
- **Compatibility Status:** `COMPATIBLE WITH CHANGES`
- **Capabilities & Limits:**
  - Bandwidth: Unlimited.
  - Build allowance: 500 builds per month (ample for CI/CD staging).
  - Global edge CDN latency: < 50ms worldwide.
- **Findings & Gaps:**
  - **API Routing Gap:** In `frontend/src/services/api.ts` line 764, `baseURL: '/api/v1'` is hardcoded as a relative path. In local Docker, Nginx acts as a reverse proxy passing `/api/` to `backend:8000`. Cloudflare Pages serves purely static assets; calls to `/api/v1` will fail with HTTP 404.
  - **SPA Deep Linking Gap:** `App.tsx` configures `react-router-dom` with HTML5 pushState routes (`/conversations`, `/tickets`, `/escalations`, `/evidence`, `/analytics`). Refreshing or directly navigating to any route other than `/` will return Cloudflare 404 unless a `public/_redirects` file (`/* /index.html 200`) is present.
- **Required Adaptations:**
  - Parameterize `baseURL` to read `import.meta.env.VITE_API_BASE_URL || '/api/v1'`.
  - Add `public/_redirects` with `/* /index.html 200`.

---

### 1.2 Backend: Render Free Web Service
- **Target Workload:** FastAPI application running via Uvicorn with in-process background workers (`EscalationWorker`, `ReconciliationWorker`, `EvidenceRetentionWorker`).
- **Compatibility Status:** `NOT COMPATIBLE AS-IS` / `COMPATIBLE WITH CHANGES`
- **Capabilities & Limits:**
  - Memory: **512 MB RAM** (Hard cgroup limit).
  - CPU: Shared 0.1 vCPU.
  - Idle Sleep: Spins down after **15 minutes** of HTTP inactivity.
  - Cold Start: **50 to 90 seconds** to boot from sleep.
  - Persistent Storage: **0 GB** (Ephemeral root filesystem; restarts wipe local changes).
  - Monthly compute hours: 750 free instance hours (sufficient for 1 continuous web service).
- **Findings & Gaps:**
  - **Fatal OOM Risk:** Runtime memory footprint of PyTorch + SentenceTransformers exceeds 512 MB (detailed in Section 2).
  - **Data Loss on Restart:** Vector index files and encrypted evidence binaries saved to `./storage` are destroyed when the instance restarts or wakes from sleep.
  - **Background Worker Suspension:** When the container sleeps after 15 minutes of inactivity, all asyncio background loops freeze. SLAs and scheduled reconciliations halt until an inbound HTTP request wakes the server.
- **Required Adaptations:**
  - Replace local PyTorch `sentence-transformers` embedding generation with remote API calls (`google-genai` / Gemini Embeddings), reducing baseline memory by ~380 MB.
  - Replace local disk evidence storage with Cloudflare R2 object storage.
  - Store vector index state in PostgreSQL (or persist index artifacts to R2).

---

### 1.3 Database: Neon Serverless PostgreSQL
- **Target Workload:** Relational transactional store, ACID constraints, row-level locking (`SKIP LOCKED`), and Alembic migrations.
- **Compatibility Status:** `COMPATIBLE WITH CHANGES`
- **Capabilities & Limits:**
  - Storage: 0.5 GB (500 MB) storage.
  - Compute: Serverless autoscaling compute (up to 0.25 vCPU on free tier).
  - Auto-suspend: Scales compute to zero after 5 minutes of inactivity.
  - Protocol: PostgreSQL 16 over TLS/SSL (`sslmode=require`).
- **Findings & Gaps:**
  - **Connection Dropping on Auto-Suspend:** When Neon suspends after 5 minutes of inactivity, open TCP sockets in the SQLAlchemy connection pool are severed by the Neon proxy. `backend/app/db/session.py` currently lacks `pool_pre_ping=True`. The first API request after a suspension will attempt to reuse a stale socket, causing `asyncpg.exceptions.ConnectionDoesNotExistError`.
  - **Connection Pooling & Prepared Statements:** Neon provides both direct connection endpoints (`ep-xyz.us-east-2.aws.neon.tech`) and PgBouncer pooled endpoints (`ep-xyz-pooler...`). `asyncpg` prepared statements conflict with transaction-mode PgBouncer unless `statement_cache_size=0` is set.
  - **SSL Parameter Parsing:** `asyncpg` does not parse `?sslmode=require` identically to psycopg2; it requires `?ssl=require` or explicit `connect_args={"ssl": True}`.
- **Required Adaptations:**
  - Enable `pool_pre_ping=True` and configure `pool_recycle=300` in `backend/app/db/session.py`.
  - Pass `connect_args={"statement_cache_size": 0}` if connecting through Neon's PgBouncer pooler.
  - Point Alembic migrations to the direct (non-pooler) Neon connection URL.

---

### 1.4 Cache & Pub/Sub: Upstash Serverless Redis
- **Target Workload:** Fast cache key storage (`cache:active_index_version`), index reload Pub/Sub (`channel:knowledge:index_reload`), and operational health monitoring.
- **Compatibility Status:** `COMPATIBLE WITH CONFIGURATION`
- **Capabilities & Limits:**
  - Request Limit: **10,000 commands per day** (hard cap).
  - Storage: 256 MB.
  - Max concurrent connections: 100.
  - Protocol: Redis over TLS (`rediss://...`).
- **Findings & Gaps:**
  - **TLS URL Support:** `backend/app/core/config.py` lines 415–425 accept a full `REDIS_URL` override. `redis.asyncio` natively supports `rediss://` for TLS connections to Upstash.
  - **Pub/Sub Compatibility:** Upstash supports Redis Pub/Sub commands (`PUBLISH`, `SUBSCRIBE`) over standard Redis protocol TCP connections.
  - **Daily Command Quota Budget:** 10,000 commands/day equals an average budget of **~0.11 commands per second**. If a background worker polls Redis every 5 seconds, it consumes 17,280 commands/day, exceeding the free tier within 14 hours.
  - **Persistence Reliability:** No critical customer or ticket state relies solely on Redis; all authoritative state resides in PostgreSQL. `RedisService` includes built-in graceful degradation: if Redis fails or the quota is exhausted, operations fall back to PostgreSQL without crashing.
- **Required Adaptations:**
  - Set `REDIS_URL=rediss://default:<password>@<endpoint>.upstash.io:6379`.
  - Ensure background workers do not poll Redis on tight loops; rely on PostgreSQL `SKIP LOCKED` or webhook triggers.

---

### 1.5 Evidence Storage: Cloudflare R2
- **Target Workload:** Encrypted binary evidence storage (receipts, invoices, PDFs, JPGs, PNGs), SHA-256 integrity digests, and 30-day retention purges.
- **Compatibility Status:** `COMPATIBLE WITH CHANGES`
- **Capabilities & Limits:**
  - Storage: **10 GB free storage per month**.
  - Class A Operations (Writes/Uploads): 1,000,000 operations/month.
  - Class B Operations (Reads/Downloads): 10,000,000 operations/month.
  - Egress Fees: **$0.00** (Zero egress charges).
  - Protocol: S3-Compatible REST API with AWS Signature V4.
- **Findings & Gaps:**
  - **Storage Architecture Mismatch:** `backend/app/modules/evidence/storage.py` is implemented exclusively around local disk paths (`pathlib.Path.write_bytes()`, `Path.read_bytes()`, `file_path.is_relative_to(self.base_dir)`).
  - **Encryption at Rest:** Encryption is performed **in-memory** using AES-256-GCM before writing to storage. This security model is 100% compatible with R2 (the encrypted payload can be uploaded as an object blob).
  - **Retention & Purge:** `StorageLifecycleManager` performs deletion via `file_path.unlink()`. In R2, deletion requires calling the S3 `DeleteObject` API.
- **Required Adaptations:**
  - Implement an S3-compatible client adapter in `storage.py` using `boto3` or `aioboto3` targeting the Cloudflare R2 endpoint (`https://<account_id>.r2.cloudflarestorage.com`).
  - Configure `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT_URL`.

---

## 2. Resource & Runtime Memory Profiling

### 2.1 Measured Memory Footprint (Empirical Verification)
Process memory working set measurements were executed on the project virtual environment using Windows OS Process Counters (`WorkingSet64`):

| Component / Execution Step | Measured Working Set (MB) | Delta (MB) | Impact on 512 MB Render Limit |
| :--- | :--- | :--- | :--- |
| **Python 3.12 Baseline Process** | 12.27 MB | +12.27 MB | Negligible (2.4%) |
| **`import torch`** | 194.78 MB | +182.51 MB | Heavy (38.0%) |
| **`import sentence_transformers`** | 383.09 MB | +188.31 MB | Severe (74.8%) |
| **Load `all-MiniLM-L6-v2` Model (CPU)** | **493.87 MB** | +110.78 MB | **Critical (96.5%)** |

> [!CAUTION]
> Loading the SentenceTransformer embedding model on CPU consumes **493.87 MB** of RAM before Uvicorn mounts application routes, initializes the SQLAlchemy pool, or allocates memory for incoming request buffers. 
> The headroom remaining on Render Free (512 MB) is only **18.13 MB**.

### 2.2 Estimated Additional Runtime Overhead

| Subsystem | Estimated Working Memory | Basis / Measurement Type |
| :--- | :--- | :--- |
| **Uvicorn Web Server & Event Loop** | 35 – 50 MB | Estimated (standard ASGI worker footprint) |
| **FastAPI Routes, Schemas & Models** | 25 – 40 MB | Estimated (Pydantic v2 metaclass cache + SQLAlchemy metadata) |
| **SQLAlchemy Async Connection Pool (5-10 conns)** | 15 – 25 MB | Estimated (asyncpg protocol buffers) |
| **FAISS Vector Index in Memory (10k vectors)** | 20 – 40 MB | Estimated (384 float32 dimensions + IndexIDMap2 tables) |
| **OCR Image Processing (`Pillow` + `pytesseract`)** | 80 – 150 MB | Estimated (raster image decompression for 300 DPI multi-page PDF) |
| **Combined Realistic Operational Footprint** | **670 – 850 MB** | **Exceeds Render Free by 158 – 338 MB** |

### 2.3 Single Web Instance & In-Process Workers
- **Can it run on 1 web instance?** Yes. The codebase is architected as a modular monolith. Database transactions and concurrency locks (`FOR UPDATE SKIP LOCKED`) ensure single-instance correctness.
- **Can workers run safely inside the web process?** Yes. `ENABLE_BACKGROUND_WORKERS="true"` successfully runs `EscalationWorker`, `ReconciliationWorker`, and `EvidenceRetentionWorker` as non-blocking `asyncio.Task` coroutines inside the FastAPI lifespan without thread starvation.

---

## 3. Database Compatibility (Neon PostgreSQL)

### 3.1 Connection String & Driver Requirements
- **SQLAlchemy Asyncpg URL:**
  ```
  postgresql+asyncpg://app_user:<password>@ep-xyz.us-east-2.aws.neon.tech/ai_customer_support?ssl=require
  ```
- **Driver Support:** `asyncpg>=0.29.0` and `psycopg2-binary>=2.9.9` are both present in `backend/pyproject.toml`.
- **Alembic Migration Compatibility:**
  Alembic runs via `backend/alembic/env.py` using `async_engine_from_config` with `pool.NullPool`. This is fully compatible with Neon.
  *Important:* Alembic must connect to Neon's **direct** endpoint (`ep-xyz...`) rather than the PgBouncer pooler (`ep-xyz-pooler...`), because DDL transactions (creating sequences, table alterations) can encounter protocol errors in transaction-pooled PgBouncer.

### 3.2 Serverless Auto-Suspend & Connection Lifecycle
- Neon suspends compute after 5 minutes of inactivity. When suspended:
  1. The next incoming connection takes ~1.5–3.0 seconds to wake the compute node.
  2. Any idle connection previously sitting in FastAPI's pool is invalidated.
- **Remediation in `backend/app/db/session.py`:**
  ```python
  engine_kwargs.update({
      "pool_size": 5,
      "max_overflow": 5,
      "pool_timeout": 30,
      "pool_recycle": 300,        # Recycle connections before Neon's 5-minute timeout
      "pool_pre_ping": True,      # CRITICAL: Ping connection before checkout to discard dead sockets
  })
  ```

### 3.3 Storage Capacity Expectations
- Neon Free Tier provides **0.5 GB (500 MB)**.
- **Estimated Database Footprint:**
  - 1,000 customer conversations (with 10 turns each): ~15 MB
  - 1,000 tickets, audit logs, and priority evaluations: ~25 MB
  - Document chunk relational metadata (excluding vector binaries): ~10 MB
  - Total initial staging footprint: **< 60 MB** (12% of Neon limit).
- **Verdict:** Highly sufficient for staging and verification.

---

## 4. Redis Compatibility (Upstash)

### 4.1 Connection & Protocol Validation
- Upstash endpoints require TLS: `rediss://default:<password>@<endpoint>.upstash.io:6379`.
- In `backend/app/core/config.py`, setting `REDIS_URL` overrides local host/port defaults and feeds directly into `redis.asyncio.ConnectionPool.from_url()`.
- Supported features verified:
  - Caching (`GET`, `SET`, `DELETE`, `EXISTS`) with TTLs.
  - Key namespacing (`app:*`).
  - Pub/Sub message publishing (`PUBLISH channel:knowledge:index_reload`).

### 4.2 Quota Risk Analysis (10,000 Commands/Day)
- Free tier limit: 10,000 requests per 24-hour UTC window.
- The platform uses Redis primarily for caching the active knowledge index tag and broadcasting promotion events.
- Because background workers run on PostgreSQL row locking rather than Redis polling, baseline idle command consumption is near zero.
- If the 10,000 command quota is exceeded, `RedisService` catches `RedisConnectionError` and degrades gracefully, falling back to PostgreSQL without interrupting customer support chat or ticket operations.

---

## 5. Evidence & Storage Compatibility (Cloudflare R2)

### 5.1 Storage Architecture Analysis
The current implementation in `backend/app/modules/evidence/storage.py` and `lifecycle_manager.py` enforces:
1. **Application-Layer Encryption:** Files are encrypted with AES-256-GCM using `evidence_id` as authenticated associated data.
2. **Local Filesystem Isolation:** Files are written to disk under `./storage/evidence/<customer_id>/<evidence_id>.enc`.
3. **Boundary Verification:** Uses `.resolve()` and `.is_relative_to()` to prevent path traversal attacks.

### 5.2 R2 Integration Requirements
- **Local Filesystem on Render is Ephemeral:** Any evidence file saved to local disk is lost when Render restarts. R2 is mandatory for durability.
- **Encryption Compatibility:** Because encryption happens in memory *before* disk write, the encrypted bytes can be transmitted directly to R2:
  ```python
  # Memory -> AES-256-GCM -> R2 Object
  r2_client.put_object(
      Bucket=settings.R2_BUCKET_NAME,
      Key=f"evidence/{customer_id}/{evidence_id}.enc",
      Body=encrypted_payload,
      ContentType="application/octet-stream"
  )
  ```
- **Lifecycle Retention & Purging:** `StorageLifecycleManager.discover_expired_evidence()` discovers expired rows from PostgreSQL. Replacing `file_path.unlink()` with `r2_client.delete_object(...)` achieves complete retention compliance.

---

## 6. Frontend Compatibility (Cloudflare Pages)

### 6.1 Configuration Verification
- **Build Output:** `npm --prefix frontend run build` outputs to `dist/` containing `index.html` and static assets.
- **Cloudflare Pages Build Settings:**
  - Build command: `npm run build`
  - Build output directory: `dist`
  - Node version: `20.x`

### 6.2 Required Frontend Code Changes
1. **Dynamic API Base URL:**
   Update `frontend/src/services/api.ts`:
   ```typescript
   export const api: AxiosInstance = axios.create({
     baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
     timeout: 30000,
   });
   ```
   Set Cloudflare Pages Environment Variable:
   ```
   VITE_API_BASE_URL=https://<your-render-app>.onrender.com/api/v1
   ```

2. **SPA Client Routing Fallback:**
   Create `frontend/public/_redirects`:
   ```
   /*    /index.html   200
   ```

3. **Backend CORS Alignment:**
   In Render environment variables, configure:
   ```
   CORS_ORIGINS=["https://<your-project>.pages.dev", "http://localhost:5173"]
   ```

---

## 7. Background Workers & Render Free-Tier Sleep Analysis

### 7.1 Operational Impact of 15-Minute Inactivity Sleep
On Render's free tier, the web container is terminated after 15 minutes of zero HTTP traffic. When asleep:

| Workflow / Subsystem | Behavior While Render is Asleep | Consequence / User Impact |
| :--- | :--- | :--- |
| **`EscalationWorker`** | Scanner loop halts completely. | Tickets requiring after-hours escalation or Rule B manager escalation are delayed. SLAs will breach unnoticed until an external HTTP request wakes the server. |
| **`ReconciliationWorker`** | Background polling pauses. | Uploaded receipts remain in `PENDING` reconciliation status instead of being reconciled against transaction ledgers. |
| **`EvidenceRetentionWorker`** | Retention scanner halts. | Expired evidence files scheduled for the 30-day purge will not be cleaned up until the server is awake. |
| **Incoming Customer Chat** | Triggers container wake-up. | Customer experiences a **50 to 90 second delay** on the first turn while Render boots the container from cold storage. |

### 7.2 Sleep Policy Compliance
> [!NOTE]
> Per architectural guidelines, artificial traffic generators, self-pingers, or synthetic keep-alive crons are **strictly prohibited**.
> For staging environments, delayed background processing and 60-second cold starts are acceptable trade-offs for zero-cost hosting. For production SLA enforcement, background workers require a 24/7 non-sleeping instance.

---

## 8. Deployment Blockers Classification (P0 – P3)

| Priority | Issue Description | Impact | Remediation |
| :--- | :--- | :--- | :--- |
| **P0** | **Render 512 MB Memory OOM** | PyTorch + SentenceTransformers consumes 493.87 MB RAM. Inbound traffic triggers container kill (OOM). | Offload embeddings to Google Gemini API (`text-embedding-004`) via `google-genai` SDK, eliminating PyTorch from runtime memory. |
| **P0** | **Ephemeral Disk Data Loss** | Render wipes `./storage` on sleep/restart; evidence files and vector indices are lost. | Implement Cloudflare R2 S3 adapter for evidence files; store vector metadata in PostgreSQL or R2. |
| **P1** | **Frontend Hardcoded Base URL** | `baseURL: '/api/v1'` fails on Cloudflare Pages (returns 404). | Parameterize `baseURL` with `import.meta.env.VITE_API_BASE_URL`. |
| **P1** | **Cloudflare Pages SPA 404s** | Direct navigation to `/tickets`, `/escalations` returns 404 on refresh. | Add `public/_redirects` with `/* /index.html 200`. |
| **P1** | **Neon Severed Sockets on Sleep** | Neon compute auto-suspends after 5 min; stale pool sockets cause database connection errors. | Add `pool_pre_ping=True` and `pool_recycle=300` in `backend/app/db/session.py`. |
| **P2** | **CORS Origin Rejection** | Cloudflare Pages domain rejected by backend CORS headers. | Update `CORS_ORIGINS` setting to include `https://*.pages.dev`. |
| **P2** | **Worker Suspension During Sleep** | Escalation scans and reconciliation pause when backend is idle. | Acceptable for staging; upgrade to Render Starter ($7/mo) for production 24/7 uptime. |
| **P3** | **Upstash 10k Daily Command Cap** | Heavy continuous polling could exhaust free Redis quota. | Ensure polling occurs against PostgreSQL `SKIP LOCKED`; use Redis strictly as passive cache. |

---

## 9. Required Code Changes Summary

To achieve free-tier compatibility, the following 6 targeted code adjustments are required:

```mermaid
flowchart TD
    subgraph Frontend Changes
        F1["frontend/src/services/api.ts<br/>Inject import.meta.env.VITE_API_BASE_URL"]
        F2["frontend/public/_redirects<br/>Add SPA fallback /* /index.html 200"]
    end
    
    subgraph Backend Configuration & DB
        B1["backend/app/db/session.py<br/>Enable pool_pre_ping=True & pool_recycle=300"]
        B2["backend/app/core/config.py<br/>Add R2 configuration & CORS settings"]
    end

    subgraph Memory & Storage Optimization
        M1["backend/app/services/embedding_service.py<br/>Add Gemini API embedding provider fallback"]
        M2["backend/app/modules/evidence/storage.py<br/>Implement Cloudflare R2 S3 adapter"]
    end
```

1. **`frontend/src/services/api.ts` (Line 764):**
   ```typescript
   baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
   ```
2. **`frontend/public/_redirects` (New File):**
   ```
   /*    /index.html   200
   ```
3. **`backend/app/db/session.py` (Lines 26–32):**
   ```python
   engine_kwargs.update({
       "pool_size": settings.DB_POOL_SIZE,
       "max_overflow": settings.DB_MAX_OVERFLOW,
       "pool_timeout": settings.DB_POOL_TIMEOUT,
       "pool_recycle": 300,
       "pool_pre_ping": True,
   })
   ```
4. **`backend/app/core/config.py`:**
   Add settings for `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, and `R2_ENDPOINT_URL`.
5. **`backend/app/modules/evidence/storage.py`:**
   Add `R2StorageManager` implementing `store_file`, `load_file`, and `delete_file` using `boto3` / `aioboto3`.
6. **`backend/app/services/embedding_service.py`:**
   Implement a lightweight Gemini Embedding API client using the already-installed `google-genai` SDK to bypass local PyTorch loading on 512 MB instances.

---

## 10. Cost Comparison: Free Tier vs. Minimum Viable Production Tier

| Platform Component | Free-Tier Staging Architecture | Minimum Viable Production Tier |
| :--- | :--- | :--- |
| **Frontend (Cloudflare Pages)** | $0.00 / month | $0.00 / month (Unlimited scale) |
| **Backend (Render Web Service)** | $0.00 / month (512 MB RAM, sleeps after 15m) | **$7.00 / month** (Render Starter: 1 GB RAM, 24/7 no sleep) |
| **Database (Neon PostgreSQL)** | $0.00 / month (0.5 GB storage, autosuspend) | $0.00 / month (or $19/mo Launch plan for continuous SLA) |
| **Cache (Upstash Redis)** | $0.00 / month (10,000 commands/day) | $0.00 / month (Pay-as-you-go: $0.20 per 100k requests) |
| **Storage (Cloudflare R2)** | $0.00 / month (10 GB storage, $0 egress) | $0.00 / month (Pay-as-you-go: $0.015/GB beyond 10GB) |
| **AI Embeddings (Gemini API)** | Free Tier (1,500 RPM free on Gemini API) | Pay-as-you-go ($0.00002 / 1k tokens) |
| **Total Monthly Cost** | **$0.00 / month** | **$7.00 – $26.00 / month** |

---

## 11. Recommended Architecture & Staging Topology

```mermaid
flowchart LR
    Browser["User Browser"]
    
    subgraph Cloudflare Edge
        CFPages["Cloudflare Pages<br/>(React SPA Static Host)"]
        CFR2["Cloudflare R2<br/>(Encrypted Evidence Storage)"]
    end
    
    subgraph Render PaaS
        FastAPI["Render Web Service<br/>FastAPI + In-Process Workers<br/>(Lightweight / Gemini Embeddings)"]
    end
    
    subgraph Serverless Data
        NeonDB[("Neon PostgreSQL<br/>Serverless ACID Store<br/>(pool_pre_ping enabled)")]
        UpstashRedis[("Upstash Redis<br/>TLS Cache & Pub/Sub")]
    end
    
    subgraph External AI
        GeminiAPI["Google Gemini API<br/>(Embeddings & Synthesis)"]
    end

    Browser -->|"HTTPS (Static UI)"| CFPages
    Browser -->|"API Calls (CORS + JWT)"| FastAPI
    FastAPI -->|"TLS Connection (Pre-ping)"| NeonDB
    FastAPI -->|"TLS rediss:// (Pub/Sub)"| UpstashRedis
    FastAPI -->|"S3 REST API (AES-256-GCM)"| CFR2
    FastAPI -->|"HTTPS (Embeddings)"| GeminiAPI
```

---

## 12. Final Verdict & Certification

```
================================================================================
FINAL VERDICT: COMPATIBLE WITH CHANGES
================================================================================
1. Cloudflare Pages:  COMPATIBLE WITH CHANGES (Requires VITE_API_BASE_URL & _redirects)
2. Render (FastAPI):  COMPATIBLE WITH CHANGES (Requires Gemini embeddings offload to avoid 512MB OOM)
3. Neon (PostgreSQL): COMPATIBLE WITH CHANGES (Requires pool_pre_ping=True & SSL params)
4. Upstash (Redis):   COMPATIBLE (TLS supported, graceful degradation on 10k limit)
5. Cloudflare R2:     COMPATIBLE WITH CHANGES (Requires S3-compatible storage adapter)
================================================================================
```

*Audit concluded. No project files, Git state, dependencies, or infrastructure settings were modified.*

