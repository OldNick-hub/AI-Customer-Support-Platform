# Intelligent Customer Support AI Platform
## Unified Enterprise Support Platform (16-Day Engineering Roadmap)

[![Architecture Status](https://img.shields.io/badge/Architecture-Approved%20Day%2001-brightgreen)](#)
[![Database Status](https://img.shields.io/badge/Database-Approved%20Day%2002-brightgreen)](#)
[![Foundational Services](https://img.shields.io/badge/Services-Approved%20Day%2003-brightgreen)](#)
[![Test Suite](https://img.shields.io/badge/Tests-420%2F420%20Passing-brightgreen)](#)
[![Python Version](https://img.shields.io/badge/Python-3.12-blue)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688)](#)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)](#)
[![React](https://img.shields.io/badge/Frontend-React%2018%20%2B%20Vite-61dafb)](#)
[![AI Stack](https://img.shields.io/badge/AI-Gemini%20%2B%20FAISS%20%2B%20Ollama-orange)](#)

---

## 1. Project Overview

The **Intelligent Customer Support AI Platform** is an enterprise-grade customer support platform built from scratch. Rather than six disconnected scripts or isolated prototypes, this platform unifies six core customer support AI capabilities into a single, cohesive modular monolith:

1. **Automated Knowledge Base Update Pipeline (`TASK 1`):** Change detection (SHA-256), quarantine of invalid docs, version lifecycle, Golden Q&A benchmark regression gates, atomic vector switchovers, and 5-minute automated rollback watchdogs.
2. **Multimodal Evidence Analyzer (`TASK 2`):** Extracts order numbers, transaction dates, amounts, and error codes from screenshots, PDFs, and invoices; cross-verifies claims against evidence; detects visual prompt injections; enforces 30-second async handoff and 30-day file retention.
3. **Intelligent Support Ticket Management (`TASK 3`):** Converts unresolved dialogs into structured tickets with completeness checks, dynamic priority matrix calculation, calendar-aware SLA timers (excluding weekends/holidays), 75% SLA warning webhooks, and masked handoff summaries.
4. **Advanced RAG Knowledge Assistant (`TASK 4`):** Granular metadata filtering (access tier, product line, region), temporal policy relevance (latest active vs historical auditing), grounded inline source citations, hallucination refusal threshold ($\tau < 0.72$), and indirect prompt injection defense.
5. **Sentiment Analysis & Escalation Engine (`TASK 5`):** 6-class sentiment classification (`positive`, `neutral`, `negative`, `frustrated`, `urgent`, `sarcastic`), multi-turn trajectory slope analysis, adaptive response tone modulation, and 15-minute unresolved negative conversation auto-escalation.
6. **Multilingual Conversation Engine (`TASK 6`):** Native multi-language support (English + 3 configurable languages), mixed-language code-switching, named entity preservation across translations, 10+ turn conversational memory, and 30-min idle expiry with 24-hour summary restoration.

---

## 2. Architecture Documentation Index

Comprehensive engineering documents are maintained under [`docs/architecture/`](docs/architecture/):

| Document | Description |
| :--- | :--- |
| **[PRODUCT_REQUIREMENTS.md](docs/architecture/PRODUCT_REQUIREMENTS.md)** | Unified PRD, detailed functional requirements (FR), non-functional requirements (NFR), and the complete 41-requirement Requirements Traceability Matrix (RTM). |
| **[SYSTEM_ARCHITECTURE.md](docs/architecture/SYSTEM_ARCHITECTURE.md)** | System overview, tier separation, synchronous vs asynchronous workflows, Mermaid architecture diagram, Blue/Green vector lifecycle, and session management. |
| **[DATABASE_DESIGN.md](docs/architecture/DATABASE_DESIGN.md)** | PostgreSQL 16 schema, Mermaid ER diagram, data dictionaries, indexing strategies, and Alembic zero-downtime migration protocols. |
| **[API_SPECIFICATION.md](docs/architecture/API_SPECIFICATION.md)** | RESTful and WebSocket API endpoints, OAuth2/JWT auth, Pydantic schemas, and RFC 7807 problem details error format. |
| **[MODULE_DEPENDENCIES.md](docs/architecture/MODULE_DEPENDENCIES.md)** | Six-module Directed Acyclic Graph (DAG) diagram, interservice Pydantic contracts, facade orchestrator, and circular dependency rules. |
| **[SECURITY_ARCHITECTURE.md](docs/architecture/SECURITY_ARCHITECTURE.md)** | Threat model, direct/indirect/visual prompt injection defenses, RBAC matrix, automated PII scrubbing (PCI-DSS), safe file quarantine, and secrets rules. |
| **[TESTING_STRATEGY.md](docs/architecture/TESTING_STRATEGY.md)** | Test pyramid, module-by-module test suite mapping for all 41 requirements, fixtures, and CI coverage gates ($\ge 85\%$). |
| **[TECHNOLOGY_DECISIONS.md](docs/architecture/TECHNOLOGY_DECISIONS.md)** | Architectural Decision Records (ADRs) justifying FastAPI, PostgreSQL, FAISS, Gemini+Ollama, Celery+Redis, and React+Vite. |
| **[PROJECT_STRUCTURE.md](docs/architecture/PROJECT_STRUCTURE.md)** | Full repository layout, modular package hierarchy, naming conventions, and coding standards. |
| **[Development History Index](docs/development-history/README.md)** | Chronological index of all 33 historical implementation reports, benchmark gates, security audits, and verification sign-offs across Days 01–08. |
| **[ARCHITECTURE_AUDIT_RESOLUTION.md](docs/development-history/ARCHITECTURE_AUDIT_RESOLUTION.md)** | Comprehensive Day 01 architecture audit resolution report addressing all 10 consistency, feasibility, and security findings. |
| **[FINAL_VERIFICATION.md](docs/development-history/FINAL_VERIFICATION.md)** | Final Day 02 database and persistence integrity verification report covering all 20 PostgreSQL tables, FAISS BIGINT sequences, and test suite. |
| **[PHASE_1_2_REPORT.md](docs/development-history/PHASE_1_2_REPORT.md)** | Day 03 Phase 1 & 2 engineering report covering centralized configuration, standardized exceptions, and PII masking service. |
| **[PHASE_3_REPORT.md](docs/development-history/PHASE_3_REPORT.md)** | Day 03 Phase 3 engineering report covering Redis async service, MiniLM embeddings, and multi-provider LLM gateway. |
| **[FINAL_AUDIT.md](docs/development-history/FINAL_AUDIT.md)** | Final Day 03 integration audit report verifying settings consistency, live integrations, embedding singletons, fallback boundaries, and full test suite. |
| **[TASK_1_ARCHITECTURE_PLAN.md](docs/development-history/TASK_1_ARCHITECTURE_PLAN.md)** | Comprehensive Day 04 Task 1 Architecture Plan (v3.0.0) covering change detection, 7-state index lifecycle, atomic manifest switchover, durable watchdog, and zero-migration tombstone protocol. |
| **[PHASE_1_REPORT.md](docs/development-history/PHASE_1_REPORT.md)** | Day 04 Task 1 Phase 1 engineering report covering document change detection, file sanitization, prompt injection defense, and quarantine subsystem. |
| **[PHASE_1_FINAL_AUDIT.md](docs/development-history/PHASE_1_FINAL_AUDIT.md)** | Final Day 04 Phase 1 engineering audit report verifying PostgreSQL advisory locking, scope preservation, TOCTOU defenses, and 95-test suite. |
| **[PHASE_2_REPORT.md](docs/development-history/PHASE_2_REPORT.md)** | Day 04 Task 1 Phase 2 engineering report covering structure-aware chunking, dense embeddings, deterministic candidate selection, and isolated FAISS candidate vector building. |
| **[PHASE_2_INDEPENDENT_AUDIT.md](docs/development-history/PHASE_2_INDEPENDENT_AUDIT.md)** | Independent code-level engineering audit report identifying eight high/medium/low findings across chunker overlap, advisory locks, quarantine checksums, and date boundaries. |
| **[PHASE_2_REMEDIATION_REPORT.md](docs/development-history/PHASE_2_REMEDIATION_REPORT.md)** | Controlled engineering report verifying the complete remediation of all eight audit findings with 114/114 passing tests. |
| **[PHASE_2_FINAL_ACCEPTANCE_AUDIT.md](docs/development-history/PHASE_2_FINAL_ACCEPTANCE_AUDIT.md)** | Final code-level acceptance audit report inspecting PostgreSQL advisory locks, cross-volume quarantine atomicity, and test matrix. |
| **[TASK_4_ARCHITECTURE_PLAN.md](docs/development-history/TASK_4_ARCHITECTURE_PLAN.md)** | Comprehensive Day 06 Task 4 Architecture Plan (v2.1) covering 3-layer security retriever, claim verification, sanitizer vault, and RAG facade. |
| **[PHASE_4C_IMPLEMENTATION_REPORT.md](docs/development-history/PHASE_4C_IMPLEMENTATION_REPORT.md)** | Day 06 Task 4 Phase 4C implementation report covering RAG orchestration, Sanitizer Vault, entity preservation, and initial verification. |
| **[PHASE_4C_FINAL_SECURITY_AUDIT.md](docs/development-history/PHASE_4C_FINAL_SECURITY_AUDIT.md)** | Final code-level security audit report evaluating server-side identity precedence, zero-width injection defenses, Base64/URL decoding, 50-client isolation, and 270/270 full platform regression. |
| **[PHASE_4C_FINAL_SIGNOFF.md](docs/development-history/PHASE_4C_FINAL_SIGNOFF.md)** | Final verification signoff report documenting Check 1 (fail-closed identity resolution across 7 tests), Check 2 (shared-orchestrator 50-customer multi-tenant isolation), and 277/277 full platform regression. |
| **[PHASE_4D_IMPLEMENTATION_REPORT.md](docs/development-history/PHASE_4D_IMPLEMENTATION_REPORT.md)** | Day 06 Task 4 Phase 4D implementation report covering the canonical REST chat endpoint (`POST /api/v1/chat/message`), server-side auth (X-Session-Token & JWT), zero client trust, refusal mapping, exception shielding, and 306/306 full platform regression. |
| **[TASK_6_IMPLEMENTATION_PLAN.md](docs/development-history/TASK_6_IMPLEMENTATION_PLAN.md)** | Comprehensive Day 07 Task 6 Architecture Plan (v3.1.0) covering language detection, 128-bit entity preservation, bidirectional neural translation, Redis isolation, session retention, and Zero Silent English. |
| **[PHASE_1_IMPLEMENTATION_REPORT.md](docs/development-history/PHASE_1_IMPLEMENTATION_REPORT.md)** | Day 07 Task 6 Phase 1 engineering report covering PreTranslationSecurityScanner, 128-bit EntityPreservationVault, and multi-tier LanguageDetector with 40/40 passing tests. |
| **[PHASE_2_IMPLEMENTATION_REPORT.md](docs/development-history/PHASE_2_IMPLEMENTATION_REPORT.md)** | Day 07 Task 6 Phase 2 engineering report covering bidirectional neural translation, pre-translation PII masking, Redis template caching, Redis outage circuit breaker, normalized placeholder injection defenses, and Zero Silent English failure protocols with 32/32 passing tests. |
| **[PHASE_3_IMPLEMENTATION_REPORT.md](docs/development-history/PHASE_3_IMPLEMENTATION_REPORT.md)** | Day 07 Task 6 Phase 3 engineering report covering conversation lifecycle management, 30-minute idle closure, 24-hour summary restoration, immutable authentication boundaries, Redis hot history buffer, and permanent archival with 23/23 passing tests. |


| **[PHASE_3A_BENCHMARK_REPORT.md](docs/development-history/PHASE_3A_BENCHMARK_REPORT.md)** | Day 05 Task 1 Phase 3A engineering report covering the 25-query Golden Q&A benchmark suite, two-stage grounding scores, category floors, zero-tolerance regressions, and quality gates with 123/123 passing tests. |
| **[PHASE_3A_FINAL_AUDIT.md](docs/development-history/PHASE_3A_FINAL_AUDIT.md)** | Final code-level audit report evaluating Golden dataset authenticity, real retrieval against FAISS, LLM judge execution, quality gate thresholds, and database persistence. |
| **[PHASE_3B_MAINTENANCE_REPORT.md](docs/development-history/PHASE_3B_MAINTENANCE_REPORT.md)** | Day 05 Task 1 Phase 3B engineering report covering timezone-aware maintenance windows, DST offsets, blackout dates, boundary precision, next-window calculations, and emergency overrides with 138/138 passing tests. |
| **[PHASE_3C_PROMOTION_REPORT.md](docs/development-history/PHASE_3C_PROMOTION_REPORT.md)** | Day 05 Task 1 Phase 3C engineering report covering atomic candidate vector index promotion, manifest pointer swapping, multi-process advisory locks, RBAC overrides, and crash recovery with 154/154 passing tests. |
| **[PHASE_3C_FINAL_AUDIT.md](docs/development-history/PHASE_3C_FINAL_AUDIT.md)** | Final code-level audit report evaluating live PostgreSQL 16 concurrency, crash consistency, Redis failure tolerance, and 4-tier reader cache synchronization with 158/158 passing tests. |
| **[PHASE_3D_FINAL_AUDIT.md](docs/development-history/PHASE_3D_FINAL_AUDIT.md)** | Final code-level runtime integration audit report verifying canary health monitoring, automated rollback triggers, atomic manifest reversion, distributed scheduler locking, and worker lifecycle recovery with 182/182 passing tests. |
| **[MASTER_WORK_LOG.md](MASTER_WORK_LOG.md)** | Authoritative master engineering work log detailing completed milestones, architectural decisions, bug remediations, and testing logs. |

---

## 3. Technology Stack

- **Frontend:** React 18, TypeScript, Vite, Vanilla CSS Design Token Architecture.
- **Backend:** Python 3.12, FastAPI, Pydantic v2, Uvicorn.
- **Persistence:** PostgreSQL 16, SQLAlchemy 2.0 (AsyncIO), Alembic migrations.
- **Caching & Broker:** Redis 7 (Session memory & Celery queue).
- **Asynchronous Processing:** Celery (specialized indexing, multimodal, and SLA queues), Celery Beat (cron scheduling).
- **Vector Search:** FAISS (in-memory, rebuildable index snapshots with atomic symlink promotion).
- **AI Inference:** Google Gemini API (`google-genai`), Ollama (LLaMA 3 local fallback), HuggingFace MiniLM (`all-MiniLM-L6-v2`).
- **Multimodal & OCR:** Pillow, PyMuPDF (`fitz`), Gemini Vision.
- **Containerization:** Docker, Docker Compose.

---

## 4. 16-Day Engineering Roadmap

- [x] **Day 01:** System Architecture, Engineering Foundation & Repository Setup *(Completed)*
- [x] **Day 02:** Database Models, SQLAlchemy Schemas, Alembic Migrations & Seed Data *(Completed & Formally Approved)*
- [x] **Day 03:** Foundational Services (LLM Gateway, Embeddings, PII Masker, Redis Cache) *(Completed & Formally Verified)*
- [x] **Day 04 (Phase 1):** Task 1 — Knowledge Base Ingestion, Sanitization & Change Detection *(Completed, Hardened & Formally Audited)*
- [x] **Day 04 (Phase 2):** Task 1 — Knowledge Base FAISS Vector Builder & Persistent Staging *(Completed & Formally Verified)*
- [x] **Day 04 (Phase 3) / Day 05:** Task 1 — Golden Benchmark Gates, Maintenance Promotion & Auto-Rollback Watchdog *(Completed & Formally Audited)*
- [x] **Day 06 (Phases 4A, 4B, 4C, 4D):** Task 4 — Advanced RAG Knowledge Assistant (Retriever, Generator, Citations, Orchestrator, Sanitizer Vault & Canonical REST API) *(Completed & Formally Audited)*
- [x] **Day 07:** Task 6 — Multilingual Conversation Engine (Phases 0, 1, 2, 3 Completed, Hardened & Audited) *(Phases 0–3 Completed & Audited; Phase 4 Pending Authorization)*
- [ ] **Day 08:** Task 5 — Sentiment Analysis & Tone Adaptation Engine
- [ ] **Day 09:** Task 5 — Risk Escalation & 15-Minute Inactivity Escalation
- [ ] **Day 10:** Task 2 — Multimodal Evidence Analyzer (File Ingestion & OCR)
- [ ] **Day 11:** Task 2 — Multimodal Cross-Verification & Prompt Injection Guard
- [ ] **Day 12:** Task 3 — Intelligent Ticket Management (Synthesis & Completeness Checks)
- [ ] **Day 13:** Task 3 — Dynamic Priority & Calendar-Aware SLA Dispatcher
- [ ] **Day 14:** Orchestration Facade & End-to-End Interservice Integration
- [ ] **Day 15:** Frontend Implementation (Customer Portal & Agent Workspace)
- [ ] **Day 16:** End-to-End Testing, Security Hardening & Production Dockerization

---

## 5. Verification Commands

To validate the platform implementation and persistence foundation, run:

```bash
# Run full automated test suite (in-memory SQLite + live PostgreSQL 16)
python -c "import sys; sys.path.insert(0, 'backend'); import pytest; sys.exit(pytest.main(['backend/tests', '-v']))"

# Execute PostgreSQL 16 live setup, migration, and audit script
python backend/scripts/setup_and_verify_postgres.py

# Verify all 20 tables, columns, constraints, and indexes
python -c "import psycopg2; from app.core.config import settings; conn = psycopg2.connect(host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD, dbname=settings.POSTGRES_DB); cur = conn.cursor(); cur.execute(\"SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name != 'alembic_version'\"); print('Verified tables:', cur.fetchone()[0]); cur.close(); conn.close()"
```
