# Technology Decisions Document (ADR Log)
## Unified Intelligent Customer Support AI Platform

**Document Version:** 1.1.0  
**Format:** Architectural Decision Records (ADR)  
**Status:** Approved Architectural Baseline (Day 01 Audit Corrections Applied)  

---

## 1. Overview of Technology Stack

The platform's technology stack was selected to satisfy enterprise reliability, high-concurrency asynchronous workloads, strict data security, and seamless integration across all six modules.

| Domain | Technology Selected | Primary Purpose |
| :--- | :--- | :--- |
| **Frontend Runtime** | React 18 + TypeScript + Vite | Customer support portal and agent command console. |
| **Frontend Routing** | `react-router-dom` v6 | Client-side page navigation (Customer, Agent, Knowledge, Health). |
| **Frontend Networking**| `axios` v1 | REST API communication with dual-auth interceptors. |
| **Frontend Styling** | Vanilla CSS Design Tokens | High-contrast, responsive, zero-utility bloat design system. |
| **Backend Runtime** | Python 3.12 + FastAPI | Async RESTful and WebSocket API gateway. |
| **Data Validation** | Pydantic v2 | High-throughput schema enforcement and serialization. |
| **Authoritative DB** | PostgreSQL 16 + SQLAlchemy 2.0 | Transactional persistence, JSONB queries, relational integrity. |
| **Schema Migrations**| Alembic | Deterministic forward/backward database schema versioning. |
| **Vector Storage** | FAISS `IndexIDMap2(IndexFlatIP)` | In-memory semantic vector indexing mapped to BIGINT IDs. |
| **Task Queue** | Celery + Redis 7 | Asynchronous worker fabric, Pub/Sub reload, and cron tasks. |
| **Primary AI Engine**| Google Gemini API (`google-genai`)| Grounded RAG synthesis, multilingual chat, multimodal vision. |
| **Fallback AI Engine**| Ollama with LLaMA 3 | Local, offline-resilient inference fallback during cloud outages. |
| **Embeddings** | HuggingFace `sentence-transformers` | Deterministic local text embeddings (`all-MiniLM-L6-v2`). |
| **Multimodal Vision**| Pillow, PyMuPDF, Gemini Vision | Document rasterization, PDF parsing, OCR, and visual checks. |
| **Password Security**| `pwdlib[bcrypt]` / `bcrypt` | Python 3.12-compatible secure password hashing. |
| **File Sniffing** | Magic Byte Header Sniffing | Cross-platform (Windows & Linux) MIME validation. |

---

## 2. Architectural Decision Records (ADRs)

### ADR-01: FastAPI & Python 3.12 for Backend Architecture
- **Context:** The platform requires asynchronous request handling for streaming chat, WebSocket sessions, and high-throughput REST APIs, combined with deep integration into AI and ML libraries.
- **Decision:** Use FastAPI on Python 3.12 with Pydantic v2.
- **Rationale:** 
  - Python is the standard for AI, LangChain, FAISS, and computer vision ecosystems.
  - FastAPI provides native `asyncio` execution, automatic OpenAPI 3.1 documentation generation, and industry-leading serialization speeds via Pydantic v2.
- **Alternatives Considered & Rejected:**
  - *Django:* Monolithic, heavier ORM overhead, non-native async streaming.
  - *Node.js / Express:* Excellent I/O, but poor native integration with FAISS, Pillow, PyMuPDF, and Python ML pipelines.

### ADR-02: PostgreSQL 16 as the Authoritative Single Source of Truth
- **Context:** The system manages transactional support tickets, SLA calendars, user identities, customer sessions, and immutable document version catalogs.
- **Decision:** Adopt PostgreSQL 16 as the sole authoritative persistence store.
- **Rationale:**
  - ACID guarantees ensure ticket creation, escalation logging, and document status updates are strictly consistent.
  - Advanced `JSONB` support with GIN indexing allows semi-structured metadata (extracted evidence entities, RAG chunk metadata) to be queried with high performance without an external NoSQL database.
- **Alternatives Considered & Rejected:**
  - *MongoDB:* Lacks native relational joins and ACID guarantees across complex multi-table ticket/audit workflows.

