# Unified Intelligent Customer Support AI Platform
## Enterprise Customer Support Monolith & Command Center

[![Acceptance Criteria](https://img.shields.io/badge/Acceptance%20Criteria-80%2F80%20PASS%20(100%25)-brightgreen?style=for-the-badge&logo=checkmarx)](#)
[![Backend Tests](https://img.shields.io/badge/Backend%20Tests-990%2F990%20Passing-brightgreen?style=for-the-badge&logo=pytest)](#)
[![Frontend Tests](https://img.shields.io/badge/Frontend%20Tests-34%2F34%20Passing-brightgreen?style=for-the-badge&logo=node.js)](#)
[![Python Version](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](#)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](#)
[![React](https://img.shields.io/badge/React-18.3%20%2B%20Vite-61DAFB?style=for-the-badge&logo=react&logoColor=black)](#)
[![Financial Cost](https://img.shields.io/badge/Lifetime%20Cost-%E2%82%B90.00%20Free%20Tier-success?style=for-the-badge)](#)

---

## 1. Project Overview

The **Unified Intelligent Customer Support AI Platform** is an enterprise-grade AI customer support operations platform engineered as the capstone project for the **ElevanceSkills AI/ML Internship**.

Enterprise customer support operations often suffer from fragmented tooling: separate chatbots, document search tools, ticketing desks, sentiment monitors, and multimodal parsers. This fragmentation leads to lost customer context, repetitive manual data re-entry, security vulnerabilities at integration seams, and critical SLA breaches.

This platform replaces fragmented prototypes with a single, highly performant **Clean Modular Monolith** that unifies all six core customer support AI capabilities into a production-grade system:
- **Task 1 — Automated Knowledge Base Update Pipeline:** Continuous SHA-256 change detection, quarantine management, immutable versioning, 25-query Golden Q&A benchmark regression gates, timezone-aware maintenance windows, atomic vector switchovers, 5-minute auto-rollback watchdogs, and persistent automatic retries at 15m, 30m, and 60m intervals.
- **Task 2 — Multimodal Evidence Processing:** Magic-byte validated intake for PNG, JPEG, and digital PDF files (up to 15MB/10 pages), PyMuPDF and receipt OCR extraction, structured entity extraction (Order IDs, amounts, currencies, dates), claim cross-verification, discrepancy calculations (`diff_amount`), `PENDING_CUSTOMER` clarification handling, zero-logging PII/PCI-DSS masking, adversarial visual injection defense, 30s background handoffs, customer completion notifications, and 30-day retention purges.
- **Task 3 — Intelligent Support Ticket Management:** Automated ticket synthesis from multi-turn chat, completeness evaluation across 8 categories, targeted non-repetitive clarification dialogs, explainable dynamic priority scoring ($1.0 - 5.0$), skill-based routing with agent capacity locking (`SELECT FOR UPDATE`), calendar-aware SLA timers excluding weekends/holidays, 75% SLA early warning webhooks, breach auto-escalations, semantic duplicate detection (cosine sim $\ge 0.85$), systemic issue clustering, masked staff handoff briefs, and runtime SLA recalculation.
- **Task 4 — Advanced Grounded RAG Assistant:** Multi-corpus document ingestion (manuals, policies, FAQs), three-layer metadata pre-filtering (access tier, product line, region, temporal validity), server-side identity authorization, default selection of latest active policies (`CURRENT_DATE`), historical policy auditing (`effective_as_of_date`), inline markdown source citations, low-similarity hallucination refusal ($\tau < 0.72$), independent claim-consistency verification, and indirect document prompt injection neutralization.
- **Task 5 — Multilingual Sentiment Analysis & Escalation:** 6-class sentiment classification (`positive`, `neutral`, `negative`, `frustrated`, `urgent`, `sarcastic`) with calibrated confidence scores, 5-turn sliding window trajectory slope analysis, pre-generation empathetic tone adaptation with strict policy invariance, repeated-negative and high-risk immediate escalations, after-hours urgent on-call webhook dispatching with delivery confirmation, next-business-day queueing, 15-minute unresolved negative conversation auto-escalation, and immutable audit logging.
- **Task 6 — Additional Language Support:** Native multi-language support (English, Spanish, French, German, Hindi), mixed-language code-switching detection, mid-stream language transitions, 128-bit entity tokenization & restoration (`EntityPreservationVault`), 10-turn conversational context memory, customer session boundary isolation, low-confidence clarification dialogs, Hinglish transliteration normalization, compound query decomposition (`QueryDecomposer`), deterministic mid-conversation slot correction (`SlotCorrectionEngine`), and 30-minute idle session expiry with 24-hour summary restoration.

---

## 2. Key Features & Architectural Capabilities

```
+--------------------------------------------------------------------------------------------------+
|                                    React 18 TypeScript Frontend                                  |
|         (Glassmorphism Command Center: Overview, Conversations, Evidence, Tickets, Ops)          |
+--------------------------------------------------------------------------------------------------+
                                                 │ HTTP / REST Polling
                                                 ▼
+--------------------------------------------------------------------------------------------------+
|                                      FastAPI Application Core                                    |
|       (Authentication, RBAC, Session Tokens, Request Tracing, RFC 7807 Exception Handlers)       |
+--------------------------------------------------------------------------------------------------+
   │                      │                       │                      │                     │
   ▼                      ▼                       ▼                      ▼                     ▼
[Task 1: KB Pipeline]  [Task 2: Evidence]    [Task 3: Ticketing]    [Task 4: Grounded RAG] [Task 5/6: NLP]
- Change Detection     - Magic-byte Intake   - Ticket Synthesis     - 3-Layer SQL Filter   - 6-Class Sentiment
- Ingestion Sanitizer  - PDF/Receipt OCR     - Completeness Matrix  - Dense FAISS Search   - Trajectory Slope
- Vector Staging       - Claim Corroborator  - Clarification Engine - Grounded Generator   - Tone Adaptation
- Golden QA Gates      - Conflict Detection  - Dynamic Priority     - Citation Verifier    - Multilingual (5)
- Ingestion Retries    - 30s Fast/Async Path - Capacity Locking     - Refusal Threshold    - Entity Vault
- Auto Rollback (5m)   - 30d Storage Purge   - Runtime SLA Recalc   - Injection Defense    - Multi-Intent / Slots
+--------------------------------------------------------------------------------------------------+
                                                 │
            ┌────────────────────────────────────┼────────────────────────────────────┐
            ▼                                    ▼                                    ▼
+───────────────────────+            +───────────────────────+            +───────────────────────+
|     PostgreSQL 16     |            |        Redis 7        |            |   FAISS Vector Core   |
| 28 Normalized Tables  |            | Session Hot History   |            | In-Memory Dense Index |
| 13 Reversible Alembic |            | Pub/Sub Index Reload  |            | Atomic Symlink Swap   |
| Row Locks SKIP LOCKED |            | Idempotency Keyring   |            | Rebuildable Cache     |
+───────────────────────+            +───────────────────────+            +───────────────────────+
```

### Architectural Highlights:
1. **Zero Hallucination Grounding:** Standard RAG queries enforce a three-layer security retriever in SQL before searching dense FAISS vector embeddings, mathematically guaranteeing that unauthorized or expired chunks never enter the LLM prompt context.
2. **Financial Verification Hierarchy:** Customer-uploaded evidence is classified as `DOCUMENT_CORROBORATED` only. Automated refunds are strictly barred (`is_refund_authorized: false`); authoritative payment gateway verification and supervisor approval are mandatory.
3. **Database Concurrency & Poison-Pill Fencing:** Background tasks utilize PostgreSQL row-level locks (`SELECT ... FOR UPDATE SKIP LOCKED`), transactional advisory locks (`pg_try_advisory_xact_lock`), and CAS heartbeats to serialize claims and reject stale worker commits.
4. **Zero-Downtime Knowledge Base Promotion:** Index promotions acquire global lock `5432002`, swap manifest symlinks in $< 500$ms, and synchronize reader processes over Redis Pub/Sub, dropping zero concurrent queries.

---

## 3. Technology Stack

| Domain | Technologies |
| :--- | :--- |
| **Backend Core** | Python 3.12, FastAPI 0.115+, Pydantic v2, Uvicorn, Starlette |
| **Persistence & ORM** | PostgreSQL 16, SQLAlchemy 2.0 (AsyncIO), asyncpg, Alembic 1.14 |
| **Caching & In-Memory** | Redis 7, redis-py 5.2 |
| **Vector Search** | FAISS (`faiss-cpu` 1.9), NumPy |
| **AI Inference** | Google Gemini API (`google-genai` 1.0+), Ollama (LLaMA 3 local fallback), HuggingFace MiniLM (`all-MiniLM-L6-v2`) |
| **Multimodal & OCR** | PyMuPDF (`fitz` 1.25), Pillow 11.0, Gemini Vision |
| **Security & Cryptography** | `cryptography` (AES-256-GCM), PyJWT, Luhn algorithm mod 10 |
| **Frontend UI** | React 18.3, TypeScript 5.5+, Vite 5.4, Vanilla CSS Design Token Architecture |
| **Testing** | Pytest 9.2, pytest-asyncio, Node Test Runner |

---

## 4. Repository Structure

```
AI-Customer-Support-Platform/
├── .env.example                               # Sanitized environment configuration template
├── .gitignore                                 # Git ignore protecting secrets, caches & builds
├── docker-compose.yml                         # Containerized multi-service deployment spec
├── pyproject.toml                             # Root configuration
├── README.md                                  # Primary project documentation
│
├── backend/                                   # FastAPI Backend Application
│   ├── pyproject.toml                         # Backend Python package manifest & dependencies
│   ├── alembic.ini                            # Alembic database migration configuration
│   ├── Dockerfile                             # Production backend container definition
│   ├── alembic/                               # Alembic Database Migrations (001 to 013)
│   │   ├── env.py                             # Migration execution environment
│   │   └── versions/                          # 13 Reversible schema migration scripts
│   ├── app/                                   # Application Source Code
│   │   ├── main.py                            # FastAPI lifespan, router mounting & middleware
│   │   ├── lifecycle.py                       # Background worker lifecycle manager
│   │   ├── worker.py                          # Dedicated background worker daemon
│   │   ├── api/v1/                            # REST API Endpoints (Auth, Chat, Evidence, Tickets, etc.)
│   │   ├── core/                              # Config, exceptions, PII masking, AES-256-GCM encryption
│   │   ├── db/                                # Async session engine & advisory lock context managers
│   │   ├── models/                            # SQLAlchemy 2.0 async domain entities (28 tables / 28 domain models)
│   │   ├── modules/                           # Six Decoupled Domain Packages
│   │   │   ├── knowledge/                     # Task 1: Change detection, sanitizer, chunker, promoter, retries
│   │   │   ├── evidence/                      # Task 2: Multimodal intake, OCR, reconciler, lifecycle purge
│   │   │   ├── ticketing/                     # Task 3: Synthesis, completeness, clarification, priority, routing
│   │   │   ├── rag/                           # Task 4: 3-layer retriever, grounded generator, citation verifier
│   │   │   ├── sentiment/                     # Task 5: 6-class analyzer, trajectory slope, on-call dispatcher
│   │   │   └── multilingual/                  # Task 6: Language detector, entity vault, query decomposer
│   │   └── services/                          # Storage abstraction, embeddings, LLM gateway
│   └── tests/                                 # 35 Pytest Test Modules (990 tests, 100% green)
│
├── frontend/                                  # React 18 TypeScript Frontend
│   ├── package.json                           # Frontend dependencies & scripts
│   ├── vite.config.ts                         # Vite bundler configuration
│   ├── tsconfig.json                          # TypeScript strict typechecking configuration
│   ├── Dockerfile                             # Multi-stage frontend container build
│   ├── nginx.conf                             # Nginx reverse proxy & security headers
│   ├── src/                                   # Frontend Source Code
│   │   ├── App.tsx                            # Root router & layout orchestration
│   │   ├── components/                        # Glassmorphism design token components
│   │   ├── pages/                             # Seven Core Command Center Views
│   │   ├── services/                          # API clients (Auth, Chat, Evidence, Tickets, Analytics)
│   │   └── context/                           # React context providers (AuthContext)
│   └── tests/                                 # 5 Native TypeScript Test Suites (34 tests, 100% green)
│
└── docs/                                      # Comprehensive Technical Documentation
    ├── architecture/                          # PRD, System Architecture, Database Design, Security
    ├── development-history/                   # Chronological phase implementation reports & audits
    └── submission/                            # ElevanceSkills Final Submission Package
        ├── FINAL_INTERNSHIP_PROJECT_REPORT.md # 17-section comprehensive capstone report
        ├── DEMO_SCRIPT.md                     # Evaluator demonstration plan for all 6 tasks
        ├── SCREENSHOT_CHECKLIST.md            # Visual evidence inventory & artifact mapping
        └── FINAL_SUBMISSION_CHECKLIST.md      # Final sign-off ledger & audit checklist
```

---

## 5. Prerequisites

Before installing the platform, ensure the following software is installed on your local system:
- **Python:** Version 3.12.x (`python --version`)
- **Node.js:** Version 20.x or 22.x LTS (`node --version`)
- **Package Manager:** `npm` (comes with Node.js)
- **Database:** PostgreSQL 16 (`psql --version`) running locally on port 5432
- **Cache / Broker:** Redis 7 (`redis-server --version`) running locally on port 6379
- **Build Tools:** Git, C++ build tools (required for compiling `faiss-cpu`)

---

## 6. Local Installation & Setup

### Step 1: Clone the Repository
```bash
git clone https://github.com/OldNick-hub/AI-Customer-Support-Platform.git
cd AI-Customer-Support-Platform
```

### Step 2: Set Up Backend Virtual Environment
```bash
# Create Python virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Upgrade pip and install backend package in editable mode
pip install --upgrade pip
pip install -e backend/
```

### Step 3: Install Frontend Dependencies
```bash
cd frontend
npm install
cd ..
```

---

## 7. Environment Variable Configuration

Copy the sanitized environment template:
```bash
cp .env.example .env
```

Edit `.env` using your local configuration. **Only safe placeholders are provided below:**

```ini
# Environment Mode
ENVIRONMENT=development
DEBUG=True
ENABLE_DEV_AUTH_ENDPOINTS=True

# Application Security (Use strong random strings in production)
SECRET_KEY=insecure-dev-secret-key-replace-in-production-minimum-32-chars
JWT_SECRET_KEY=insecure-dev-jwt-key-replace-in-production-minimum-32-chars
ENCRYPTION_MASTER_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

# PostgreSQL 16 Connection
POSTGRES_SERVER=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=ai_customer_support
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_postgres_password

# Redis 7 Connection
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=

# AI Provider Configuration
PRIMARY_LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
OLLAMA_BASE_URL=http://localhost:11434

# Worker Flags
ENABLE_BACKGROUND_WORKERS=True
KB_RETRY_WORKER_ENABLED=True
```

---

## 8. Database Migration Instructions

Initialize the database schema using Alembic:

> [!NOTE]
> `backend/alembic.ini` specifies `script_location = alembic` relative to `backend/`. Always change directory into `backend/` prior to invoking Alembic CLI commands.

```bash
# Ensure PostgreSQL is running and database 'ai_customer_support' exists:
# psql -U postgres -c "CREATE DATABASE ai_customer_support;"

# Navigate to backend directory:
cd backend

# Run all 13 migrations forward to head:
# Windows (PowerShell):
..\.venv\Scripts\alembic upgrade head
# Linux / macOS:
alembic upgrade head

# Verify current revision:
# Windows (PowerShell):
..\.venv\Scripts\alembic current
# Linux / macOS:
alembic current
# Expected: 013_kb_ingestion_retries (head)

# Return to repository root:
cd ..
```

To test migration reversibility:
```bash
cd backend

# Downgrade 1 revision:
# Windows (PowerShell):
..\.venv\Scripts\alembic downgrade -1
# Linux / macOS:
alembic downgrade -1

# Re-upgrade to head:
# Windows (PowerShell):
..\.venv\Scripts\alembic upgrade head
# Linux / macOS:
alembic upgrade head

cd ..
```

---

## 9. Starting the Application

### Option A: Local Native Processes (Recommended for Development)

**Terminal 1 — Backend API Server:**
```bash
# Windows:
.venv\Scripts\uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
# Linux / macOS:
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

**Terminal 2 — Frontend Development Server:**
```bash
cd frontend
npm run dev
# Running at http://localhost:5173
```

### Option B: Docker Compose (Full Stack)
```bash
docker compose up -d --build
# Frontend accessible at http://localhost:5173
# Backend accessible at http://localhost:8000
```

---

## 10. Running the Automated Test Suites

### 1. Run Complete Backend Test Suite (990 Tests)
```bash
# Run all 35 test files:
.venv\Scripts\pytest backend/tests

# Run with execution duration report:
.venv\Scripts\pytest backend/tests --durations=10
```
*Expected: **990 passed** in ~120s with 0 failures and 0 regressions.*

### 2. Run Live PostgreSQL Concurrency Tests
```bash
.venv\Scripts\pytest backend/tests/test_kb_retry_postgres.py backend/tests/test_postgres_integration.py
```
*Expected: **14 passed** verifying multi-connection `SKIP LOCKED` claims and advisory lock serialization.*

### 3. Run Frontend Test Suite (34 Tests)
```bash
npm --prefix frontend test -- --run
```
*Expected: **34 passed** across 5 suites in ~500ms.*

### 4. Verify Frontend Production Build
```bash
npm --prefix frontend run build
```
*Expected: `tsc && vite build` completes in ~1s with **0 compilation errors**.*

---

## 11. Interactive API Documentation

FastAPI automatically generates interactive OpenAPI documentation accessible in any browser:
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **OpenAPI JSON Contract:** `http://localhost:8000/api/v1/openapi.json`

---

## 12. Security Architecture & Data Protection

- **Tenant Isolation:** Enforced via `tenant_id` column indexes across all models and Layer 1 SQL pre-filtering.
- **Application-Layer Encryption:** Sensitive customer attachments and unmasked messages are encrypted at rest using AES-256-GCM (`ApplicationLayerEncryption`).
- **PII Scrubbing:** All customer queries, LLM prompts, and log outputs are filtered through `PIIMaskingEngine`. Payment cards (Luhn mod 10), SSNs, phone numbers, and emails are masked. Raw PANs and CVVs are never persisted or vectorized.
- **Adversarial Input Guard:** `AdversarialEvidenceGuard` neutralizes Unicode homoglyphs, strips zero-width spaces, decodes bounded Base64/Hex/URL payloads, and neutralizes model delimiter tokens.
- **Fail-Closed Dev Personas:** `/api/v1/auth/dev-personas` and `/api/v1/auth/dev-token` return HTTP 404 when `ENABLE_DEV_AUTH_ENDPOINTS=False` or `ENVIRONMENT=production`.

---

## 13. Known Limitations & Operational Boundaries

1. **Multimodal OCR in Offline Evaluation:**  
   Offline test suites utilize PyMuPDF for digital PDFs and rule-based regex parsing for standardized receipts. Processing arbitrary scanned camera photographs or handwritten receipts in production requires configuring a valid `GEMINI_API_KEY`.
2. **Production Cryptographic Secrets:**  
   The application requires cryptographically random secrets for `SECRET_KEY`, `ENCRYPTION_MASTER_KEY`, and database passwords when deployed to public networks.
3. **Outbound Notification Webhooks:**  
   Urgent after-hours on-call dispatching and 75% SLA warning notifications operate against mock HTTP dispatchers in test environments. Live delivery requires setting valid webhook URLs.
4. **Zero-Cost Storage Boundary:**  
   To maintain the ₹0.00 lifetime cost constraint, the platform operates on local disk storage and PostgreSQL bytea storage (`PostgresByteaStorageAdapter`). Cloud object storage adapters (AWS S3, Google Cloud Storage) are defined via `BaseStorageAdapter` but left unprovisioned.
5. **Client-Server Communication & Asynchronous Workers:**  
   Real-time UI state updates (evidence OCR processing status, claim reconciliation jobs, and ticket SLA recalculations) operate via HTTP REST polling (`GET /api/v1/evidence/{id}/status`, `GET /api/v1/evidence/reconcile/status/{job_id}`) and transactional outbox events (`chat_outbox_events`) rather than a dedicated persistent WebSocket server. Asynchronous workers (`KBIngestionRetryWorker`, `ReconciliationWorker`, `EvidenceRetentionWorker` invoking `StorageLifecycleManager.purge_expired_evidence()`, `EscalationWorker`, and `OutboxWorker`) run within the FastAPI application lifespan and can be enabled or disabled via environment settings (`ENABLE_BACKGROUND_WORKERS`, `KB_RETRY_WORKER_ENABLED`).

---

## 14. Submission & Evaluation Package Index

Comprehensive evaluation documents are archived under `docs/submission/`:
- **[Final Internship Project Report](docs/submission/FINAL_INTERNSHIP_PROJECT_REPORT.md):** 17-section capstone architecture, module design, database schemas, and outcomes.
- **[Demonstration Script](docs/submission/DEMO_SCRIPT.md):** Step-by-step evaluation scenarios for all six tasks with UI paths and curl commands.
- **[Screenshot & Visual Evidence Checklist](docs/submission/SCREENSHOT_CHECKLIST.md):** Catalogue of 20 required UI screens, components, and artifact images.
- **[Final Submission Checklist](docs/submission/FINAL_SUBMISSION_CHECKLIST.md):** Evaluator sign-off ledger verifying 80/80 passed criteria and system readiness.

---
*Developed for the ElevanceSkills AI/ML Internship Program. Verified 100% Passing & Ready for Evaluation.*
