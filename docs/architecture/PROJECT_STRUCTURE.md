# Project Structure Document
## Unified Intelligent Customer Support AI Platform

**Document Version:** 1.1.0  
**Design Standard:** Clean Architecture & Modular Monolith Layout  
**Status:** Approved Architectural Baseline (Day 01 Audit Corrections Applied)  

---

## 1. Complete Repository Directory Tree

```
AI-Customer-Support-Platform/
├── .env.example                     # Canonical configuration template (zero real secrets)
├── .gitignore                       # Multi-stack git ignore rules (Python, Node, Docker)
├── README.md                        # Master engineering roadmap & setup documentation
├── docker-compose.yml               # Multi-service composition (App, DB, Redis, Celery)
│
├── docs/                            # Comprehensive architectural documentation
│   ├── architecture/
│   │   ├── PRODUCT_REQUIREMENTS.md  # PRD, functional requirements & RTM matrix
│   │   ├── SYSTEM_ARCHITECTURE.md   # System overview, sync/async, diagrams
│   │   ├── DATABASE_DESIGN.md       # PostgreSQL schema, ER diagram, migrations
│   │   ├── API_SPECIFICATION.md     # REST & WebSocket API endpoints & contracts
│   │   ├── MODULE_DEPENDENCIES.md   # DAG diagram, interservice contracts
│   │   ├── SECURITY_ARCHITECTURE.md # Threat model, RBAC, PII redaction
│   │   ├── TESTING_STRATEGY.md      # Test pyramid, module test matrix
│   │   ├── TECHNOLOGY_DECISIONS.md  # ADR log, stack justification
│   │   └── PROJECT_STRUCTURE.md     # Code organization & naming guidelines
│   └── guides/                      # Developer onboarding and runbooks
│
├── backend/                         # Core Python 3.12 FastAPI backend
│   ├── Dockerfile                   # Production container definition
│   ├── .dockerignore                # Container build exclusions
│   ├── pyproject.toml               # Poetry/Pip dependencies & tool configurations
│   ├── alembic.ini                  # Alembic database migration config
│   │
│   ├── alembic/                     # Database migration revisions
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/
│   │
│   ├── app/                         # Application source code
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI initialization & middleware chain
│   │   │
│   │   ├── core/                    # Core infrastructural kernel
│   │   │   ├── __init__.py
│   │   │   ├── config.py            # Pydantic BaseSettings management
│   │   │   ├── security.py          # JWT creation/verification & RBAC guards
│   │   │   ├── logging.py           # Structured JSON logger with PII masking
│   │   │   └── exceptions.py        # RFC 7807 problem exceptions
│   │   │
│   │   ├── db/                      # Database connection and session management
│   │   │   ├── __init__.py
│   │   │   ├── session.py           # Async engine and sessionmaker
│   │   │   └── base.py              # Declarative Base and model registry
│   │   │
│   │   ├── models/                  # SQLAlchemy 2.0 ORM entity models
│   │   │   ├── __init__.py
│   │   │   ├── auth.py              # User, Role, UserRole
│   │   │   ├── customer.py          # Customer, Session, Conversation, Message
│   │   │   ├── sentiment.py         # SentimentLog
│   │   │   ├── document.py          # Document, DocumentVersion, DocumentChunk
│   │   │   ├── knowledge_index.py   # KnowledgeIndex, BenchmarkRun
│   │   │   ├── evidence.py          # EvidenceFile, ExtractedEvidenceEntity
│   │   │   └── ticket.py            # Ticket, TicketEvent, EscalationAudit, SLACalendar
│   │   │
│   │   ├── schemas/                 # Pydantic v2 validation DTOs
│   │   │   ├── __init__.py
│   │   │   ├── common.py            # Response envelopes, error models, pagination
│   │   │   ├── auth.py              # Token, LoginRequest, UserProfile
│   │   │   ├── chat.py              # ChatTurnRequest, ChatTurnResponse
│   │   │   ├── evidence.py          # EvidenceUploadResponse, ExtractedEntities
│   │   │   ├── ticket.py            # TicketCreate, TicketResponse, PriorityBreakdown
│   │   │   └── knowledge.py         # DocumentCreate, SyncStatus, CandidateStatus
│   │   │
│   │   ├── api/                     # API routes & dependency injection
│   │   │   ├── __init__.py
│   │   │   ├── deps.py              # get_db, get_current_user, require_role
│   │   │   └── v1/
│   │   │       ├── __init__.py
│   │   │       ├── router.py        # Aggregator router mounting all modules
│   │   │       ├── endpoints/
│   │   │       │   ├── auth.py
│   │   │       │   ├── chat.py
│   │   │       │   ├── evidence.py
│   │   │       │   ├── tickets.py
│   │   │       │   ├── knowledge.py
│   │   │       │   └── health.py
│   │   │
│   │   ├── modules/                 # Six modular domain engines
│   │   │   ├── knowledge/           # TASK 1: Automated Knowledge Base Pipeline
│   │   │   │   ├── change_detector.py
│   │   │   │   ├── sanitizer.py
│   │   │   │   ├── vector_builder.py
│   │   │   │   ├── golden_evaluator.py
│   │   │   │   ├── index_manager.py
│   │   │   │   └── watchdog.py
│   │   │   │
│   │   │   ├── evidence/            # TASK 2: Multimodal Evidence Analyzer
│   │   │   │   ├── file_validator.py
│   │   │   │   ├── ocr_extractor.py
│   │   │   │   ├── claim_verifier.py
│   │   │   │   ├── injection_guard.py
│   │   │   │   └── retention_cleaner.py
│   │   │   │
│   │   │   ├── ticketing/           # TASK 3: Intelligent Ticket Management
│   │   │   │   ├── ticket_builder.py
│   │   │   │   ├── priority_calculator.py
│   │   │   │   ├── routing_engine.py
│   │   │   │   ├── sla_calculator.py
│   │   │   │   ├── cluster_matcher.py
│   │   │   │   └── handoff_generator.py
│   │   │   │
│   │   │   ├── rag/                 # TASK 4: Advanced RAG Knowledge Assistant
│   │   │   │   ├── chunker.py
│   │   │   │   ├── temporal_retriever.py
│   │   │   │   ├── access_filter.py
│   │   │   │   ├── citation_formatter.py
│   │   │   │   └── injection_detector.py
│   │   │   │
│   │   │   ├── sentiment/           # TASK 5: Sentiment & Escalation Engine
│   │   │   │   ├── classifier.py
│   │   │   │   ├── trajectory_analyzer.py
│   │   │   │   ├── tone_adapter.py
│   │   │   │   ├── risk_escalator.py
│   │   │   │   └── inactivity_monitor.py
│   │   │   │
│   │   │   └── multilingual/        # TASK 6: Multilingual Conversation Engine
│   │   │       ├── language_detector.py
│   │   │       ├── translator.py
│   │   │       ├── entity_preserver.py
│   │   │       ├── context_memory.py
│   │   │       └── session_manager.py
│   │   │
│   │   ├── services/                # Shared cross-cutting infrastructural services
│   │   │   ├── llm_gateway.py       # Gemini API / Ollama fallback router
│   │   │   ├── embedding_service.py # HuggingFace MiniLM embeddings
│   │   │   ├── pii_masker.py        # Sensitive data redaction filter
│   │   │   ├── redis_service.py     # Fast session caching & lock manager
│   │   │   └── orchestrator.py      # Master conversational pipeline facade
│   │   │
│   │   └── workers/                 # Celery distributed workers & tasks
│   │       ├── celery_app.py        # Celery configuration & beat schedule
│   │       └── tasks/
│   │           ├── indexing_tasks.py
│   │           ├── multimodal_tasks.py
│   │           ├── sla_tasks.py
│   │           └── cleanup_tasks.py
│   │
│   └── tests/                       # Comprehensive automated test suites
│       ├── conftest.py              # Pytest fixtures, mock LLMs, in-memory DB
│       ├── unit/                    # Fast isolated component tests
│       ├── integration/             # Multi-module & database integration tests
│       └── security/                # Adversarial injection & PII leakage tests
│
├── frontend/                        # React 18 + TypeScript + Vite web client
│   ├── Dockerfile                   # Production Nginx frontend container
│   ├── .dockerignore
│   ├── package.json                 # Node package configuration
│   ├── tsconfig.json                # Strict TypeScript configuration
│   ├── vite.config.ts               # Vite bundler configuration
│   ├── index.html                   # HTML5 entrypoint
│   │
│   └── src/
│       ├── index.css                # Global design tokens, themes & resets
│       ├── main.tsx                 # React DOM mount point
│       ├── App.tsx                  # Root component & routing
│       │
│       ├── components/              # Reusable UI component library
│       │   ├── common/              # Buttons, inputs, modals, alerts, badges
│       │   ├── chat/                # ChatBubble, MessageInput, StreamingView
│       │   ├── evidence/            # DragDropUpload, EvidenceInspector, OCRPreview
│       │   ├── tickets/             # TicketCard, SLACountdown, PriorityBadge
│       │   └── knowledge/           # VersionTable, BenchmarkScorecard, RollbackModal
│       │
│       ├── pages/                   # Application view containers
│       │   ├── CustomerPortal.tsx   # Customer conversational chat & evidence page
│       │   ├── AgentWorkspace.tsx   # Live ticket management & handoff briefing
│       │   ├── KnowledgeOps.tsx     # Index version management & benchmark reports
│       │   └── SystemHealth.tsx     # Latency metrics, queue depths, SLA health
│       │
│       ├── hooks/                   # Custom React hooks (useChat, useWebSocket, useAuth)
│       ├── services/                # Axios API client & WebSocket adapters
│       └── types/                   # TypeScript interfaces matching backend models
│
├── infrastructure/                  # Operational & deployment configurations
│   ├── docker/                      # Specialized container configs (postgres, redis)
│   └── scripts/                     # Seeders, benchmark suites & backup scripts
│
└── storage/                         # Local mounted volumes (gitignored)
    ├── raw_documents/               # Staging area for incoming knowledge documents
    ├── indices/                     # FAISS active and candidate index files
    ├── evidence_uploads/            # Customer uploaded screenshots and PDFs
    └── quarantine/                  # Flagged toxic or injection files
```

