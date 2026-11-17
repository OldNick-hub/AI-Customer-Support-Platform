# Controlled Local Deployment Rehearsal Report

**Project:** Intelligent Customer Support AI Platform  
**Repository:** `https://github.com/OldNick-hub/AI-Customer-Support-Platform`  
**Execution Date:** November 7, 2026  
**Rehearsal Status:** COMPLETED  
**Overall Verdict:** **READY WITH CONDITIONS**  

---

## 1. Executive Summary

A controlled local deployment rehearsal was conducted to validate that the Intelligent Customer Support AI Platform can build, start, migrate, execute functional customer and staff workflows, survive service interruptions, and pass full regression testing as a containerized stack.

The rehearsal utilized an isolated configuration (`.env.rehearsal`) targeting a disposable PostgreSQL database (`rehearsal_support_db`) and isolated host port mappings (`5433:5432`) to ensure absolute protection and zero interference with the host's existing PostgreSQL service running on port 5432.

### Final Verification Results
- **Container Build & Packaging:** 100% Success (Backend Python 3.12 Slim + Frontend Node 20 / Nginx Alpine)
- **Container Startup & Probes:** 100% Passing (`/api/v1/health/live` & `/api/v1/health/ready` returned 200 OK)
- **Database Schema Migrations:** 10/10 Alembic migrations applied cleanly to `head` (27 tables created)
- **Functional API Verification:** 10/10 Core journey suites passed (Dev Personas, RBAC Guards, Customer Chat, Ticket Synthesis, Priority/Routing, Resolution Actions, Evidence Intake & Reconciliation, Operational Intelligence)
- **Persistence & Recovery:** 100% Verified (PostgreSQL & Backend restarted; 3 tickets, 3 conversations, 2 evidence files survived; workers recovered with zero duplicates)
- **Frontend Regression Suite:** 34/34 tests passing (`npm test`), production build clean (`npm run build`)
- **Backend Regression Suite:** 833/833 tests passing (`pytest backend/tests/ -q`)
- **Teardown & Cleanup:** 100% Clean (`docker compose down -v` removed all containers, networks, and volumes)

---

## 2. Safety & Backup Verification

Prior to any build or container execution, complete system preservation was established and independently verified:
- **Workspace ZIP Archive:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_pre_rehearsal_workspace.zip`
  - Total Files: 349 files
  - Size: 1,640,219 bytes
  - Integrity Check: `zipfile.testzip()` verified 0 CRC/header errors.
- **Git Committed History Bundle:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_pre_rehearsal_git.bundle`
  - References: 4 refs (`main`, `HEAD`, tags)
  - Integrity Check: `git bundle verify` returned clean repository validation.
- **Git Working Tree Invariants:** Zero git staging (`git add`), committing, pushing, resetting, or stashing was performed. All untracked frontend and backend artifacts were preserved untouched.

---

## 3. Discovered Packaging & Deployment Findings (Fixed)

The rehearsal exposed four subtle containerization and environment differences that were proactively resolved in shared code without breaking changes:

1. **Host PostgreSQL Port Collision Prevention:**
   - *Issue:* Host machine had PostgreSQL actively bound to `127.0.0.1:5432` / `::5432`.
   - *Resolution:* Parameterized `docker-compose.yml` with `POSTGRES_EXTERNAL_PORT:-5432` and `REDIS_EXTERNAL_PORT:-6379`. Set `POSTGRES_EXTERNAL_PORT=5433` in `.env.rehearsal`. All rehearsal containers bound to `127.0.0.1:5433`, eliminating host port collision.

2. **Setuptools Flat-Layout Discovery Conflict:**
   - *Issue:* Both `app` and `alembic` exist at the root of `backend/`. Setuptools raised `Multiple top-level packages discovered in a flat-layout: ['app', 'alembic']` and required a `README.md`.
   - *Resolution:* Added `[tool.setuptools.packages.find] include = ["app*"]` to `backend/pyproject.toml` and created `backend/README.md`.

3. **Missing Runtime Dependencies in `pyproject.toml`:**
   - *Issue:* Container startup failed with `ModuleNotFoundError: No module named 'jwt'` in `backend/app/api/v1/auth.py` and `backend/app/api/deps.py`. In addition, `httpx` was listed under `[project.optional-dependencies] dev` instead of core runtime dependencies.
   - *Resolution:* Added `"pyjwt[crypto]>=2.8.0"` and `"httpx>=0.27.0"` to the core `dependencies` section of `backend/pyproject.toml`.