### ADR-03: FAISS Index Architecture & Metadata-Filtered Retrieval
- **Context:** The system requires fast, versioned vector search supporting Blue/Green candidate promotion, automated 5-minute rollbacks, and strict metadata filtering by access level, region, and effective date.
- **Decision:** Adopt `faiss.IndexIDMap2(faiss.IndexFlatIP(384))` on unit-normalized embeddings, mapped directly to PostgreSQL `document_chunks.faiss_vector_id BIGINT`, executing metadata-filtered retrieval via `faiss.SearchParameters(sel=faiss.IDSelectorBatch(valid_ids))` with a Three-Layer Defense-in-Depth Security Guarantee.
- **Runtime Verification:** Verified empirically on Python 3.12 with `faiss-cpu==1.15.1`. In modern FAISS, `IndexIDMap2` properly delegates `SearchParameters` to `IndexFlatIP` via `IDSelectorTranslated`. Index serialization (`faiss.write_index`) and deserialization (`faiss.read_index`) preserve custom 64-bit IDs and work seamlessly with `IDSelectorBatch`. Boundary conditions (empty candidate sets, candidate counts $< k$, disjoint IDs) return sentinel values (`-1`, distance `-3.4028235e+38`) cleanly without exceptions.
- **Comparative Evaluation of Filtering Approaches (Benchmarked on 20,000 vectors, $d=384$):**
  1. **Selected Solution — `IndexFlatIP` + `IDSelectorBatch`:**
     - *Latency:* ~1.85 ms.
     - *Recall:* 100% exact cosine similarity recall across authorized candidates.
     - *Operational Simplicity:* Zero index training required (`is_trained = True` immediately). Supports arbitrary dynamic chunk additions and deletions via `remove_ids()`.
  2. **Rejected Alternative — `IndexFlatIP` Over-Fetch ($k=100$) + Application-Level Filtering:**
     - *Latency:* ~2.32 ms.
     - *Vulnerability — Recall Starvation:* If a customer query semantically matches higher-tier internal runbooks or policies from another region, the top-$K$ over-fetch window is monopolized by unauthorized items. When application filtering discards them, the result collapses to 0 valid chunks, causing false refusals or hallucinations even when authorized matching policies exist.
  3. **Rejected Alternative — `IndexIVFFlat` + `SearchParametersIVF`:**
     - *Latency:* ~0.34 ms.
     - *Vulnerability — Cluster Pruning Drops & Maintenance Friction:* Requires offline centroid clustering (`index.train()`) and hyperparameter tuning (`nlist`, `nprobe`). If candidate authorized vectors lie in clusters not probed by the query, they are missed entirely (empirically confirmed: top-1 vector dropped in benchmark). Dynamic document ingestion skews cluster centroids, requiring periodic index retrains. Unjustified complexity for corpora under 100,000 chunks.
- **Three-Layer Defense-in-Depth Security Guarantee:**
  - *Layer 1 (PostgreSQL Pre-Filter):* Evaluates caller `access_tier`, `region`, and temporal validity window (`effective_from <= date <= effective_until`). Returns candidate `faiss_vector_id`s. If 0 candidates match, retrieval stops immediately.
  - *Layer 2 (FAISS Pre-Filtered Search):* `IDSelectorBatch(valid_ids)` guarantees distance calculations occur strictly across authorized candidates.
  - *Layer 3 (Application-Layer Security Gate):* Verifies all returned IDs against the authorized candidate whitelist, strips sentinel `-1` entries, and checks that chunk access tiers $\le$ caller tier before loading chunk text. **Zero unauthorized chunks can enter the LLM context.**
- **Alternatives Considered & Rejected:**
  - *Pinecone / Milvus:* Adds network latency, external cloud billing, and complex multi-environment mocking overhead.
  - *FAISS Flat without IDMap:* Only supports sequential 0-indexed offsets, breaking synchronization when chunks are inserted or versioned.

### ADR-04: Dual-Provider AI Strategy (Google Gemini API + Local Ollama Fallback)
- **Context:** Enterprise customer support cannot afford complete downtime when an upstream cloud AI API experiences outages, rate limiting, or network partitions.
- **Decision:** Implement a unified LLM gateway using Google Gemini API as the primary model and Ollama (LLaMA 3) as a local fallback.
- **Rationale:**
  - Gemini provides state-of-the-art reasoning, native multimodal vision, large context windows, and low latency.
  - Ollama provides local, air-gapped resilience, ensuring emergency ticket dispatch and basic conversation continuity continue even during external API downtime.
- **Alternatives Considered & Rejected:**
  - *Single Cloud Provider:* Single point of failure violating enterprise high-availability standards.