---

## 2. Naming Conventions & Code Standards

- **Python (Backend):**
  - Files and directories: `snake_case` (e.g. `change_detector.py`, `routing_engine.py`).
  - Classes: `PascalCase` (e.g. `DocumentVersionManager`, `SmartDispatchEngine`).
  - Functions, methods, and variables: `snake_case` (e.g. `calculate_priority_score`).
  - Constants: `UPPER_SNAKE_CASE` (e.g. `MAX_UPLOAD_SIZE_BYTES`, `DEFAULT_SLA_TARGET_MINUTES`).
  - Type annotations: Mandatory on all function signatures (`def method(param: str) -> bool:`).
- **TypeScript (Frontend):**
  - Component files: `PascalCase.tsx` (e.g. `CustomerPortal.tsx`, `TicketCard.tsx`).
  - Utility/hook files: `camelCase.ts` (e.g. `useWebSocket.ts`, `apiClient.ts`).
  - Interfaces and Types: `PascalCase` with explicit descriptors (e.g. `TicketResponse`, `ChatTurnPayload`).
- **Database (PostgreSQL):**
  - Tables: plural `snake_case` (e.g. `tickets`, `document_versions`, `sentiment_logs`).
  - Primary keys: `id UUID PRIMARY KEY`.
  - Foreign keys: `<singular_table>_id` (e.g. `customer_id`, `conversation_id`).
  - Timestamp columns: `_at` suffix (e.g. `created_at`, `expires_at`, `sla_breach_at`).
  - Boolean flags: `is_` or `has_` prefix (e.g. `is_active`, `is_escalated`, `has_attachments`).