4. **Alembic `configparser` URL Interpolation:**
   - *Issue:* Passwords containing URL-encoded `%` (e.g. `%21`) caused Python's `configparser` in `alembic/env.py` to raise `ValueError: invalid interpolation syntax`.
   - *Resolution:* Applied standard Alembic escaping: `config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))` in `backend/alembic/env.py`.

5. **Timing Flake in Worker Background Loop Test:**
   - *Issue:* Under heavy Docker build and database I/O, `test_worker_continues_periodic_scanning_after_startup` had a fixed 60ms `await asyncio.sleep(0.06)`, which occasionally woke up before the 2nd scan iteration completed.
   - *Resolution:* Replaced fixed sleep with an adaptive polling loop (50 iterations x 10ms) checking `worker.scan_count >= 2`, making the test rock-solid on heavily loaded systems.

---

## 4. Database Migration Rehearsal (Alembic)

Executed inside the running backend container against disposable PostgreSQL database `rehearsal_support_db`:
```bash
docker compose -p acsp-rehearsal exec -T backend alembic upgrade head
```
All 10 revision migrations applied sequentially with transactional DDL:
- `001_initial_schema` — Core platform schema (users, customers, sessions, conversations, messages, documents)
- `002_chat_idempotency_and_outbox` — Deduplication records and outbox events
- `003_escalation_after_hours` — SLA calendars, escalation audits, and after-hours flags
- `004_evidence_intake_ocr` — Evidence files, SHA-256 hashes, and retention expiry
- `005_evidence_reconciliation` — Claim verifications and structured entity comparisons
- `006_multimodal_security_guard` — Quarantine flags and adversarial audit checks
- `007_evidence_purge_reliability` — Retention purge counters and bounded retry records
- `008_intelligent_ticket_synthesis` — Ticket syntheses, completeness scores, clarification requests
- `009_ticket_routing_priority` — Routing history, department skills, and workload tracking
- `010_ticket_resolution` — Resolution actions, playbook evaluations, and verification states

Verification query (`\dt`) confirmed **27 relational tables** generated and ready.

---

## 5. Functional Rehearsal Verification Suite

A 10-point end-to-end integration test was executed against `http://localhost:8000` (backend) and `http://localhost:5173` (Nginx frontend reverse proxy):

| Verification Suite | Target Endpoints | Result | Key Observations |
| :--- | :--- | :---: | :--- |
| **1. Health Probes** | `GET /api/v1/health/live`<br>`GET /api/v1/health/ready` | **PASS** | Live and ready probes returned HTTP 200 `{"status": "alive"}` and `{"status": "ready"}`. |
| **2. Authentication** | `GET /api/v1/auth/dev-personas`<br>`POST /api/v1/auth/dev-token`<br>`GET /api/v1/auth/me` | **PASS** | Successfully issued sessions for customer, VIP customer, agent, supervisor, and admin. Verified tokens via `/auth/me`. |
| **3. RBAC & Security Guards** | `GET /api/v1/tickets`<br>`POST /api/v1/tickets/.../priority/evaluate` | **PASS** | HTTP 401 on unauthenticated access; HTTP 403 when customer session attempted staff-restricted operations. |
| **4. Customer Chat & Idempotency** | `POST /api/v1/chat/message`<br>`GET /api/v1/conversations`<br>`GET /api/v1/conversations/{id}/messages` | **PASS** | Dialogue created, PII masked, UUID turn returned. Replay with identical idempotency key returned cached conversation without duplicates. |
| **5. Tenant Isolation** | `GET /api/v1/conversations/{id}/messages`<br>`GET /api/v1/tickets` | **PASS** | VIP customer denied access to Alice's conversation (HTTP 404) and saw 0 of Alice's tickets. |
| **6. Escalation & Ticket Generation** | `POST /api/v1/escalation/evaluate` | **PASS** | Triggered `URGENT_INQUIRY` escalation; dynamically synthesized Ticket `TCK-202611-...` with status `ESCALATED` and priority `URGENT`. |
| **7. Priority & Intelligent Routing** | `POST /api/v1/tickets/{id}/priority/evaluate`<br>`POST /api/v1/tickets/{id}/route` | **PASS** | Deterministic priority evaluation returned `MEDIUM` (score: 2.00) with explainable facts. Routing engine safely assigned team queue. |
| **8. Resolution Playbook & Actions** | `POST /api/v1/tickets/{id}/resolution/evaluate`<br>`POST /api/v1/tickets/{id}/resolution/actions/{id}/execute` | **PASS** | Resolution playbook evaluated and proposed action; executed action returned HTTP 200 OK with transactional row lock. |
| **9. Evidence Intake & Reconciliation** | `POST /api/v1/evidence/upload`<br>`POST /api/v1/evidence/reconcile` | **PASS** | Magic byte validation verified PNG signature; AES-256-GCM encrypted payload; reconciliation matched claim with 0.97 confidence score. |
| **10. Analytics & Operational Intel** | `GET /api/v1/analytics/overview`<br>`GET /api/v1/analytics/performance`<br>`GET /api/v1/analytics/operational-intelligence` | **PASS** | Overview KPIs, SLA time-series trends, and rule-based capacity warnings returned authoritative data. |
| **11. Nginx Reverse Proxy** | `http://localhost:5173/api/v1/*` | **PASS** | Nginx proxied API requests cleanly, appending security headers (`X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`). |