### ADR-05: Redis & Celery for Asynchronous Worker Fabric & Cross-Process Pub/Sub
- **Context:** Heavy document OCR, multi-page PDF rasterization, vector embedding generation, 15-minute inactivity monitors, and cross-process vector index reload notifications between Celery and FastAPI.
- **Decision:** Deploy Celery backed by Redis 7 for task queuing and Celery Beat for periodic cron monitoring, leveraging Redis Pub/Sub (`channel:knowledge:index_reload`) for cross-process index swap signaling.
- **Rationale:**
  - Celery provides robust task retries (15, 30, 60 minutes), dead-letter handling, rate-limiting, and distributed worker scaling.
  - Redis serves a triple role: fast session memory cache, Celery message broker, and real-time Pub/Sub signal bus.
- **Alternatives Considered & Rejected:**
  - *FastAPI BackgroundTasks:* In-process only, non-persistent, loses tasks if the server restarts, cannot scale across worker nodes.

### ADR-06: React 18, TypeScript, and Vite with Vanilla CSS Design System
- **Context:** The web application requires high-performance rendering for live streaming chat, kanban boards, and accessible ticket inspectors.
- **Decision:** Use React 18 with TypeScript, bundled with Vite, styled with a semantic Vanilla CSS Design Token system.
- **Rationale:**
  - Vite provides instantaneous Hot Module Replacement (HMR) and optimized tree-shaken production bundles.
  - Vanilla CSS design tokens provide total control over themes, dark mode, glassmorphism surfaces, and micro-animations without external framework lock-in or style collisions.
- **Alternatives Considered & Rejected:**
  - *Tailwind CSS:* Adds build abstraction and utility class pollution unless explicitly demanded; Vanilla CSS tokens provide superior maintainability for long-term design consistency.

### ADR-07: Password Hashing on Python 3.12 (`pwdlib[bcrypt]`)
- **Context:** The legacy `passlib` library has not seen active updates for modern Python 3.12, triggering deprecation warnings or import errors with modern `bcrypt >= 4.1.0` (due to the removal of the standard library `crypt` module in Python 3.12).
- **Decision:** Adopt `pwdlib[bcrypt] >= 0.2.0` (or `bcrypt >= 4.1.3` with `cryptography >= 43.0.0`) as the password hashing engine.
- **Rationale:**
  - `pwdlib` is modern, actively maintained, and fully compatible with Python 3.12's standard library changes.
  - It uses secure, constant-time verification with audited bcrypt work factors.
- **Alternatives Considered & Rejected:**
  - *Legacy passlib[bcrypt]:* Deprecated on Python 3.12, prone to runtime attribute errors with recent bcrypt versions.

### ADR-08: Cross-Platform File MIME Detection (Windows & Linux Compatibility)
- **Context:** `python-magic` relies on OS-level `libmagic` C shared libraries (`libmagic.so` on Linux, `libmagic.dll` on Windows). On Windows developer machines, `import magic` frequently throws `ImportError: failed to find libmagic`.
- **Decision:** Implement a cross-platform binary header magic byte sniffing validator (checking initial bytes for `%PDF-`, `\x89PNG`, `\xFF\xD8\xFF`, `RIFF....WEBP`), complemented by `python-magic-bin` for Windows and `python-magic` for Linux containers.
- **Rationale:**
  - Pure-Python binary header inspection guarantees 100% reliable MIME identification without external native DLL compilation or system path issues on Windows.
  - Eliminates developer setup friction while maintaining rigorous extension-spoofing defense.
- **Alternatives Considered & Rejected:**
  - *File extension only:* Insecure; allows attackers to upload executable `.exe` disguised as `.pdf`.
  - *Forcing libmagic Windows DLL installation:* Fragile and breaks CI/developer onboarding.

### ADR-09: Frontend Routing & HTTP Client Dependencies (`react-router-dom` & `axios`)
- **Context:** The architecture specifies distinct application views (`CustomerPortal`, `AgentWorkspace`, `KnowledgeOps`, `SystemHealth`) and robust REST API communications with request/response interceptors for session tokens and JWTs.
- **Decision:** Include `react-router-dom` (v6) and `axios` (v1) in `frontend/package.json`.
- **Rationale:**
  - `react-router-dom` enables declarative client-side routing, route-level authorization guards, and URL synchronization between the customer chat widget and the agent console.
  - `axios` provides clean request interceptors to automatically inject `X-Session-Token` or `Authorization: Bearer <jwt>`, unified response error handling (RFC 7807), and upload progress events for multimodal evidence files.
- **Alternatives Considered & Rejected:**
  - *Single-component conditional rendering without router:* Hard to deep-link, breaks browser history navigation, poor modularity.
  - *Native window.fetch without wrapper:* Requires repetitive boilerplate for auth header injection and JSON error parsing across all services.