---

## 6. Persistence & Container Recovery

A crash recovery drill was simulated by restarting PostgreSQL and the Backend:
```bash
docker compose -p acsp-rehearsal restart backend postgres
```
- **Readiness Recovery:** Backend resumed readiness probe (`{"status": "ready"}`) within 4 seconds.
- **Data Persistence:** Database inspection verified 100% data retention across restart (3 tickets, 3 conversations, 2 evidence records intact).
- **Worker Concurrency Safety:** Backend background workers (`EscalationWorker`, `ReconciliationWorker`, `EvidenceRetentionWorker`) resumed polling using PostgreSQL `FOR UPDATE SKIP LOCKED`. Zero worker duplication or concurrency deadlocks detected.

---

## 7. Full Regression Suite Results

Following container recovery, full regression suites were executed across frontend and backend:
- **Frontend Test Suite:** `npm --prefix frontend test`
  - Suites: 5/5
  - Tests: **34/34 Passing** (0 failures, 0 skipped, 1255ms duration)
- **Frontend Production Build:** `npm --prefix frontend run build`
  - Build Tool: Vite v5.4.21 + TypeScript
  - Status: **Clean Build** (0 errors, 115 modules transformed, 1.11s duration)
- **Backend Test Suite:** `pytest backend/tests/ -q`
  - Total Tests: **833/833 Passing** (100% green pass rate)

---

## 8. Stack Teardown & Environment Verification

Upon verification completion, the rehearsal stack was cleanly decommissioned:
```bash
$env:ENV_FILE=".env.rehearsal"; docker compose -p acsp-rehearsal --env-file .env.rehearsal down -v
```
- All 4 containers (`acsp-frontend`, `acsp-backend`, `acsp-postgres`, `acsp-redis`) stopped and removed.
- Networks (`acsp-rehearsal_acsp-network`) removed.
- Disposable volumes (`postgres_data`, `redis_data`) purged.
- Host inspection confirmed zero lingering containers, zero orphan volumes, and ports 5433, 6379, 8000, 5173 freed.

---

## 9. Final Deployment Verdict & Conditions

### Verdict: **READY WITH CONDITIONS**

The current implementation is architecturally sound, container-ready, performant, and resilient under containerized execution.

### Mandatory Pre-Deployment Conditions for Live Production:
1. **Secrets Management:** Replace development placeholder secrets in `.env` with cryptographically secure, externally managed secrets (e.g. HashiCorp Vault or AWS Secrets Manager) for `SECRET_KEY`, `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, and `REDIS_PASSWORD`.
2. **LLM Provider API Keys:** Provide a valid `GEMINI_API_KEY` for live multimodal OCR and Gemini generative vision calls.
3. **Database Port Assignment:** In production, run PostgreSQL on its default container network port 5432 and avoid exposing database ports to public interfaces.
4. **SSL/TLS Termination:** Terminate TLS (HTTPS) at the ingress load balancer or Nginx reverse proxy using valid certificates (Let's Encrypt or corporate CA).

