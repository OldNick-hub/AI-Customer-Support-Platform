# Day 07 — Task 6: Multilingual Support
## Phase 4 Implementation Plan: Multilingual Orchestrator & Endpoint Integration (v2.16.0)

**Target Milestone:** Day 07 — Task 6: Multilingual Support (Phase 4 of 4)  
**Document Version:** 2.16.0 (Targeted Correction: Database-Controlled Atomic Turn Allocator, Table-Specific Least-Privilege Grants, Verified Migration Connection Configuration & Real-Role Test Matrix)  
**Status:** PROPOSED & AWAITING DUAL MIGRATION & IMPLEMENTATION AUTHORIZATION  
**Author:** Senior AI/ML Architect & Backend Security Engineer  
**Canonical Endpoint:** `POST /api/v1/chat/message`  
**Verified Baseline Suite:** 420 / 420 passing (100% green in 45.73s; exit code 0; 0 failures, 0 warnings)  
**Task 6 Baseline Suite (Phases 1–3):** 106 / 106 passing (100% green in 1m 58s; exit code 0; 0 failures, 0 warnings)  
**FAISS Production Invariant:** `storage/indices/active_index.json` confirmed ABSENT (`False`)  
**Database Schema Status:** GUID and `sa.String(36)` compatibility forensically verified; 100% ORM/migration parity verified across all indexes, constraints, nullability, Python defaults, and server defaults. 1 dedicated migration proposed (`002_chat_idempotency_and_outbox.py`). Awaiting explicit dual authorization before execution.  

---

## 1. Forensic Read-Only Inspection Evidence

Before formulating this specification, read-only forensic inspections of the existing production schema, models, connection configurations, and migration environments were conducted:

1. **[`backend/app/core/config.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/core/config.py) (lines 39–48, 201–225):**
   - Database connection settings: `POSTGRES_USER: str = "postgres"`, `POSTGRES_PASSWORD: str = ""`, `POSTGRES_HOST: str = "localhost"`, `POSTGRES_PORT: int = 5432`, `POSTGRES_DB: str = "ai_customer_support"`.
   - `async_database_url` generates `postgresql+asyncpg://...` using `POSTGRES_USER`.
   - `sync_database_url` generates `postgresql+psycopg2://...` using `POSTGRES_USER`.
   - **Key Architectural Finding:** Both the runtime application engine and the Alembic migration runner share a single configured database connection string (`POSTGRES_USER`). In development/testing, this is the default PostgreSQL superuser/owner. In production, role separation (`app_user` vs. `migration_admin`) must be enforced as a deployment prerequisite.

2. **[`backend/app/db/session.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/db/session.py) (lines 1–55):**
   - Configures `AsyncSessionLocal` using `async_engine` bound to `settings.async_database_url`.
   - Configures `sync_engine` using `create_engine` bound to `settings.sync_database_url`.

3. **[`backend/alembic/env.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/alembic/env.py) (lines 1–60):**
   - Alembic imports `settings.async_database_url` and binds `target_metadata = Base.metadata`.
   - Runs migrations inside a single synchronous/async transaction block (`with context.begin_transaction():`).

4. **[`backend/alembic/versions/001_initial_schema.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/alembic/versions/001_initial_schema.py) (lines 108–136):**
   - Table `conversations` (lines 108–118): Defines `turn_count: sa.Integer(), nullable=False, server_default="0"`.
   - Table `messages` (lines 119–136): Defines columns `id String(36)`, `conversation_id String(36)`, `sender_type String(20)`, `sender_id String(36)`, `content Text`, `encrypted_raw_content LargeBinary`, `detected_language String(10)`, `language_confidence Float`, `tokens_used Integer`, `created_at DateTime(timezone=True)`.

5. **[`backend/app/models/customer.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/models/customer.py) (lines 80–152):**
   - Model `Message` nullability audit:
     - `id: Mapped[uuid.UUID]` (non-nullable PK).
     - `conversation_id: Mapped[uuid.UUID]` (`nullable=False`).
     - `sender_type: Mapped[str]` (`nullable=False`).
     - `sender_id: Mapped[Optional[uuid.UUID]]` (**`nullable=True`**).
     - `content: Mapped[str]` (`nullable=False`).
     - `encrypted_raw_content: Mapped[Optional[bytes]]` (**`nullable=True`**).
     - `turn_sequence_number: Mapped[Optional[int]]` (**`nullable=True`**).

6. **Repository-Wide PostgreSQL Role & Privilege Configuration Audit:**
   - **[`backend/app/core/config.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/core/config.py#L39-L48) (lines 39–48, 201–225):** Configures default connection: `POSTGRES_USER: str = "postgres"`, `POSTGRES_PASSWORD: str = ""`. Both `async_database_url` (runtime FastAPI engine) and `sync_database_url` (migration engine) construct connection URLs using `POSTGRES_USER`.
   - **[`.env`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/.env#L18) (line 18):** Defines `POSTGRES_USER=postgres`, `POSTGRES_PASSWORD=Mahi2208`.
   - **[`docker-compose.yml`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/docker-compose.yml#L11-L14) (lines 11–14):** Configures `POSTGRES_USER: ${POSTGRES_USER:-support_admin}`, `POSTGRES_DB: ${POSTGRES_DB:-customer_support_db}`.
   - **[`backend/scripts/setup_and_verify_postgres.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/scripts/setup_and_verify_postgres.py#L34-L140) (lines 34–140):** Connects as `settings.POSTGRES_USER` for database creation, Alembic execution, schema auditing, and seeding.
   - **[`backend/tests/test_postgres_integration.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/tests/test_postgres_integration.py#L31-L48) (lines 31–48):** Live integration test suite binds `AsyncEngine` directly to `settings.async_database_url` (`POSTGRES_USER`).
   - **[`backend/alembic/env.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/alembic/env.py#L33-L38) (lines 33–38):** Migration runner binds to `settings.async_database_url` (`POSTGRES_USER`).
   - **Forensic Assessment (STATUS: CURRENTLY UNIMPLEMENTED IN CODEBASE):** Zero role separation exists in the current codebase. `backend/alembic/env.py` (lines 33–38) sets `sqlalchemy.url` using `settings.async_database_url`, which resolves to `POSTGRES_USER` (`postgres` in `.env` / `support_admin` in `docker-compose.yml`). The runtime application, test harnesses, setup scripts, and Alembic migrations all share this single superuser/owner database account. Role separation between `app_user` (strictly least-privilege DML, non-owner) and `migration_admin` (table owner, schema DDL) is **NOT active in the codebase or PostgreSQL instance**. It is a mandatory proposed deployment task that cannot be claimed as active until dual authorization is granted, configuration files are updated, and role verification tests pass.

---

## 2. Exhaustive `turn_count` Writer Audit & Inventory

A repository-wide search across all `.py` files in `backend/` was executed for `turn_count`, assignments, updates, and message creation.

### 2.1. Writer Inventory Table

| # | Exact File & Line Range | Entity & Operation Performed | Message Types Involved | Does `turn_count` Change? | Customer/AI Pair Guaranteed? | Transactional Boundary | Writer Analysis & Single Contract Enforcement |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | `backend/alembic/versions/001_initial_schema.py:110` | DDL Schema Definition (`sa.Column("turn_count", sa.Integer(), ... server_default="0")`) | None | No (Initializes schema default) | N/A | DDL Migration | Establishes column in PostgreSQL with default 0. |
| **2** | `backend/app/models/customer.py:98` | SQLAlchemy ORM Model Declaration (`turn_count = mapped_column(..., default=0)`) | None | No (ORM declaration) | N/A | DeclarativeBase | Declares model attribute. |
| **3** | `backend/app/modules/multilingual/lifecycle_manager.py:501-512` | Restores `IDLE_CLOSED` session; inserts fresh `Conversation(turn_count=0)` | None | Explicitly sets `turn_count=0` | N/A (Fresh thread) | Enclosed in `session_lock` + DB transaction | Valid initialization of a fresh conversation thread upon session restoration. |
| **4** | `backend/app/modules/multilingual/lifecycle_manager.py:518-535` | Inserts solitary `seed_msg` (`sender_type="AI_AGENT"`) containing untrusted summary | Solitary `AI_AGENT` | **No change (`turn_count` remains 0)** | **No** (Solitary seed) | Same restoration transaction | The untrusted summary seed is an initial prompt context seed, NOT a customer dialog turn. It correctly retains `turn_sequence_number = NULL`. |
| **5** | `backend/app/api/v1/chat.py:158-250` | Canonical endpoint in Phase 4D; executes RAG query via `RAGOrchestrator` | None | **No DB writes** (RAG only) | N/A | Stateless query | Currently does not persist dialog messages or update `conversations`. Phase 4 introduces `MultilingualOrchestrator`. |
| **6** | `backend/tests/test_task6_phase3.py:343` | Test fixture setup for Phase 3 lifecycle tests (`Conversation(turn_count=2)`) | Mock messages | Mock assignment | N/A (Test fixture) | Isolated test session | Test fixture asserting idle closure and summary behavior. |

### 2.2. Legacy Backfill Consequences & Zero-`turn_count` Historical Conversations:
- **Zero Production Writers Found:** In the entire existing codebase, **no writer increments `turn_count`**!
- **Consequence for Existing Conversations:** Any conversation created prior to Phase 4 has `turn_count = 0`.
- **Zero-`turn_count` Conversations Remain Unsequenced Legacy:** If an existing conversation contains messages but has `turn_count = 0`, it cannot satisfy `COUNT(messages) == turn_count * 2`. **It will remain legacy data with `turn_sequence_number = NULL`.** We strictly refuse to relax backfill requirements merely to number historical records.
- **Single Allocator Guarantee:** The atomic operation in `MultilingualOrchestrator` Tx2:
  ```sql
  UPDATE conversations
  SET turn_count = turn_count + 1,
      last_message_at = NOW()
  WHERE id = :conversation_id
  RETURNING turn_count;
  ```
  is the **sole and exclusive writer of `turn_count` in the entire platform**. When a customer sends a new turn on a legacy conversation, `turn_count` increments from 0 to 1, allocating sequence 1 for the new turn without corrupting legacy history.

---

## 3. Executive Summary & Objective

Phase 4 completes the implementation of **Day 07 — Task 6: Multilingual Support** by integrating the tested and audited multilingual subsystems:
- **Phase 1:** `PreTranslationSecurityScanner`, `EntityPreservationVault` (128-bit CSPRNG tokens), `LanguageDetector` ($\ge 0.85$ confidence)
- **Phase 2:** `Translator` (Bidirectional neural translation, PII-free Redis caching, localized failure notices)
- **Phase 3:** `ConversationLifecycleManager` (30m idle closure, 24h restoration with untrusted summary seed, 10-turn hot history Redis buffer)

into the canonical FastAPI chat endpoint:
$$\text{POST } /api/v1/chat/message$$

### Core Architecture Invariants for Phase 4 (v2.15.0):
1. **Durable Database Idempotency with Optional Client Keys:**
   - Client requests may supply an `Idempotency-Key` header (or request body parameter).
   - If a key is provided:
     - The turn is fingerprinted using a dedicated, cryptographically validated HMAC-SHA256 secret key ring (`IDEMPOTENCY_HMAC_KEY_RING`).
     - A dedicated reservation is committed to PostgreSQL in **Transaction 1** (`chat_idempotency_records`) with `status='IN_PROGRESS'`, an owner token, and a 30s lease.
     - While processing runs, an independent database session periodically renews the lease every 10s. Renewals enforce `lease_expires_at > NOW()`, strictly forbidding resurrection of expired leases.
     - Concurrent requests with identical keys/fingerprints immediately observe the committed reservation and receive **HTTP 409 Conflict** with `Retry-After: 1` without blocking.
     - Atomic CAS recovery transitions `status='FAILED'` back to `'IN_PROGRESS'` on client retry, enforcing fingerprint and version match directly in SQL.
     - At completion, **Transaction 2** finalizes the idempotency record (requiring `owner_token = :my_owner_token`, `status='IN_PROGRESS'`, and `lease_expires_at > NOW()`), writes the customer and AI messages, increments `conversations.turn_count`, updates session activity, and enqueues a transactional outbox event.
   - If no key is provided:
     - The request executes cleanly as a non-idempotent turn without writing an idempotency record.
2. **Outbox Durability & RESTRICT Deletion Behavior:**
   - Outbox events (`chat_outbox_events`) are committed atomically with the turn in Transaction 2.
   - Foreign keys on `customer_message_id` and `agent_message_id` strictly enforce `ON DELETE RESTRICT`. Deleting a message while an outbox event references it is rejected by PostgreSQL.
   - `PENDING`, `IN_FLIGHT`, and `DEAD_LETTER` outbox rows are never pruned. Only `DELIVERED` outbox events older than 7 days are pruned.
3. **One Authoritative Sequence Source (`conversations.turn_count`):**
   - `conversations.turn_count` in PostgreSQL is the **sole, definitive, atomic allocator** of sequence numbers.
   - Tx2 executes `UPDATE conversations SET turn_count = turn_count + 1 WHERE id = :id RETURNING turn_count;`. The returned integer $S$ is assigned to customer message, agent message, and outbox event.
   - Watermark $W_{auth}$ is **always and exclusively read from `conversations.turn_count`**.
4. **Non-Spoofable Database Migration Boundary & Complete Revocation:**
   - Instead of spoofable custom GUC settings (`app.allow_legacy_sequence_backfill`), the backfill runs inside a restricted migration DDL block.
   - The migration script creates the triggers **AFTER** the verified backfill executes.
   - Once the triggers are landed, the database **permanently and unconditionally forbids any `UPDATE` from assigning a sequence number to an unsequenced message**.
   - No application session or role can ever backfill legacy records post-migration.
5. **Database-Enforced INSERT and UPDATE Immutability Triggers:**
   - `BEFORE INSERT` trigger validates that if `turn_sequence_number IS NOT NULL`:
     1. It **must strictly equal `conversations.turn_count`**.
     2. `sender_type` must be `'CUSTOMER'` or `'AI_AGENT'`. Direct insertion of arbitrary sequence numbers is blocked.
   - `BEFORE UPDATE` trigger uses `IS DISTINCT FROM` to forbid modifying `turn_sequence_number`, `conversation_id`, `sender_type`, `sender_id`, or `content`. Setting a sequence or content to `NULL` is unconditionally blocked.
   - `BEFORE DELETE` trigger forbids deleting any message with a non-null `turn_sequence_number`, even if outbox rows have been pruned.
6. **Separation of Global Continuity ($W_{global\_contig}$) and Retained-Window Continuity ($W_{window\_contig}$):**
   - $W_{global\_contig} \equiv \text{conversations.turn\_count}$: Authoritative, gapless sequence continuity in PostgreSQL.
   - $W_{window\_contig}$: Contiguous sequence watermark within Redis retained window $[\max(1, W_{max}-9), W_{max}]$.
   - Retained-window continuity is complete **if and only if $W_{window\_contig} == W_{max}$**.
   - Intentional sliding window eviction below $\text{window\_start}$ is recognized as normal and never treated as a gap.
   - Readers detecting $W_{window\_contig} < W_{max}$ fall back immediately to PostgreSQL. A partial timeline is never served.

---

## 4. End-to-End Request Sequence & Strict Call Order

For every inbound call to `POST /api/v1/chat/message`, execution follows this exact 14-step pipeline:

```
[Inbound Client Request]
       │
       ▼
[Step 1: Session Token Validation & Expiry Check]
       │ ── Invalid/Expired ──► HTTP 401 Unauthorized
       ▼
[Step 2: Idempotency Key & Fingerprint Evaluation]
       │ ── Key Present: Compute HMAC-SHA256 Fingerprint
       ▼
[Step 3: Transaction 1 — Dedicated Committed Reservation (If Key Present)]
       │ ── Duplicate Key (Same Fingerprint & In-Progress) ──► HTTP 409 Conflict (Retry-After: 1)
       │ ── Duplicate Key (Completed) ──► Decrypt & Return Cached HTTP 200
       │ ── Duplicate Key (Different Fingerprint) ──► HTTP 409 Conflict ("Key reused with different payload")
       │ ── FAILED State ──► CAS Recovery to IN_PROGRESS with New Owner Token
       │ ── Fresh Key ──► INSERT IN_PROGRESS, Commit Tx1, Spawn Heartbeat
       ▼
[Step 4: Session Lifecycle & Inactivity Check]
       │ ── Inactive > 24h ──► HTTP 401 SessionExpired
       │ ── Inactive > 30m ──► Restore Session (Active, Untrusted Summary Seed, Fresh Conv ID)
       │ ── Active <= 30m  ──► Retrieve Hot 10-Turn Redis History (With Contiguous Gap Check)
       ▼
[Step 5: Inbound Security Scan (Perimeter Defense)]
       │ ── Threat Detected ──► Fail-Closed Localized Refusal (Zero Translation, Zero LLM)
       ▼
[Step 6: Language Detection & Routing Policy]
       │ ── Confidence < 0.85 or Unsupported ──► Return Localized Clarification
       │ ── English & No Override ──► Passthrough to Step 9
       │ ── Non-English / Override ──► Proceed to Step 7
       ▼
[Step 7: Pre-Translation Entity Masking & Vaulting]
       │ ── 128-bit CSPRNG Secure Placeholders Replace Entities
       ▼
[Step 8: Inbound Translation to English]
       │ ── Translation Service Unavailable ──► Fallback Tier (Localized Notice)
       ▼
[Step 9: RAG Retrieval & English AI Response Generation]
       │ ── Grounded English Response Generated with Inline Citations [1], [2]
       ▼
[Step 10: Outbound Response Translation & Entity Re-attachment]
       │ ── Re-attach Vaulted Entities via Exact CSPRNG Matching
       │ ── 4-Point Structural Parity Check (Tokens, Citations, Brackets, Invariants)
       │       └── Mismatch ──► Fail-Closed to Localized Service Notice (Zero Heuristic Guessing)
       ▼
[Step 11: Transaction 2 — Atomic Multi-Entity Finalization & Outbox Staging]
       │ ── Verify Owner Token, Status='IN_PROGRESS', and lease_expires_at > NOW()
       │ ── Atomic Sequence Allocation via Database Allocator: SELECT fn_allocate_next_turn_sequence(:conv_id) (Allocates N)
       │       └── app_user has zero direct UPDATE rights on conversations.turn_count; allocation is strictly database-enforced
       │ ── Insert Customer Message (turn_sequence_number = N, sender_type = 'CUSTOMER')
       │ ── Insert Agent Message (turn_sequence_number = N, sender_type = 'AI_AGENT')
       │ ── Insert Outbox Event (turn_sequence_number = N, references customer_message_id AND agent_message_id)
       │ ── Finalize Idempotency Record (status = 'COMPLETED', lease_expires_at = NULL)
       │ ── Commit: Evaluates Deferrable Constraint Trigger tg_enforce_turn_pair_completeness
       │       └── Database aborts commit if customer message, agent message, turn_count, or outbox event is missing/torn
       ▼
[Step 12: Asynchronous Outbox Worker Redis Delivery]
       │ ── SELECT ... FOR UPDATE SKIP LOCKED
       │ ── Dual-Watermark Redis Lua Script (Watermark Enforcement, Gap Detection, Sliding Window)
       │ ── Mark Event DELIVERED in PostgreSQL
       ▼
[Step 13: Heartbeat Task Termination]
       │ ── Cancel & Await Background Heartbeat Loop
       ▼
[Step 14: Return Canonical HTTP 200 Response]
```

---

## 5. Inbound Language Detection vs. Response Preference Policy

### 5.1. Language Selection Decision Table

| Inbound Detected Lang ($L_{det}$, conf $\ge 0.85$) | Client `preferred_language` ($L_{pref}$) | Inbound Translation to English? | English RAG Processing | Outbound Translation Target ($L_{out}$) |
| :--- | :--- | :--- | :--- | :--- |
| `en` | `None` or `"en"` | **No** (Direct Passthrough) | Yes | **None** (English Passthrough) |
| `es` / `fr` / `de` / `hi` | `None` | **Yes** ($L_{det} \rightarrow$ `en`) | Yes | **Yes** (`en` $\rightarrow L_{det}$) |
| `en` | `es` / `fr` / `de` / `hi` | **No** (Input already English) | Yes | **Yes** (`en` $\rightarrow L_{pref}$) |
| `es` | `fr` | **Yes** (`es` $\rightarrow$ `en`) | Yes | **Yes** (`en` $\rightarrow$ `fr`) |
| `und` (conf $< 0.85$) | Any | **No** | **No** | **Clarification Required** |
| Unsupported (e.g. `ru`, `ja`) | Any | **No** | **No** | **Clarification Required** |

### 5.2. Script & Threshold Specifications
- Supported Languages: English (`en`), Spanish (`es`), French (`fr`), German (`de`), Hindi (`hi`).
- Confidence Threshold: $\ge 0.85$.
- Hindi Dual-Script Handling:
  - Devanagari script: Native Unicode block `\u0900-\u097F`.
  - Romanized Hinglish: Latin script matched against curated Hindi phonetic stopword lexicon.
- Untrusted Preference Rule: `preferred_language` represents a **response preference only**; it is NEVER used as proof of inbound query language.

---

## 6. Structurally Safe Citation & Request-Scoped Entity Preservation

### 6.1. Structural Citation Anchoring & Shielding
- Citations generated by RAG appear strictly as `[1]`, `[2]`, `[1, 2]`.
- Before outbound translation, citations are replaced with cryptographic placeholders:
  $$\text{Citation Token} = \text{f"__PRESERVE\_CITE\_\{index\}\_\{secrets.token\_hex(8)\}__"}$$
- Placeholders prevent translation models from localizing bracket syntax or corrupting numeric references.

### 6.2. Request-Scoped Cryptographic Placeholder Security & Concurrent Isolation
- Placeholders utilize 128-bit CSPRNG entropy (`secrets.token_hex(16)`).
- Every request allocates an isolated `EntityPreservationVault` instance. Placeholders are never shared across requests or threads.
- Predictable sequence tokens (`__ENT_0__`, `[CITE_1]`) are strictly forbidden to eliminate prompt collision and cross-request tampering.

### 6.3. Post-Translation 4-Point Structural Parity Check
After outbound translation and placeholder re-attachment, the orchestrator evaluates:
1. **Count Parity:** Count of output citations strictly equals input citations.
2. **Identifier Parity:** Set of citation indices `[i]` in translated text matches source exactly.
3. **Bracket Balance:** Unmatched `[` or `]` brackets trigger parity rejection.
4. **Placeholder Leakage:** Any unreplaced `__PRESERVE_` substring triggers immediate rejection.

### 6.4. Elimination of Heuristic Reattachment & Fail-Closed Behavior
- If the 4-point structural parity check fails, the orchestrator **fails closed**.
- Heuristic string appending or bracket guessing is strictly prohibited.
- The endpoint emits a localized technical service notice (`LOCALIZED_SERVICE_NOTICES[target_lang]`).

---

## 7. Reconciled Localization Fallback Matrix & Semantic State Separation

### 7.1. Semantic State Decoupling
The canonical response model cleanly separates mutually exclusive outcomes:
- **Clarification (`clarification_needed=True`):** Input ambiguous or unsupported language. Safe prompting text returned; `technical_error=False`, `refusal_triggered=False`.
- **Policy Refusal (`refusal_triggered=True`):** Security policy violation (e.g. prompt injection). Safe refusal returned; `technical_error=False`, `clarification_needed=False`.
- **Technical Failure (`technical_error=True`):** Translation or downstream outage. Localized service notice returned; `refusal_triggered=False`, `clarification_needed=False`.

### 7.2. Fallback Tier Matrix

| Tier | Trigger Condition | System Behavior | HTTP Code | Non-Success Flags |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1 (Localized Notice)** | Inbound or outbound translation fails; target language $L$ is known | Return `LOCALIZED_SERVICE_NOTICES[L]` | 200 | `technical_error=True`, `error_code="TRANSLATION_UNAVAILABLE"` |
| **Tier 2 (Untranslated Notice)**| Translation fails and target language is undetermined | Return English notice prefixed with `[Service Notice: Untranslated]` | 200 | `technical_error=True`, `error_code="UNTRANSLATED_SYSTEM_FAILURE"` |
| **Tier 3 (Perimeter Refusal)** | Inbound prompt injection detected | Return localized perimeter refusal notice | 200 | `refusal_triggered=True`, `refusal_reason="PROMPT_INJECTION_DETECTED"` |
| **Tier 4 (Clarification)** | Low confidence ($< 0.85$) or unsupported language | Return localized clarification prompt | 200 | `clarification_needed=True`, `detected_language="und"` |

### 7.3. PII Safety in Fallbacks
Fallback service notices are static constants from `LOCALIZED_SERVICE_NOTICES`. They **never** interpolate user message text, entity values, or external exception strings.

---

## 8. Durable PostgreSQL Idempotency & Transactional Outbox Architecture

### 8.1. Outbox Durability & RESTRICT Deletion Behavior
- **`ON DELETE RESTRICT` Enforced:** Foreign keys on `customer_message_id` and `agent_message_id` enforce `ON DELETE RESTRICT`. Deleting a message while an outbox event references it is rejected by PostgreSQL.
- **Outbox Preservation:** `PENDING`, `IN_FLIGHT`, and `DEAD_LETTER` outbox rows are never pruned. Only `DELIVERED` outbox events older than 7 days are pruned.
- **Reconciliation on Unavailable Message:** If an outbox row is pruned and its message is unavailable, reconciliation marks the turn `UNAVAILABLE_HISTORICAL_TURN`, excludes it from the hot 10-turn window, logs an operational warning, and **never synthesizes fake message data**.

### 8.2. One Authoritative Sequence Source & Database-Controlled Atomic Allocator (`fn_allocate_next_turn_sequence`)
- `conversations.turn_count` in PostgreSQL is the **single, definitive, authoritative sequence counter**.
- **Database-Controlled Atomic Allocator:** Ordinary `app_user` has **zero direct UPDATE privileges on `conversations.turn_count`** (`REVOKE UPDATE (turn_count) ON conversations FROM app_user;`). Any attempt by `app_user` to execute `UPDATE conversations SET turn_count = ...` is rejected by PostgreSQL with `permission denied for column "turn_count"` (SQLSTATE `42501`).
- Sequence numbers can **only** be allocated by calling the database-controlled `SECURITY DEFINER` function:
  ```sql
  CREATE OR REPLACE FUNCTION fn_allocate_next_turn_sequence(p_conversation_id UUID)
  RETURNS INTEGER
  SECURITY DEFINER
  SET search_path = public, pg_temp
  LANGUAGE plpgsql AS $$
  DECLARE
      v_allocated_turn INT;
  BEGIN
      -- Atomic row lock and strictly monotonic increment
      UPDATE conversations
      SET turn_count = turn_count + 1,
          updated_at = NOW()
      WHERE id = p_conversation_id
      RETURNING turn_count INTO v_allocated_turn;

      IF v_allocated_turn IS NULL THEN
          RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', p_conversation_id;
      END IF;

      RETURN v_allocated_turn;
  END;
  $$;

  REVOKE ALL ON FUNCTION fn_allocate_next_turn_sequence(UUID) FROM PUBLIC;
  GRANT EXECUTE ON FUNCTION fn_allocate_next_turn_sequence(UUID) TO app_user;
  ALTER FUNCTION fn_allocate_next_turn_sequence(UUID) OWNER TO migration_admin;
  ```
- **Monotonicity Trigger on `conversations`:** Even inside the allocator or under elevated privileges, an additional trigger guarantees that `turn_count` can only increment by exactly 1:
  ```sql
  CREATE OR REPLACE FUNCTION fn_enforce_turn_count_monotonicity()
  RETURNS TRIGGER AS $$
  BEGIN
      IF NEW.turn_count IS DISTINCT FROM OLD.turn_count THEN
          IF NEW.turn_count != OLD.turn_count + 1 THEN
              RAISE EXCEPTION 'INVALID_TURN_COUNT_MUTATION: turn_count cannot be arbitrarily modified. It must increment by exactly 1 (from % to %)',
                  OLD.turn_count, NEW.turn_count;
          END IF;
      END IF;
      RETURN NEW;
  END;
  $$ LANGUAGE plpgsql;

  CREATE TRIGGER tg_enforce_turn_count_monotonicity
  BEFORE UPDATE OF turn_count ON conversations
  FOR EACH ROW EXECUTE FUNCTION fn_enforce_turn_count_monotonicity();
  ```
- In Transaction 2 (Tx2), the application calls:
  ```sql
  SELECT fn_allocate_next_turn_sequence(:conversation_id);
  ```
  The returned integer $S = \text{turn\_count}$ is assigned to `messages.turn_sequence_number` (customer and agent messages) and `chat_outbox_events.sequence_number`.
- Watermark $W_{auth}$ is **always and exclusively read from `conversations.turn_count`**:
  $$W_{auth} = \text{SELECT turn\_count FROM conversations WHERE id = :conversation\_id}$$

### 8.3. Strictly Monotonic Historical Backfill (Zero Manufactured Causality)
- **Removal of Sender-Role Priority:** Sender-role priority is completely eliminated as evidence of causal ordering.
- **Equal Timestamps Disqualified:** Equal `created_at` timestamps between messages are treated as fundamentally ambiguous. Random UUIDs (`uuid.uuid4`) are never used to break ties.
- **Strict Qualification Rules:** A conversation qualifies for backfill **if and only if**:
  1. Zero `SYSTEM` and zero `HUMAN_AGENT` messages exist.
  2. Exactly $K$ customer messages and $K$ agent messages ($K \ge 1$).
  3. Total message count equals `turn_count * 2`.
  4. Timestamps are **strictly monotonically increasing**:
     $$\text{created\_at}(m_1) < \text{created\_at}(m_2) < \dots < \text{created\_at}(m_{2K})$$
     Zero equal timestamps are permitted anywhere in the conversation.
  5. The sequence strictly alternates: $m_1$ is `CUSTOMER`, $m_2$ is `AI_AGENT`, $m_3$ is `CUSTOMER`, etc.
- All other conversations retain `turn_sequence_number = NULL` (Legacy Data).

### 8.4. Database Triggers for Message Sequence Allocation & Immutability

#### 8.4.1. Trigger 1: Sequence Allocation Validation on INSERT
Enforces that new messages cannot be inserted with an arbitrary sequence number:
```sql
CREATE OR REPLACE FUNCTION fn_validate_message_turn_sequence_insert()
RETURNS TRIGGER AS $$
DECLARE
    v_current_turn INT;
BEGIN
    IF NEW.turn_sequence_number IS NOT NULL THEN
        -- Only dialog messages can carry a sequence number
        IF NEW.sender_type NOT IN ('CUSTOMER', 'AI_AGENT') THEN
            RAISE EXCEPTION 'NON_DIALOG_MESSAGE_SEQUENCE_FORBIDDEN: sender_type % cannot have turn_sequence_number', NEW.sender_type;
        END IF;

        -- Verify sequence strictly matches conversations.turn_count
        SELECT turn_count INTO v_current_turn
        FROM conversations WHERE id = NEW.conversation_id;

        IF v_current_turn IS NULL THEN
            RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', NEW.conversation_id;
        END IF;

        IF NEW.turn_sequence_number != v_current_turn THEN
            RAISE EXCEPTION 'SEQUENCE_ALLOCATION_MISMATCH: turn_sequence_number % does not match conversations.turn_count %',
                NEW.turn_sequence_number, v_current_turn;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_validate_message_turn_sequence_insert
BEFORE INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION fn_validate_message_turn_sequence_insert();
```

#### 8.4.2. Trigger 2: Strict NULL-Safe Immutability on UPDATE and DELETE
Guarantees that once committed, numbered messages can never be modified or deleted:
```sql
CREATE OR REPLACE FUNCTION fn_enforce_message_turn_immutability()
RETURNS TRIGGER AS $$
BEGIN
    -- 1. Forbid deletion of numbered messages (even if outbox row pruned)
    IF TG_OP = 'DELETE' THEN
        IF OLD.turn_sequence_number IS NOT NULL THEN
            RAISE EXCEPTION 'NUMBERED_MESSAGE_DELETION_PROHIBITED: Cannot delete committed dialog message % with turn_sequence_number %',
                OLD.id, OLD.turn_sequence_number;
        END IF;
        RETURN OLD;
    END IF;

    -- 2. Forbid updating unsequenced messages to assign a sequence number post-migration
    IF OLD.turn_sequence_number IS NULL AND NEW.turn_sequence_number IS NOT NULL THEN
        RAISE EXCEPTION 'LEGACY_SEQUENCE_MUTATION_FORBIDDEN: Cannot assign turn_sequence_number to unsequenced message % post-migration', OLD.id;
    END IF;

    -- 3. Enforce strict immutability once turn_sequence_number is assigned
    IF OLD.turn_sequence_number IS NOT NULL THEN
        IF NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number THEN
            RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify turn_sequence_number on committed message % (from % to %)',
                OLD.id, OLD.turn_sequence_number, NEW.turn_sequence_number;
        END IF;
        IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id THEN
            RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot reassign conversation_id on committed message %', OLD.id;
        END IF;
        IF NEW.sender_type IS DISTINCT FROM OLD.sender_type THEN
            RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_type on committed message % (from % to %)',
                OLD.id, OLD.sender_type, NEW.sender_type;
        END IF;
        IF NEW.sender_id IS DISTINCT FROM OLD.sender_id THEN
            RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_id on committed message %', OLD.id;
        END IF;
        IF NEW.content IS DISTINCT FROM OLD.content THEN
            RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify content on committed message %', OLD.id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_enforce_message_turn_immutability
BEFORE UPDATE OR DELETE ON messages
FOR EACH ROW EXECUTE FUNCTION fn_enforce_message_turn_immutability();
```

#### 8.4.3. Trigger 3: Atomic Customer/AI Dialog Pair & Outbox Integrity (DEFERRABLE CONSTRAINT TRIGGER)

In PostgreSQL, statements inside a transaction execute sequentially. A standard `BEFORE INSERT` trigger evaluating the customer message cannot see the AI agent message before it is inserted. To guarantee that a turn cannot commit with an incomplete pair, a **`DEFERRABLE INITIALLY DEFERRED` constraint trigger** is enforced at transaction `COMMIT` time:

```sql
CREATE OR REPLACE FUNCTION fn_validate_conversation_turn_pair_completeness()
RETURNS TRIGGER AS $$
DECLARE
    v_cust_count INT;
    v_agent_count INT;
    v_turn_count INT;
    v_outbox_count INT;
BEGIN
    -- Only evaluate numbered dialog turns
    IF NEW.turn_sequence_number IS NOT NULL THEN
        -- 1. Verify exact customer/AI dialog pairing for this turn sequence
        SELECT 
            COUNT(*) FILTER (WHERE sender_type = 'CUSTOMER'),
            COUNT(*) FILTER (WHERE sender_type = 'AI_AGENT')
        INTO v_cust_count, v_agent_count
        FROM messages
        WHERE conversation_id = NEW.conversation_id
          AND turn_sequence_number = NEW.turn_sequence_number;

        IF v_cust_count != 1 OR v_agent_count != 1 THEN
            RAISE EXCEPTION 'INCOMPLETE_TURN_PAIR: Turn % in conversation % must contain exactly 1 CUSTOMER message and 1 AI_AGENT message at commit time (found % customer, % agent)',
                NEW.turn_sequence_number, NEW.conversation_id, v_cust_count, v_agent_count;
        END IF;

        -- 2. Verify sequence number matches allocated turn_count
        SELECT turn_count INTO v_turn_count
        FROM conversations
        WHERE id = NEW.conversation_id;

        IF v_turn_count IS NULL OR v_turn_count < NEW.turn_sequence_number THEN
            RAISE EXCEPTION 'UNALLOCATED_TURN_SEQUENCE: Turn % exceeds conversations.turn_count % in conversation %',
                NEW.turn_sequence_number, COALESCE(v_turn_count, -1), NEW.conversation_id;
        END IF;

        -- 3. Verify exactly 1 chat_outbox_events record exists for this turn sequence
        SELECT COUNT(*) INTO v_outbox_count
        FROM chat_outbox_events
        WHERE conversation_id = NEW.conversation_id
          AND sequence_number = NEW.turn_sequence_number;

        IF v_outbox_count != 1 THEN
            RAISE EXCEPTION 'MISSING_OUTBOX_EVENT: Turn % in conversation % must have exactly 1 chat_outbox_events record at commit time (found %)',
                NEW.turn_sequence_number, NEW.conversation_id, v_outbox_count;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER tg_enforce_turn_pair_completeness
AFTER INSERT OR UPDATE ON messages
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_validate_conversation_turn_pair_completeness();
```

**Atomic Invariants Enforced by `tg_enforce_turn_pair_completeness`:**
1. **No Lone Customer Messages:** If a transaction inserts a customer message and attempts to commit without the corresponding AI agent message, PostgreSQL aborts the transaction at commit time.
2. **No Lone AI Messages:** If a transaction inserts an AI agent message without the corresponding customer message, PostgreSQL aborts the transaction.
3. **No Unallocated Turns:** The turn sequence number cannot exceed `conversations.turn_count`.
4. **Mandatory Outbox Coupling:** A numbered turn cannot commit without its accompanying `chat_outbox_events` record referencing both message IDs.
5. **ACID Rollback Boundary:** If any statement in Tx2 fails, or if the connection is dropped, PostgreSQL rolls back all inserts and updates. Zero messages, zero turn increments, zero outbox events, and zero Redis writes occur.

#### 8.4.4. Trigger 4: Monotonic Turn Count Trigger on `conversations`
Guarantees that `turn_count` can never jump or decrement:
```sql
CREATE OR REPLACE FUNCTION fn_enforce_turn_count_monotonicity()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.turn_count IS DISTINCT FROM OLD.turn_count THEN
        IF NEW.turn_count != OLD.turn_count + 1 THEN
            RAISE EXCEPTION 'INVALID_TURN_COUNT_MUTATION: turn_count cannot be arbitrarily modified. It must increment by exactly 1 (from % to %)',
                OLD.turn_count, NEW.turn_count;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_enforce_turn_count_monotonicity
BEFORE UPDATE OF turn_count ON conversations
FOR EACH ROW EXECUTE FUNCTION fn_enforce_turn_count_monotonicity();
```

#### 8.4.5. Trigger 5: Chat Idempotency Record Immutability & Deletion Prohibition
Guarantees that idempotency records cannot be deleted or arbitrarily modified after completion:
```sql
CREATE OR REPLACE FUNCTION fn_enforce_idempotency_record_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDEMPOTENCY_DELETION_PROHIBITED: Deleting chat_idempotency_records is strictly forbidden';
    END IF;

    -- Once COMPLETED, record is permanently frozen
    IF OLD.status = 'COMPLETED' THEN
        IF NEW.status IS DISTINCT FROM OLD.status OR
           NEW.response_payload IS DISTINCT FROM OLD.response_payload OR
           NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number OR
           NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
           NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id THEN
            RAISE EXCEPTION 'COMPLETED_IDEMPOTENCY_IMMUTABLE: Completed idempotency record % cannot be mutated', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_enforce_idempotency_record_immutability
BEFORE UPDATE OR DELETE ON chat_idempotency_records
FOR EACH ROW EXECUTE FUNCTION fn_enforce_idempotency_record_immutability();
```

#### 8.4.6. Trigger 6: Chat Outbox Event Immutability & Deletion Prohibition
Guarantees that outbox events cannot be deleted by application sessions and core headers cannot be tampered with:
```sql
CREATE OR REPLACE FUNCTION fn_enforce_outbox_event_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'OUTBOX_DELETION_PROHIBITED: Deleting chat_outbox_events is strictly forbidden. Use retention archival.';
    END IF;

    -- Prevent modifying immutable event headers and payload
    IF NEW.event_id IS DISTINCT FROM OLD.event_id OR
       NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
       NEW.conversation_id IS DISTINCT FROM OLD.conversation_id OR
       NEW.sequence_number IS DISTINCT FROM OLD.sequence_number OR
       NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
       NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id OR
       NEW.payload IS DISTINCT FROM OLD.payload OR
       NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'OUTBOX_IMMUTABILITY_VIOLATION: Core event headers and payload on outbox event % cannot be mutated', OLD.event_id;
    END IF;

    -- Once DELIVERED, status cannot regress
    IF OLD.status = 'DELIVERED' AND NEW.status != 'DELIVERED' THEN
        RAISE EXCEPTION 'DELIVERED_OUTBOX_REGRESSION_FORBIDDEN: Event % is already delivered', OLD.event_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_enforce_outbox_event_immutability
BEFORE UPDATE OR DELETE ON chat_outbox_events
FOR EACH ROW EXECUTE FUNCTION fn_enforce_outbox_event_immutability();
```

---

### 8.5. Global Sequence Continuity ($W_{global\_contig}$) vs. Retained-Window Continuity ($W_{window\_contig}$)

To eliminate ambiguity between database continuity and cache sliding window completeness, the architecture formally separates:

1. **Global Sequence Continuity ($W_{global\_contig}$ in PostgreSQL):**
   - $W_{global\_contig} \equiv \text{conversations.turn\_count}$: The authoritative monotonic sequence count in PostgreSQL.
   - Because sequence allocation and customer/agent message persistence commit atomically within Tx2, PostgreSQL is **always 100% gapless**:
     $$\forall s \in [1, W_{global\_contig}], \quad \exists \text{ exactly one customer message and one AI message with } \text{turn\_sequence\_number} = s$$
2. **Retained-Window Continuity ($W_{window\_contig}$ in Redis):**
   - Retained Window Start: $\text{window\_start} = \max(1, W_{max} - 9)$.
   - Expected Window: $[\text{window\_start}, W_{max}]$.
   - $W_{window\_contig}$ is the highest sequence number $c \in [\text{window\_start}, W_{max}]$ such that **all sequences from $\text{window\_start}$ to $c$ are contiguous and present in Redis**.
   - If $\text{window\_start}$ is missing: $W_{window\_contig} = \text{window\_start} - 1$.
   - **Completeness Condition:** The retained window is complete **if and only if $W_{window\_contig} == W_{max}$**.
   - **Intentional Eviction vs. Gaps:** Absence of turns $< \text{window\_start}$ is recognized as normal sliding window eviction and is never flagged as a gap.

#### 8.5.1. Dual-Watermark Redis Lua Script (`apply_turn_to_conversation_timeline`):
```lua
-- KEYS[1] = conv:{conv_id}:timeline (ZSET)
-- KEYS[2] = conv:{conv_id}:turn_payloads (HASH)
-- KEYS[3] = conv:{conv_id}:seen_events (SET)
-- KEYS[4] = conv:{conv_id}:watermark (STRING - W_max)
-- KEYS[5] = conv:{conv_id}:window_contiguous_watermark (STRING - W_window_contig)
-- ARGV[1] = event_id (str)
-- ARGV[2] = sequence_number (int)
-- ARGV[3] = serialized_payload (json)
-- ARGV[4] = expected_event_id (str - deterministic UUIDv5)
-- ARGV[5] = ttl_seconds (86400)

local event_id = ARGV[1]
local seq_num = tonumber(ARGV[2])
local payload = ARGV[3]
local expected_id = ARGV[4]
local ttl = tonumber(ARGV[5])

-- 1. Identity Validation
if event_id ~= expected_id then
    return {0, "IDENTITY_MISMATCH"}
end

-- 2. Idempotency Check
if redis.call('SISMEMBER', KEYS[3], event_id) == 1 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
    redis.call('EXPIRE', KEYS[3], ttl)
    redis.call('EXPIRE', KEYS[4], ttl)
    redis.call('EXPIRE', KEYS[5], ttl)
    return {1, "ALREADY_PRESENT"}
end

-- 3. Watermark Existence Check
local raw_w = redis.call('GET', KEYS[4])
if not raw_w then
    return {0, "UNINITIALIZED_WATERMARK_REQUIRED"}
end
local w_max = tonumber(raw_w)
local window_start = math.max(1, w_max - 9)

-- 4. Stale Event Rejection (below window)
if seq_num < window_start then
    redis.call('SADD', KEYS[3], event_id)
    redis.call('EXPIRE', KEYS[3], ttl)
    return {1, "STALE_OUTSIDE_WINDOW"}
end

-- 5. Insert Event into Buffer
redis.call('ZADD', KEYS[1], seq_num, event_id)
redis.call('HSET', KEYS[2], event_id, payload)
redis.call('SADD', KEYS[3], event_id)

-- 6. Update W_max
if seq_num > w_max then
    w_max = seq_num
    window_start = math.max(1, w_max - 9)
    redis.call('SET', KEYS[4], tostring(w_max), 'EX', ttl)
end

-- 7. Recalculate W_window_contig strictly from window_start
local w_contig = window_start - 1
while true do
    local next_score = w_contig + 1
    if next_score > w_max then
        break
    end
    local found = redis.call('ZRANGEBYSCORE', KEYS[1], next_score, next_score)
    if #found > 0 then
        w_contig = next_score
    else
        break
    end
end
redis.call('SET', KEYS[5], tostring(w_contig), 'EX', ttl)

-- 8. Evict Turns Falling Outside Sliding Window [window_start, w_max]
local to_evict = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', window_start - 1)
for _, old_id in ipairs(to_evict) do
    redis.call('HDEL', KEYS[2], old_id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', window_start - 1)

-- 9. Refresh TTLs
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
redis.call('EXPIRE', KEYS[5], ttl)

if w_contig < w_max then
    return {1, "APPLIED_WITH_GAP"}
else
    return {1, "APPLIED_CONTIGUOUS"}
end
```

---

### 8.6. Canonical UUIDv5 Compatibility Contract

1. **Immutable Namespace Constant:**
   ```python
   CHAT_OUTBOX_NAMESPACE_V1 = uuid.UUID("e8a56f2e-4b71-4a9c-9c32-84183d8e52a9")
   ```
2. **Canonical String Construction:**
   ```python
   def generate_outbox_event_id(conversation_id: Union[uuid.UUID, str], turn_sequence_number: int) -> uuid.UUID:
       canon_conv = str(uuid.UUID(str(conversation_id))).lower()
       canon_seq = int(turn_sequence_number)
       name_string = f"v1:{canon_conv}:{canon_seq}"
       return uuid.uuid5(CHAT_OUTBOX_NAMESPACE_V1, name_string)
   ```
3. **Outbox Primary Key Confirmation:**
   - **The outbox primary key (`chat_outbox_events.id`) directly uses this deterministic UUID string.**
4. **Golden Vector Drift Protection:**
   - Test asserts: `str(generate_outbox_event_id("12345678-1234-5678-1234-567812345678", 1))` strictly equals `"5d56372a-7576-5454-856a-602a0895e80c"`.

---

### 8.7. Alembic Migration Script (`backend/alembic/versions/002_chat_idempotency_and_outbox.py`)

```python
"""add chat_idempotency_records, chat_outbox_events tables, messages.turn_sequence_number, and triggers

Revision ID: 002_chat_idempotency_and_outbox
Revises: 001_initial_schema
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa
from app.db.base import JSONBType

revision = "002_chat_idempotency_and_outbox"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 1. messages: add turn_sequence_number and composite unique index for composite FKs
    op.add_column("messages", sa.Column("turn_sequence_number", sa.Integer(), nullable=True))
    op.create_index("ix_messages_turn_seq", "messages", ["conversation_id", "turn_sequence_number"])
    op.create_index("uq_messages_id_conversation", "messages", ["id", "conversation_id"], unique=True)
    
    # Partial unique index: at most one customer and one AI agent message per turn
    op.create_index(
        "uq_messages_conv_turn_sender",
        "messages",
        ["conversation_id", "turn_sequence_number", "sender_type"],
        unique=True,
        postgresql_where=sa.text("turn_sequence_number IS NOT NULL"),
    )

    # 2. Historical Message Backfill (Executed BEFORE triggers are installed)
    op.execute("""
    WITH candidate_conversations AS (
        SELECT conversation_id
        FROM messages
        GROUP BY conversation_id
        HAVING 
            COUNT(*) FILTER (WHERE sender_type NOT IN ('CUSTOMER', 'AI_AGENT')) = 0
            AND COUNT(*) FILTER (WHERE sender_type = 'CUSTOMER') = COUNT(*) FILTER (WHERE sender_type = 'AI_AGENT')
            AND COUNT(*) = (SELECT turn_count * 2 FROM conversations WHERE id = messages.conversation_id)
            AND COUNT(DISTINCT created_at) = COUNT(*)
    ),
    ordered_turns AS (
        SELECT 
            m.id,
            m.conversation_id,
            m.sender_type,
            m.created_at,
            ROW_NUMBER() OVER (PARTITION BY m.conversation_id ORDER BY m.created_at ASC) AS row_num
        FROM messages m
        WHERE m.conversation_id IN (SELECT conversation_id FROM candidate_conversations)
    ),
    verified_conversations AS (
        SELECT conversation_id
        FROM ordered_turns
        GROUP BY conversation_id
        HAVING 
            COUNT(*) FILTER (WHERE (row_num % 2 = 1 AND sender_type != 'CUSTOMER') OR (row_num % 2 = 0 AND sender_type != 'AI_AGENT')) = 0
    )
    UPDATE messages m
    SET turn_sequence_number = CEIL(ot.row_num / 2.0)::integer
    FROM ordered_turns ot
    WHERE m.id = ot.id
      AND m.conversation_id IN (SELECT conversation_id FROM verified_conversations);
    """)

    # 3. Message Sequence Insertion Validation Trigger
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_message_turn_sequence_insert()
    RETURNS TRIGGER AS $$
    DECLARE
        v_current_turn INT;
    BEGIN
        IF NEW.turn_sequence_number IS NOT NULL THEN
            IF NEW.sender_type NOT IN ('CUSTOMER', 'AI_AGENT') THEN
                RAISE EXCEPTION 'NON_DIALOG_MESSAGE_SEQUENCE_FORBIDDEN: sender_type % cannot have turn_sequence_number', NEW.sender_type;
            END IF;

            SELECT turn_count INTO v_current_turn
            FROM conversations WHERE id = NEW.conversation_id;

            IF v_current_turn IS NULL THEN
                RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', NEW.conversation_id;
            END IF;

            IF NEW.turn_sequence_number != v_current_turn THEN
                RAISE EXCEPTION 'SEQUENCE_ALLOCATION_MISMATCH: turn_sequence_number % does not match conversations.turn_count %',
                    NEW.turn_sequence_number, v_current_turn;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_validate_message_turn_sequence_insert
    BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION fn_validate_message_turn_sequence_insert();
    """)

    # 4. Message Immutability & Deletion Prohibition Trigger (NULL-Safe via IS DISTINCT FROM)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_message_turn_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        -- Forbid deleting numbered messages
        IF TG_OP = 'DELETE' THEN
            IF OLD.turn_sequence_number IS NOT NULL THEN
                RAISE EXCEPTION 'NUMBERED_MESSAGE_DELETION_PROHIBITED: Cannot delete committed dialog message % with turn_sequence_number %',
                    OLD.id, OLD.turn_sequence_number;
            END IF;
            RETURN OLD;
        END IF;

        -- Forbid assigning sequence numbers to legacy unsequenced messages post-migration
        IF OLD.turn_sequence_number IS NULL AND NEW.turn_sequence_number IS NOT NULL THEN
            RAISE EXCEPTION 'LEGACY_SEQUENCE_MUTATION_FORBIDDEN: Cannot assign turn_sequence_number to unsequenced message % post-migration', OLD.id;
        END IF;

        -- Enforce strict immutability once turn_sequence_number is assigned
        IF OLD.turn_sequence_number IS NOT NULL THEN
            IF NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify turn_sequence_number on committed message % (from % to %)',
                    OLD.id, OLD.turn_sequence_number, NEW.turn_sequence_number;
            END IF;
            IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot reassign conversation_id on committed message %', OLD.id;
            END IF;
            IF NEW.sender_type IS DISTINCT FROM OLD.sender_type THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_type on committed message % (from % to %)',
                    OLD.id, OLD.sender_type, NEW.sender_type;
            END IF;
            IF NEW.sender_id IS DISTINCT FROM OLD.sender_id THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_id on committed message %', OLD.id;
            END IF;
            IF NEW.content IS DISTINCT FROM OLD.content THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify content on committed message %', OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_enforce_message_turn_immutability
    BEFORE UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_message_turn_immutability();
    """)

    # 5. chat_idempotency_records
    op.create_table(
        "chat_idempotency_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted_response_payload", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_chat_idempotency_cust_sess_key",
        "chat_idempotency_records",
        ["customer_id", "session_id", "idempotency_key"],
    )
    op.create_index("ix_chat_idempotency_cust_sess", "chat_idempotency_records", ["customer_id", "session_id"])
    op.create_index("ix_chat_idempotency_conv_id", "chat_idempotency_records", ["conversation_id"])
    op.create_index("ix_chat_idempotency_lease", "chat_idempotency_records", ["status", "lease_expires_at"])
    op.create_index("ix_chat_idempotency_expires", "chat_idempotency_records", ["expires_at"])

    # 6. chat_outbox_events with RESTRICT Foreign Keys
    op.create_table(
        "chat_outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_message_id", sa.String(36), nullable=False),
        sa.Column("agent_message_id", sa.String(36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False, server_default="CONVERSATION_TURN_COMPLETED"),
        sa.Column("payload", JSONBType, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_cust_msg_conv",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_agent_msg_conv",
            ondelete="RESTRICT",
        ),
    )
    op.create_unique_constraint(
        "uq_chat_outbox_conv_seq",
        "chat_outbox_events",
        ["conversation_id", "sequence_number"],
    )
    op.create_unique_constraint("uq_chat_outbox_cust_msg", "chat_outbox_events", ["customer_message_id"])
    op.create_unique_constraint("uq_chat_outbox_agent_msg", "chat_outbox_events", ["agent_message_id"])
    op.create_index("ix_chat_outbox_status_retry", "chat_outbox_events", ["status", "next_retry_at"])
    op.create_index("ix_chat_outbox_conv_seq", "chat_outbox_events", ["conversation_id", "sequence_number"])
    op.create_index("ix_chat_outbox_cust_sess", "chat_outbox_events", ["customer_id", "session_id"])

    # 7. Outbox Trigger: Validate Message Linkage, Sender Types, and Turn Sequences
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_chat_outbox_turn()
    RETURNS TRIGGER AS $$
    DECLARE
        v_cust_sender VARCHAR(20);
        v_cust_conv VARCHAR(36);
        v_cust_seq INT;
        v_agent_sender VARCHAR(20);
        v_agent_conv VARCHAR(36);
        v_agent_seq INT;
    BEGIN
        SELECT sender_type, conversation_id, turn_sequence_number
        INTO v_cust_sender, v_cust_conv, v_cust_seq
        FROM messages WHERE id = NEW.customer_message_id;

        IF v_cust_sender IS NULL THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id % does not exist', NEW.customer_message_id;
        END IF;
        IF v_cust_sender != 'CUSTOMER' THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id % has sender_type %, expected CUSTOMER', NEW.customer_message_id, v_cust_sender;
        END IF;
        IF v_cust_conv != NEW.conversation_id THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id belongs to conv %, expected %', v_cust_conv, NEW.conversation_id;
        END IF;
        IF v_cust_seq != NEW.sequence_number THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id has turn_seq %, expected %', v_cust_seq, NEW.sequence_number;
        END IF;

        SELECT sender_type, conversation_id, turn_sequence_number
        INTO v_agent_sender, v_agent_conv, v_agent_seq
        FROM messages WHERE id = NEW.agent_message_id;

        IF v_agent_sender IS NULL THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id % does not exist', NEW.agent_message_id;
        END IF;
        IF v_agent_sender != 'AI_AGENT' THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id % has sender_type %, expected AI_AGENT', NEW.agent_message_id, v_agent_sender;
        END IF;
        IF v_agent_conv != NEW.conversation_id THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id belongs to conv %, expected %', v_agent_conv, NEW.conversation_id;
        END IF;
        IF v_agent_seq != NEW.sequence_number THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id has turn_seq %, expected %', v_agent_seq, NEW.sequence_number;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_validate_chat_outbox_turn
    BEFORE INSERT OR UPDATE ON chat_outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_validate_chat_outbox_turn();
    """)

    # 8. Atomic Customer/AI Dialog Pair & Outbox Integrity Constraint Trigger (DEFERRABLE)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_conversation_turn_pair_completeness()
    RETURNS TRIGGER AS $$
    DECLARE
        v_cust_count INT;
        v_agent_count INT;
        v_turn_count INT;
        v_outbox_count INT;
    BEGIN
        IF NEW.turn_sequence_number IS NOT NULL THEN
            -- 1. Verify exact customer/AI dialog pairing for this turn sequence
            SELECT 
                COUNT(*) FILTER (WHERE sender_type = 'CUSTOMER'),
                COUNT(*) FILTER (WHERE sender_type = 'AI_AGENT')
            INTO v_cust_count, v_agent_count
            FROM messages
            WHERE conversation_id = NEW.conversation_id
              AND turn_sequence_number = NEW.turn_sequence_number;

            IF v_cust_count != 1 OR v_agent_count != 1 THEN
                RAISE EXCEPTION 'INCOMPLETE_TURN_PAIR: Turn % in conversation % must contain exactly 1 CUSTOMER message and 1 AI_AGENT message at commit time (found % customer, % agent)',
                    NEW.turn_sequence_number, NEW.conversation_id, v_cust_count, v_agent_count;
            END IF;

            -- 2. Verify sequence number matches allocated turn_count
            SELECT turn_count INTO v_turn_count
            FROM conversations
            WHERE id = NEW.conversation_id;

            IF v_turn_count IS NULL OR v_turn_count < NEW.turn_sequence_number THEN
                RAISE EXCEPTION 'UNALLOCATED_TURN_SEQUENCE: Turn % exceeds conversations.turn_count % in conversation %',
                    NEW.turn_sequence_number, COALESCE(v_turn_count, -1), NEW.conversation_id;
            END IF;

            -- 3. Verify exactly 1 chat_outbox_events record exists for this turn sequence
            SELECT COUNT(*) INTO v_outbox_count
            FROM chat_outbox_events
            WHERE conversation_id = NEW.conversation_id
              AND sequence_number = NEW.turn_sequence_number;

            IF v_outbox_count != 1 THEN
                RAISE EXCEPTION 'MISSING_OUTBOX_EVENT: Turn % in conversation % must have exactly 1 chat_outbox_events record at commit time (found %)',
                    NEW.turn_sequence_number, NEW.conversation_id, v_outbox_count;
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE CONSTRAINT TRIGGER tg_enforce_turn_pair_completeness
    AFTER INSERT OR UPDATE ON messages
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_conversation_turn_pair_completeness();
    """)

    # 9. Atomic Turn Sequence Allocator (SECURITY DEFINER)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_allocate_next_turn_sequence(p_conversation_id UUID)
    RETURNS INTEGER
    SECURITY DEFINER
    SET search_path = public, pg_temp
    LANGUAGE plpgsql AS $$
    DECLARE
        v_allocated_turn INT;
    BEGIN
        UPDATE conversations
        SET turn_count = turn_count + 1,
            updated_at = NOW()
        WHERE id = p_conversation_id
        RETURNING turn_count INTO v_allocated_turn;

        IF v_allocated_turn IS NULL THEN
            RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', p_conversation_id;
        END IF;

        RETURN v_allocated_turn;
    END;
    $$;
    """)

    # 10. Monotonic Turn Count Trigger on conversations
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_turn_count_monotonicity()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.turn_count IS DISTINCT FROM OLD.turn_count THEN
            IF NEW.turn_count != OLD.turn_count + 1 THEN
                RAISE EXCEPTION 'INVALID_TURN_COUNT_MUTATION: turn_count cannot be arbitrarily modified. It must increment by exactly 1 (from % to %)',
                    OLD.turn_count, NEW.turn_count;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_enforce_turn_count_monotonicity
    BEFORE UPDATE OF turn_count ON conversations
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_turn_count_monotonicity();
    """)

    # 11. Immutability & Deletion Prohibition on chat_idempotency_records
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_idempotency_record_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'IDEMPOTENCY_DELETION_PROHIBITED: Deleting chat_idempotency_records is strictly forbidden';
        END IF;

        IF OLD.status = 'COMPLETED' THEN
            IF NEW.status IS DISTINCT FROM OLD.status OR
               NEW.response_payload IS DISTINCT FROM OLD.response_payload OR
               NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number OR
               NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
               NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id THEN
                RAISE EXCEPTION 'COMPLETED_IDEMPOTENCY_IMMUTABLE: Completed idempotency record % cannot be mutated', OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_enforce_idempotency_record_immutability
    BEFORE UPDATE OR DELETE ON chat_idempotency_records
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_idempotency_record_immutability();
    """)

    # 12. Immutability & Deletion Prohibition on chat_outbox_events
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_outbox_event_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'OUTBOX_DELETION_PROHIBITED: Deleting chat_outbox_events is strictly forbidden. Use retention archival.';
        END IF;

        IF NEW.event_id IS DISTINCT FROM OLD.event_id OR
           NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
           NEW.conversation_id IS DISTINCT FROM OLD.conversation_id OR
           NEW.sequence_number IS DISTINCT FROM OLD.sequence_number OR
           NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
           NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id OR
           NEW.payload IS DISTINCT FROM OLD.payload OR
           NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'OUTBOX_IMMUTABILITY_VIOLATION: Core event headers and payload on outbox event % cannot be mutated', OLD.event_id;
        END IF;

        IF OLD.status = 'DELIVERED' AND NEW.status != 'DELIVERED' THEN
            RAISE EXCEPTION 'DELIVERED_OUTBOX_REGRESSION_FORBIDDEN: Event % is already delivered', OLD.event_id;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_enforce_outbox_event_immutability
    BEFORE UPDATE OR DELETE ON chat_outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_outbox_event_immutability();
    """)

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_outbox_event_immutability ON chat_outbox_events;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_outbox_event_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_idempotency_record_immutability ON chat_idempotency_records;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_idempotency_record_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_turn_count_monotonicity ON conversations;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_turn_count_monotonicity();")
    op.execute("DROP FUNCTION IF EXISTS fn_allocate_next_turn_sequence(UUID);")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_turn_pair_completeness ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_conversation_turn_pair_completeness();")
    op.execute("DROP TRIGGER IF EXISTS tg_validate_chat_outbox_turn ON chat_outbox_events;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_chat_outbox_turn();")
    op.drop_table("chat_outbox_events")
    op.drop_table("chat_idempotency_records")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_message_turn_immutability ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_message_turn_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_validate_message_turn_sequence_insert ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_message_turn_sequence_insert();")
    op.drop_index("uq_messages_conv_turn_sender", table_name="messages")
    op.drop_index("uq_messages_id_conversation", table_name="messages")
    op.drop_index("ix_messages_turn_seq", table_name="messages")
    op.drop_column("messages", "turn_sequence_number")
```

---

### 8.8. Table-Specific Least-Privilege Grants & Migration Connection Configuration

To ensure that ordinary application sessions cannot disable triggers, modify schema, delete audit records, or tamper with sequence and outbox mechanisms, blanket CRUD grants are completely replaced by **explicit, table-specific, least-privilege grants**.

#### 8.8.1. Table-Specific Least-Privilege Role DDL

```sql
-- 1. Create runtime application role (least privilege)
CREATE ROLE app_user WITH 
    LOGIN 
    PASSWORD 'app_user_secure_password' 
    NOSUPERUSER 
    NOCREATEDB 
    NOCREATEROLE 
    NOINHERIT 
    NOREPLICATION;

-- 2. Create migration administrator role (schema owner)
CREATE ROLE migration_admin WITH 
    LOGIN 
    PASSWORD 'migration_admin_secure_password' 
    NOSUPERUSER 
    CREATEDB 
    NOCREATEROLE 
    INHERIT 
    NOREPLICATION;

-- 3. Database connection privileges
REVOKE ALL ON DATABASE ai_customer_support FROM PUBLIC;
GRANT CONNECT ON DATABASE ai_customer_support TO app_user;
GRANT CONNECT, CREATE ON DATABASE ai_customer_support TO migration_admin;

-- 4. Schema privileges
GRANT USAGE ON SCHEMA public TO app_user;
GRANT ALL ON SCHEMA public TO migration_admin;

-- 5. Table ownership: All tables owned exclusively by migration_admin
ALTER TABLE conversations OWNER TO migration_admin;
ALTER TABLE messages OWNER TO migration_admin;
ALTER TABLE chat_idempotency_records OWNER TO migration_admin;
ALTER TABLE chat_outbox_events OWNER TO migration_admin;
ALTER TABLE customers OWNER TO migration_admin;
ALTER TABLE sessions OWNER TO migration_admin;
ALTER TABLE users OWNER TO migration_admin;
ALTER TABLE roles OWNER TO migration_admin;
ALTER TABLE documents OWNER TO migration_admin;
ALTER TABLE document_chunks OWNER TO migration_admin;
ALTER TABLE document_versions OWNER TO migration_admin;
ALTER TABLE knowledge_indexes OWNER TO migration_admin;
ALTER TABLE tickets OWNER TO migration_admin;
ALTER TABLE sla_calendars OWNER TO migration_admin;
ALTER TABLE escalation_audits OWNER TO migration_admin;
ALTER TABLE evidence_files OWNER TO migration_admin;
ALTER TABLE evidence_verifications OWNER TO migration_admin;
ALTER TABLE extracted_evidence_entities OWNER TO migration_admin;
ALTER TABLE benchmark_runs OWNER TO migration_admin;
ALTER TABLE sentiment_analysis_runs OWNER TO migration_admin;

-- 6. Table-Specific Grants for app_user (REPLACING BLANKET CRUD):

-- a) messages: Append-Only (SELECT, INSERT). ZERO UPDATE, ZERO DELETE!
GRANT SELECT, INSERT ON messages TO app_user;
REVOKE UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON messages FROM app_user;

-- b) conversations: SELECT, INSERT, Restricted Column-Level UPDATE. ZERO DELETE!
-- turn_count UPDATE is REVOKED; turn_count is allocated ONLY via fn_allocate_next_turn_sequence
GRANT SELECT, INSERT ON conversations TO app_user;
GRANT UPDATE (status, customer_id, session_id, updated_at) ON conversations TO app_user;
REVOKE UPDATE (turn_count), DELETE, TRUNCATE, TRIGGER, REFERENCES ON conversations FROM app_user;

-- c) chat_idempotency_records: SELECT, INSERT, Column-Level UPDATE for worker lifecycle. ZERO DELETE!
GRANT SELECT, INSERT ON chat_idempotency_records TO app_user;
GRANT UPDATE (status, response_status_code, response_payload, customer_message_id, agent_message_id, turn_sequence_number, owner_token, lease_expires_at, heartbeat_at, failure_reason, attempt_count, updated_at) ON chat_idempotency_records TO app_user;
REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON chat_idempotency_records FROM app_user;

-- d) chat_outbox_events: SELECT, INSERT, Column-Level UPDATE for worker delivery transitions. ZERO DELETE!
-- Immutable headers (event_id, idempotency_key, sequence_number, payload, etc.) cannot be altered
GRANT SELECT, INSERT ON chat_outbox_events TO app_user;
GRANT UPDATE (status, attempt_count, next_retry_at, delivered_at, last_error) ON chat_outbox_events TO app_user;
REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON chat_outbox_events FROM app_user;

-- e) Operational transactional entities: customers, sessions, tickets, escalation_audits
GRANT SELECT, INSERT, UPDATE ON customers, sessions, tickets, escalation_audits TO app_user;
REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON customers, sessions, tickets, escalation_audits FROM app_user;

-- f) Read-only lookup tables: roles, knowledge_indexes, documents, document_chunks, document_versions, sla_calendars
GRANT SELECT ON roles, knowledge_indexes, documents, document_chunks, document_versions, sla_calendars TO app_user;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON roles, knowledge_indexes, documents, document_chunks, document_versions, sla_calendars FROM app_user;

-- 7. Allocator Function Execution Gateway:
REVOKE ALL ON FUNCTION fn_allocate_next_turn_sequence(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_allocate_next_turn_sequence(UUID) TO app_user;

-- 8. Sequences:
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
```

#### 8.8.2. Inviolable Security Boundaries Enforced by PostgreSQL Engine

| Operation Attempted by `app_user` | SQL Command | PostgreSQL Engine Response | Security Guarantee |
| :--- | :--- | :--- | :--- |
| **Direct `turn_count` Mutation** | `UPDATE conversations SET turn_count = 99 WHERE id = :id;` | `ERROR: permission denied for column "turn_count" of relation "conversations"` (SQLSTATE `42501`) | `app_user` cannot arbitrarily set turn sequences. |
| **Arbitrary Turn Increment** | `UPDATE conversations SET turn_count = turn_count + 5;` | `ERROR: INVALID_TURN_COUNT_MUTATION` (via trigger) | Turn count can only increment by 1. |
| **Authorized Turn Allocation** | `SELECT fn_allocate_next_turn_sequence(:id);` | Returns `turn_count + 1` | Safe, database-controlled serialized allocation. |
| **Delete from Outbox** | `DELETE FROM chat_outbox_events WHERE event_id = :id;` | `ERROR: permission denied for table chat_outbox_events` (SQLSTATE `42501`) | Outbox audit trail cannot be pruned by application sessions. |
| **Mutate Outbox Core Headers** | `UPDATE chat_outbox_events SET sequence_number = 99;` | `ERROR: permission denied for column "sequence_number"` (SQLSTATE `42501`) | Event headers and payloads are immutable. |
| **Delete Idempotency Record** | `DELETE FROM chat_idempotency_records WHERE id = :id;` | `ERROR: permission denied for table chat_idempotency_records` (SQLSTATE `42501`) | Idempotency history cannot be purged by application. |
| **Mutate Completed Idempotency** | `UPDATE chat_idempotency_records SET status = 'IN_PROGRESS' WHERE status = 'COMPLETED';` | `ERROR: COMPLETED_IDEMPOTENCY_IMMUTABLE` (via trigger) | Completed responses cannot be altered. |
| **Disable Triggers** | `ALTER TABLE messages DISABLE TRIGGER ALL;` | `ERROR: must be owner of table messages` (SQLSTATE `42501`) | `app_user` cannot disable or bypass immutability triggers. |
| **Drop Trigger** | `DROP TRIGGER tg_enforce_turn_pair_completeness ON messages;` | `ERROR: must be owner of table messages` (SQLSTATE `42501`) | Triggers cannot be dropped by application sessions. |
| **Delete Numbered Message** | `DELETE FROM messages WHERE id = :id;` | `ERROR: permission denied for table messages` (SQLSTATE `42501`) | Dialog messages are append-only. |

#### 8.8.3. Verified Migration Connection Configuration

- **Current Baseline State:**
  In `backend/alembic/env.py` lines 34–37:
  ```python
  if not config.get_main_option("sqlalchemy.url"):
      db_url = settings.async_database_url
      if db_url:
          config.set_main_option("sqlalchemy.url", db_url)
  ```
  `settings.async_database_url` connects with `settings.POSTGRES_USER` (`postgres`).
- **Required Codebase Updates upon Authorization:**
  1. In `backend/app/core/config.py`:
     - Add `MIGRATION_POSTGRES_USER: str = "migration_admin"`
     - Add `MIGRATION_POSTGRES_PASSWORD: str = "migration_admin_secure_password"`
     - Add `MIGRATION_DATABASE_URL: Optional[str] = None`
     - Add computed property:
       ```python
       @computed_field
       @property
       def migration_async_database_url(self) -> str:
           if self.MIGRATION_DATABASE_URL:
               return self.MIGRATION_DATABASE_URL
           user = quote_plus(self.MIGRATION_POSTGRES_USER)
           password = quote_plus(self.MIGRATION_POSTGRES_PASSWORD)
           return (
               f"postgresql+asyncpg://{user}:{password}@"
               f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
           )
       ```
  2. In `backend/alembic/env.py` line 35:
     ```python
     if not config.get_main_option("sqlalchemy.url"):
         db_url = settings.migration_async_database_url
         if db_url:
             config.set_main_option("sqlalchemy.url", db_url)
     ```
  3. **Non-Implementation Warning:**
     Role separation is **NOT currently active** in the repository. Do not claim role separation until implemented and verified via live tests.

---

## 9. Explicit Unresolved Architectural Risks & Deployment Blockers

### 9.1. Exact Remaining Implementation Blockers

Prior to executing migration `002_chat_idempotency_and_outbox.py` and implementing Phase 4 application code, the following three blockers must be resolved:

1. **User Dual Authorization Blocker:** Explicit authorization must be granted by the user to author migration `002_chat_idempotency_and_outbox.py` and implement Phase 4 application code.
2. **PostgreSQL Role Provisioning Blocker (Prerequisite DBA Task):** In the live PostgreSQL 16 server (`localhost:5432`), the database administrator must execute the role provisioning script (Section 8.8.1) to create `app_user` and `migration_admin` and set table ownership on all existing schema tables.
3. **Configuration & Alembic Connection Blocker:** `backend/app/core/config.py` and `.env` must be configured with `MIGRATION_POSTGRES_USER` and `MIGRATION_POSTGRES_PASSWORD`, and `backend/alembic/env.py` must be updated to bind to `settings.migration_async_database_url`.

### 9.2. Operational & Architectural Risk Matrix

| Risk / Requirement | Likelihood | Impact | Built-in Architectural Mitigation & Prerequisite |
| :--- | :--- | :--- | :--- |
| **1. Unenforced Role Separation (Blocker)** | High | High | Section 8.8 formally specifies the DDL, table-specific grants, and configuration separation. Role separation is **currently unimplemented** in the codebase and must be provisioned and verified before migration 002. |
| **2. Database Clock Drift vs. Worker Clock** | Low | Medium | All lease expiry checks execute in PostgreSQL using `NOW() + INTERVAL '30 SECONDS'` and `lease_expires_at > NOW()`. Worker local clocks are never used for lease evaluation. |
| **3. Extended Network Partition During Tx2 Finalization** | Low | High | If network disconnects after Tx2 commit but before Redis delivery, the outbox record remains `PENDING`. Outbox worker delivers turn to Redis upon reconnection. Atomic Lua deduplication prevents double application. |
| **4. Redis Eviction / OOM Under Extreme Turn Volume** | Low | High | Redis is configured with volatile-lru. Hot history keys have explicit 24h TTL. Full state is authoritatively persisted in PostgreSQL `messages` and reconstructed on cache miss via deterministic UUIDv5 event IDs. |
| **5. Long-Lived Stale Outbox Re-Drives After Pruning** | Low | Low | Outbox retention maintenance runs daily and strictly preserves any record with `status != 'DELIVERED'`. Unacknowledged turns are never pruned. |
| **6. Atomic Customer/AI Pair & Outbox Integrity (Resolved via Constraint Trigger)** | Resolved | High | Section 8.4.3 defines `tg_enforce_turn_pair_completeness` (`DEFERRABLE INITIALLY DEFERRED`). A conversation turn cannot commit with only the customer message or only the AI response. Both messages, `turn_count` increment, idempotency completion, and outbox event commit or roll back together. |

---

## 10. Comprehensive Test Matrix (123 Enumerated Mandatory Integration Test Areas)

All Phase 4 integration tests will be authored in a new test file:  
[`backend/tests/test_task6_phase4.py`](backend/tests/test_task6_phase4.py).  
All database concurrency, locking, heartbeat, CAS, outbox worker, and Redis timeline tests **run against real PostgreSQL and live Redis** via `test_postgres_integration` infrastructure.

The matrix contains **exactly 141 enumerated test areas**:

| # | Test Case Name | Target Behavior & Assertions | Expected Outcome |
| :--- | :--- | :--- | :--- |
| **1** | `test_spanish_chat_turn_success` | Inbound Spanish query translated to English, RAG resolved, outbound response translated to Spanish with inline citations preserved. | HTTP 200, `detected_language="es"`, citations intact, `context_restored=False`. |
| **2** | `test_french_chat_turn_success` | French query translated, answered, and localized to French. | HTTP 200, `detected_language="fr"`, citations intact. |
| **3** | `test_german_chat_turn_success` | German query translated, answered, and localized to German. | HTTP 200, `detected_language="de"`, citations intact. |
| **4** | `test_hindi_devanagari_chat_turn_success` | Hindi Devanagari query translated, answered, and localized to Hindi Devanagari. | HTTP 200, `detected_language="hi"`, script handled cleanly. |
| **5** | `test_hindi_hinglish_chat_turn_success` | Romanized Hinglish query detected as `hi`, translated, answered, and localized. | HTTP 200, `detected_language="hi"`, stopword lexicon matches. |
| **6** | `test_english_passthrough_backward_compat` | Inbound English query bypasses translation, executes RAG, returns English response matching Phase 4D contract exactly. | HTTP 200, `detected_language="en"`, 100% Phase 4D field parity. |
| **7** | `test_preferred_language_valid_override` | English query with `preferred_language="es"` answers in Spanish. | HTTP 200, `detected_language="en"`, `reply_text` in Spanish. |
| **8** | `test_preferred_language_invalid_rejected` | User supplies `preferred_language="invalid_code"`; rejected by Pydantic validator. | HTTP 422 Unprocessable Entity. |
| **9** | `test_low_confidence_triggers_clarification` | Ambiguous input with confidence $< 0.85$ triggers clarification. `preferred_language` is NOT used as proof of source language. | HTTP 200, `clarification_needed=True`, `refusal_triggered=False`, `technical_error=False`, `detected_language="und"`. |
| **10** | `test_unsupported_language_triggers_clarification` | Query in unsupported language (e.g. Russian, Japanese) returns clarification notice. | HTTP 200, `clarification_needed=True`, `refusal_triggered=False`, `technical_error=False`, `detected_language="und"`. |
| **11** | `test_non_english_prompt_injection_refusal` | Spanish prompt injection intercepted by perimeter security scanner. | HTTP 200, `refusal_triggered=True`, `refusal_reason="PROMPT_INJECTION_DETECTED"`, zero LLM calls. |
| **12** | `test_policy_refusal_versus_technical_failure_distinction` | Verifies mutual exclusivity of policy refusal vs. technical error vs. clarification. | Exact field assertion verifying mutually exclusive non-success flags. |
| **13** | `test_multilingual_entity_and_citation_preservation` | Query containing `ORDER-12345` and `$499.00` preserves identifiers; outbound answer preserves inline citation `[1]`. | Exact identifier and citation token equality verified. |
| **14** | `test_citation_parity_failure_fails_closed` | Translation model drops citation `[2]`. 4-point structural parity check fails. | Fails closed to `LOCALIZED_SERVICE_NOTICES[lang]`. `technical_error=True`, `refusal_triggered=False`. Zero heuristic guessing. |
| **15** | `test_inbound_translation_failure_degradation` | LLM timeout during translation returns localized service notice in customer's language. | HTTP 200, `technical_error=True`, `technical_error_code="TRANSLATION_UNAVAILABLE"`, `refusal_triggered=False`, zero silent English. |
| **16** | `test_catastrophic_failure_untranslated_notice` | Translation fails and target language is undetermined. Returns Level 2 notice. | HTTP 200, prefixed with `[Service Notice: Untranslated]`, `technical_error=True`, `technical_error_code="UNTRANSLATED_SYSTEM_FAILURE"`. |
| **17** | `test_chat_turn_restores_idle_closed_session` | Chat turn on an `IDLE_CLOSED` session ($< 24\text{h}$) restores session to `ACTIVE`, creates fresh conversation thread. | HTTP 200, `context_restored=True`, fresh `conversation_id`. |
| **18** | `test_chat_turn_on_expired_token_fails_401` | Chat turn on an expired session token (`expires_at <= now`) fails closed. | HTTP 401 Unauthorized; token never extended. |
| **19** | `test_idempotent_retry_after_successful_commit` | First turn succeeds and commits. Client retries with same key. Returns cached response with 0 new DB rows, turn count == 1, Redis history count == 1. | HTTP 200, identical response returned, database message count == 2, `turn_count == 1`. |
| **20** | `test_idempotency_failed_reservation_recovery_same_fingerprint` | Turn fails and transitions to FAILED. Client retries with identical fingerprint. Reclaims record via CAS with new owner token and commits cleanly. | First attempt -> 500/FAILED; second attempt -> HTTP 200, turn committed cleanly. |
| **21** | `test_idempotency_failed_reservation_rejects_different_fingerprint` | Client retries key previously marked FAILED, but with altered query parameters. | HTTP 409 Conflict ("Idempotency key reused with different request payload"). |
| **22** | `test_idempotency_concurrent_failed_reclaim_conflict` | Two concurrent retries race to reclaim a FAILED record. Exactly one worker wins CAS; second worker observes in-progress state and receives 409 + Retry-After. | Exactly one recovery proceeds; second receives HTTP 409 with `Retry-After: 1`. |
| **23** | `test_idempotency_lease_heartbeat_renewal_active` | During active processing, background heartbeat periodically extends `lease_expires_at = now() + 30s` via owner-token CAS. | Lease successfully extended; prevents premature timeout during long turns. |
| **24** | `test_idempotency_lease_loss_aborts_finalization` | Worker loses lease ownership (simulated disconnect). Heartbeat / finalization CAS update fails (rows=0). Worker aborts turn without writing messages. | Zero messages committed, turn count remains 0; error logged cleanly. |
| **25** | `test_idempotency_concurrent_in_progress_returns_409_retry_after` | Two concurrent coroutines submit the same key and fingerprint; second request observes committed reservation and immediately receives HTTP 409 Conflict with `Retry-After: 1` without blocking. | HTTP 409 with `Retry-After: 1` on concurrent duplicate; winning turn commits cleanly. |
| **26** | `test_idempotency_key_reuse_different_payload_conflict` | Reusing key with different message text returns HTTP 409 Conflict. Cached response is never leaked. | HTTP 409 Conflict ("Idempotency key reused with different request payload"). |
| **27** | `test_idempotency_hmac_fingerprint_versioning_and_rotation` | Record fingerprinted with `v1`. System rotates active key to `v2`. Client retries with same key. System verifies against `v1` secret; retry succeeds without false 409 conflict. | HTTP 200; versioned key lookup succeeds. |
| **28** | `test_cached_response_payload_encrypted_at_rest_and_redis_outage_resilient` | Verifies payload is encrypted with AES-256-GCM in PostgreSQL (`LargeBinary`); total Redis outage still returns decrypted cached response. | Assert database stores ciphertext `LargeBinary`, not plaintext JSON; HTTP 200 on Redis outage. |
| **29** | `test_heartbeat_cannot_renew_after_lease_expiration` | Worker's lease expires (`lease_expires_at <= NOW()`). Even if no other worker has taken over, heartbeat update affects 0 rows, lease is not renewed, and worker sets lease loss event. | CAS update returns 0 rows; lease_lost_event is set; worker raises `IdempotencyLeaseLostError`. |
| **30** | `test_old_owner_cannot_renew_after_successful_takeover` | Worker B takes over Worker A's expired lease and updates owner_token. Worker A attempts heartbeat renewal with its old owner token. | Heartbeat update returns 0 rows; Worker A's renewal is rejected; Worker A halts. |
| **31** | `test_old_owner_cannot_finalize_after_successful_takeover` | Worker B completes processing and commits. Worker A wakes up and attempts Transaction 2 finalization using its old owner token. | Update returns 0 rows; Transaction 2 rolls back; Worker A commits 0 rows. |
| **32** | `test_missing_hmac_active_key_prevents_startup` | Configuration has `IDEMPOTENCY_HMAC_ACTIVE_VERSION="v2"` but key ring does not contain `"v2"`. | Application startup fails immediately with `RuntimeError`. |
| **33** | `test_placeholder_hmac_key_prevents_startup` | Key ring contains blacklisted placeholder string `"change_me_to_a_secure_hmac_secret"`. | Validation fails; application startup crashes with `RuntimeError`. |
| **34** | `test_hmac_key_shorter_than_32_bytes_prevents_startup` | Key ring contains a secret with length < 32 bytes (e.g. 16 bytes). | Validation fails; application startup crashes with `RuntimeError`. |
| **35** | `test_heartbeat_task_reliably_cancelled_after_success` | Turn completes successfully. Background heartbeat task is cancelled and awaited. | Heartbeat task `is_cancelled()` or `is_done()` is True; zero orphan heartbeat loops remain. |
| **36** | `test_heartbeat_task_reliably_cancelled_after_processing_exception` | Upstream RAG or translation raises unhandled exception. Heartbeat task is cleanly cancelled and awaited in `finally` block. | Heartbeat task terminates cleanly; no orphan tasks or open database connections. |
| **37** | `test_heartbeat_task_cleaned_up_after_request_cancellation` | Client disconnects HTTP connection mid-turn (`asyncio.CancelledError`). Heartbeat task is cancelled, reservation is marked `FAILED` via owner-token check, and database transaction rolls back. | Heartbeat cancelled; record status transitions cleanly to FAILED. |
| **38** | `test_redis_history_update_remains_idempotent_across_retries` | First turn commits and writes to Redis hot context. Client retries with same idempotency key. Redis update checks `seen_events` set and skips duplicate append. | Redis timeline contains exactly 1 turn; `seen_events` set contains 1 event; zero duplicates. |
| **39** | `test_expired_owner_cannot_finalize_even_without_takeover` | Worker's lease expires (`lease_expires_at <= NOW()`). Even though no other worker took over, the finalization UPDATE condition `lease_expires_at > NOW()` evaluates to false (0 rows). The transaction rolls back immediately. | 0 rows affected; `await db.rollback()`; zero messages committed; turn count unchanged. |
| **40** | `test_atomic_failed_reclaim_rejects_mismatched_fingerprint_in_sql` | Client attempts to reclaim a `FAILED` record with a mismatched query fingerprint. The CAS UPDATE enforces `request_fingerprint = :expected_request_fingerprint` in SQL. | 0 rows affected; raises HTTP 409 Conflict; `FAILED` record remains unaltered. |
| **41** | `test_redis_reconciliation_rebuilds_hot_buffer_after_redis_outage` | Redis is down during turn 1. PostgreSQL commit succeeds. On turn 2, `get_recent_history()` detects cache miss and reconciles the hot buffer directly from PostgreSQL `chat_outbox_events` / `messages`. | Hot buffer successfully populated with historical turns; zero missing context. |
| **42** | `test_cached_retry_triggers_redis_reconciliation_if_missing` | Turn 1 commits in PostgreSQL while Redis is offline. Client later retries the completed turn. System decrypts cached response, detects `record.id` missing from Redis `seen_events`, and populates Redis before returning HTTP 200. | Cached HTTP 200 returned; Redis hot context and `seen_events` reconciled. |
| **43** | `test_redis_history_concurrent_or_out_of_order_deduplication` | Multiple retries or concurrent completion routines attempt to append the same `event_id` to Redis hot history. | Atomic `SADD` ensures exactly 1 insertion into `seen_events` and 1 append to timeline ZSET. |
| **44** | `test_redis_history_sliding_window_retention` | Successive turns are appended to Redis hot history. When count exceeds 10, oldest scores are evicted, keeping list size strictly $\le 10$. | Redis hot history contains exactly 10 turns; chronological sequence order preserved. |
| **45** | `test_request_cancellation_when_db_cleanup_fails_recovers_via_lease_expiry` | Client cancels request; DB cleanup fails (e.g. database disconnect). Record remains `IN_PROGRESS`. After lease expires (`lease_expires_at <= NOW()`), a subsequent retry successfully takes over via CAS. | Record recovers cleanly via CAS takeover; turn completes successfully. |
| **46** | `test_abrupt_worker_termination_recovers_via_lease_expiry_and_cas_takeover` | Worker process terminates abruptly (`SIGKILL`) mid-execution with zero cleanup. Record remains `IN_PROGRESS`. After 30s lease expires, subsequent retry reclaims reservation via CAS takeover. | CAS takeover succeeds; new worker owns reservation and commits turn. |
| **47** | `test_outbox_event_committed_atomically_with_turn` | In Tx2, verify `chat_outbox_events` record is inserted with `sequence_number = turn_count`, `status = 'PENDING'`, and identical payload alongside customer/agent messages and idempotency finalization. If Tx2 rolls back, 0 outbox events exist. | Exact ACID atomicity verified in real PostgreSQL; rollback produces 0 rows across all tables. |
| **48** | `test_outbox_worker_claims_events_skip_locked` | Two concurrent worker coroutines poll pending outbox events using `SELECT ... FOR UPDATE SKIP LOCKED`. Assert workers receive disjoint sets of events with zero lock blocking and zero duplicate processing. | Worker 1 and Worker 2 claim distinct batches; zero double-delivery. |
| **49** | `test_outbox_worker_crash_before_redis_delivery_recovers` | Worker claims pending outbox event, but crashes (or connection terminates) before calling Redis. Lock is released by PostgreSQL; surviving worker claims event on next iteration and delivers it. | Event status remains `PENDING` until delivered; turns cleanly applied to Redis. |
| **50** | `test_outbox_worker_crash_after_redis_before_ack_idempotent` | Worker delivers turn to Redis timeline, but crashes before committing `status = 'DELIVERED'` in PostgreSQL. Retrying worker re-delivers; Redis Lua script recognizes duplicate `event_id`, returns `ALREADY_PRESENT`, and worker commits `DELIVERED`. | Redis timeline has exactly 1 entry for the turn; outbox event transitions to `DELIVERED`. |
| **51** | `test_outbox_worker_exponential_backoff_on_redis_outage` | Redis is offline. Outbox worker encounters connection errors, increments `attempt_count`, and schedules `next_retry_at` with exponential backoff ($1\text{s}, 2\text{s}, 4\text{s}$). After 5 failed attempts, logs critical operational alert. | Status remains `PENDING` with exponential backoff; alert logged cleanly. |
| **52** | `test_delayed_old_event_delivery_preserves_chronological_order` | Turn 2 is delivered to Redis before Turn 1 (simulated network reordering). ZSET scores ($1$ and $2$) guarantee Turn 1 precedes Turn 2 in `ZRANGE timeline 0 -1`. | Redis timeline returns `[turn_1, turn_2]` in strictly ascending chronological order. |
| **53** | `test_concurrent_event_delivery_preserves_sequence_order` | Concurrent workers deliver turns 3, 4, 5 simultaneously. Lua script executes atomically; timeline ZSET places each turn at its exact sequence score without race conditions. | `ZRANGE timeline 0 -1` yields `[turn_1, turn_2, turn_3, turn_4, turn_5]` strictly ordered. |
| **54** | `test_reconciliation_reconstructs_correct_event_ids_and_turn_pairing` | Redis hot buffer is wiped (simulated cache loss). Reconciliation runs, queries `chat_outbox_events` and `messages`, pairs turns correctly, and rebuilds timeline ZSET with original event IDs and sequence scores. | Reconstructed timeline exactly matches original outbox sequence numbers and payloads. |
| **55** | `test_reconciliation_concurrent_with_new_deliveries` | Reconciliation merges turns 1–5 while live outbox worker simultaneously delivers turn 6. Timeline ZSET scores prevent clobbering; resulting timeline contains turns 1–6 in exact order. | Timeline contains turns 1–6 with contiguous sequence scores; zero lost turns. |
| **56** | `test_expired_deduplication_entries_do_not_append_stale_events_as_latest` | `seen_events` index expires. A delayed worker attempts delivering Turn 1 when timeline contains turns 11–20. Lua script detects `seq_num=1 < min_seq=11`, returns `STALE_OUTSIDE_WINDOW`, and leaves latest 10 turns intact. | Timeline remains strictly turns 11–20; stale Turn 1 is ignored and does not displace active turns. |
| **57** | `test_outbox_retention_cleanup_respects_pending_delivery_state` | Retention cleanup purges `DELIVERED` outbox events older than 7 days, but strictly preserves `PENDING` or unacknowledged events regardless of age. | Delivered events older than 7 days pruned; pending/unacknowledged events preserved. |
| **58** | `test_redis_empty_when_old_pending_event_arrives_triggers_initialization` | Redis is empty. An old delayed outbox event arrives. System detects uninitialized timeline, runs reconciliation first, populates latest turns, then ignores old event. | Latest context preserved; old turn does not become latest event. |
| **59** | `test_redis_keys_expired_while_old_outbox_pending` | Timeline keys expire after TTL. Old pending event arrives. Worker reconciles from PostgreSQL before processing live events, ensuring correct chronological watermark. | Expired timeline safely re-initialized from PostgreSQL before accepting live traffic. |
| **60** | `test_concurrent_timeline_initialization_and_live_delivery` | Worker A triggers reconciliation while Worker B simultaneously delivers a live new turn. Redis Lua script and ZSET scores preserve sequence monotonic ordering without deadlock. | All turns $1 \dots N$ ordered monotonically in ZSET; zero data clobbering. |
| **61** | `test_redis_restart_followed_by_delayed_event_delivery` | Redis restarts (all keys cleared). Delayed event arrives. System initializes from PostgreSQL, detects delayed event is outside 10-turn window, and drops it safely. | Redis timeline reflects PostgreSQL authoritative latest 10 turns. |
| **62** | `test_outbox_dead_letter_transition_and_redrive` | An outbox event fails 10 consecutive delivery attempts, transitioning to `DEAD_LETTER`. Automated watchdog resets it to `PENDING` when Redis recovers; event delivers cleanly. | Event transitions `PENDING` $\rightarrow$ `DEAD_LETTER` $\rightarrow$ re-driven to `DELIVERED`. |
| **63** | `test_reconciliation_identity_preservation_with_partial_outbox_pruning` | Outbox records for turns 1–3 were purged by retention. Reconciliation recovers turns 1–3 from `messages` using deterministic UUIDv5 event IDs. IDs match original outbox event IDs exactly. | Deterministic UUIDv5 event IDs match original IDs; 100% identity preservation. |
| **64** | `test_outbox_payload_data_minimization_and_pii_masking` | Inbound query with credit card number and SSN is processed. Verifies outbox `payload` contains only masked content (`[REDACTED_CREDIT_CARD]`) and zero raw credentials. | Assert zero plaintext PII in JSONB payload; only masked text stored. |
| **65** | `test_outbox_sanitized_error_storage` | Injected simulated error with sensitive API key and stack trace. Verifies `last_error` stores only sanitized `ExceptionClass:ERROR_CODE` under 128 characters without leakage. | Assert `last_error` is sanitized string without stack trace or API credentials. |
| **66** | `test_migration_002_backfills_unambiguous_historical_messages` | Verified conversation with strictly monotonic alternating `CUSTOMER` and `AI_AGENT` messages matching `turn_count*2` receives sequential integer `turn_sequence_number` values ($1 \dots N$). | All messages in candidate conversation receive correct turn sequence numbers; zero data corruption. |
| **67** | `test_migration_002_preserves_ambiguous_or_system_messages_as_legacy_null` | Conversation containing `SYSTEM` messages, unequal counts, or non-alternating senders leaves `turn_sequence_number = NULL` on all messages. | Ambiguous messages remain `NULL`; zero incorrect pairing; original rows untouched. |
| **68** | `test_migration_002_rollback_preserves_original_message_data` | Executing `downgrade()` removes `turn_sequence_number` and partial indexes, but 100% preserves original message columns, content, timestamps, and foreign keys. | Downgrade executes cleanly; all original messages intact. |
| **69** | `test_redis_watermark_rejects_stale_event_when_timeline_has_fewer_than_10_turns` | Timeline has 3 turns (scores 8, 9, 10; $W_{max} = 10$). A delayed event with `seq_num = 0` arrives ($0 < 10-9$). Lua evaluates stale condition and rejects. | Event rejected as `STALE_OUTSIDE_WINDOW`; small `ZCARD` never allows obsolete events into buffer. |
| **70** | `test_redis_watermark_advances_monotonically_on_newer_event` | Timeline has $W_{max} = 10$. Event with `seq_num = 11` arrives. Watermark updates to 11, new window is $[2, 11]$, and Turn 1 is evicted. | $W_{max} == 11$; oldest score $< 2$ evicted; monotonicity preserved. |
| **71** | `test_redis_uninitialized_timeline_rejects_ad_hoc_insert_without_watermark` | Timeline key exists but watermark key is missing. Lua rejects insertion with `UNINITIALIZED_WATERMARK_REQUIRED`. | Return code 0; enforces that worker must run atomic initialization first. |
| **72** | `test_concurrent_reconciliation_lock_prevents_duplicate_initialization` | Two workers detect uninitialized timeline simultaneously. Worker 1 acquires `conv:{id}:reconcile_lock`. Worker 2 backs off and waits. | Exactly 1 worker queries DB and initializes timeline; zero duplicate reconciliation load. |
| **73** | `test_reconciliation_merge_never_decreases_watermark_or_overwrites_newer_live_event` | Worker A initiates reconciliation snapshotting $W_{auth} = 10$. Concurrently, live Worker B delivers turn 11 ($W_{max} = 11$). Worker A's merge executes. | Watermark remains 11; turn 11 in ZSET is untouched; older missing turns merged safely. |
| **74** | `test_db_enforces_one_customer_and_one_agent_message_per_turn_sequence` | Attempting to insert two `CUSTOMER` messages with the same `turn_sequence_number = 1` in the same conversation raises PostgreSQL `UniqueViolation` on `uq_messages_conv_turn_sender`. | PostgreSQL constraint violation raised; transaction rolls back. |
| **75** | `test_db_permits_multiple_system_messages_with_null_sequence` | Inserting multiple `SYSTEM` or `HUMAN_AGENT` messages with `turn_sequence_number = NULL` in the same conversation succeeds cleanly. | Successful insertion of multiple NULL sequence rows without constraint conflict. |
| **76** | `test_db_composite_foreign_keys_prevent_cross_conversation_message_linkage` | Attempting to insert a `chat_outbox_events` row where `customer_message_id` belongs to Conversation A but `conversation_id` is Conversation B raises PostgreSQL `ForeignKeyViolation`. | Database rejects cross-conversation message referencing; 100% integrity enforced. |
| **77** | `test_reconciliation_skips_incomplete_or_corrupted_message_pairs` | Conversation has a customer message with `turn_sequence_number = 4` but no corresponding agent message. Reconciliation marks turn incomplete and omits it from hot history. | Incomplete turn excluded from 10-turn window; warning logged; zero synthetic fake IDs. |
| **78** | `test_canonical_uuidv5_golden_vector_and_namespace_stability` | Golden test verifying `generate_outbox_event_id("12345678-1234-5678-1234-567812345678", 1)` strictly equals `"5d56372a-7576-5454-856a-602a0895e80c"`. | Golden assert passes; guarantees zero accidental future drift in event ID hashing. |
| **79** | `test_outbox_event_preserved_when_message_deleted_restrict_prevents_erasure` | Attempting to delete a message row referenced by an active outbox event raises `ForeignKeyViolation` on `ON DELETE RESTRICT`. | Message cannot be deleted while referenced by outbox; evidence of committed turn preserved. |
| **80** | `test_reconciliation_handles_unavailable_historical_messages` | In a pruned conversation, message for Turn 11 was deleted. Reconciliation discovers missing message, skips Turn 11, logs warning, and does not synthesize fake text. | Missing turn omitted cleanly; zero hallucinated data; valid turns 12–20 populated. |
| **81** | `test_single_authoritative_sequence_allocation_from_turn_count` | Concurrent transactions attempt turn completion. Row lock on `conversations` serializes sequence allocation from `turn_count + 1`. Both turns get distinct, monotonic, gapless sequences. | Sequences strictly $N, N+1$; zero race collisions. |
| **82** | `test_sequence_allocation_on_legacy_only_conversation` | Conversation with 3 legacy turns (`NULL` sequences, `turn_count = 3`) receives a Phase 4 turn. Allocates sequence 4, leaving legacy messages `NULL`. | New messages and outbox receive sequence 4; legacy messages remain intact. |
| **83** | `test_watermark_calculation_after_outbox_pruning_and_redis_restart` | Outbox events for turns 1–12 are pruned. Redis restarts. System reads $W_{auth} = \text{turn\_count} = 20$ from `conversations`. Watermark accurately reflects 20. | $W_{auth} == 20$; window accurately set to $[11, 20]$. |
| **84** | `test_backfill_rejects_ambiguous_timestamp_with_multiple_customer_messages` | Conversation with two customer messages having identical timestamps fails alternating check and retains `turn_sequence_number = NULL`. | Messages remain `NULL`; zero arbitrary sorting by random UUID. |
| **85** | `test_backfill_strictly_monotonic_timestamps_required` | Conversation where customer and agent have identical `created_at` timestamp is rejected from backfill because equal timestamps are ambiguous. | Messages remain `NULL`; zero assumption of causality without temporal proof. |
| **86** | `test_backfill_rejects_interleaved_human_agent_role` | Conversation with an interleaved `HUMAN_AGENT` message disqualifies entire thread from backfill. All messages retain `turn_sequence_number = NULL`. | All messages remain `NULL`; zero disruption of human dialogue records. |
| **87** | `test_backfill_rejects_duplicate_customer_messages_in_a_row` | Conversation with two consecutive customer messages is rejected from backfill. | All messages remain `NULL`; zero corrupted pairing. |
| **88** | `test_backfill_rejects_incomplete_trailing_customer_message` | Conversation with an unreplied customer message is rejected from backfill. | All messages remain `NULL`; zero orphan turn creation. |
| **89** | `test_distributed_reconciliation_lock_owner_verified_release` | Worker A acquires lock with token A. Worker B attempts release with token B (returns 0). Worker A releases with token A (returns 1). | Atomic Lua compares token; worker never deletes another worker's lock. |
| **90** | `test_reconciliation_merge_after_lock_lease_expiry_preserves_monotonicity` | Worker A's lock lease expires after 5s. Live Worker B delivers turn 11 ($W_{max}=11$). Worker A's delayed merge runs with $W_{auth}=10$. | Watermark remains 11; turn 11 in ZSET is untouched; older missing turns merged safely. |
| **91** | `test_db_trigger_enforces_outbox_customer_message_sender_type` | Trigger `tg_validate_chat_outbox_turn` intercepts outbox insert where `customer_message_id` points to an `AI_AGENT` or `SYSTEM` message. | Trigger raises `OUTBOX_INTEGRITY_VIOLATION`; transaction rolls back. |
| **92** | `test_db_trigger_enforces_outbox_agent_message_sender_type` | Trigger intercepts outbox insert where `agent_message_id` points to a `CUSTOMER` message. | Trigger raises `OUTBOX_INTEGRITY_VIOLATION`; transaction rolls back. |
| **93** | `test_db_trigger_enforces_outbox_sequence_matches_message_sequence` | Trigger intercepts outbox insert where `outbox.sequence_number = 5` but `messages.turn_sequence_number = 4`. | Trigger raises `OUTBOX_INTEGRITY_VIOLATION`; transaction rolls back. |
| **94** | `test_db_trigger_enforces_message_turn_sequence_immutability` | Post-commit attempt to update `turn_sequence_number` on a message raises PostgreSQL `MESSAGE_IMMUTABILITY_VIOLATION`. | Trigger prevents modification; turn sequence immutable. |
| **95** | `test_db_trigger_enforces_message_sender_type_immutability` | Post-commit attempt to modify `sender_type` on an already sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION`. | Sender type immutable; audit trail protected. |
| **96** | `test_db_trigger_enforces_message_conversation_id_immutability` | Post-commit attempt to reassign `conversation_id` on a sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION`. | Reassignment prohibited; cross-thread integrity enforced. |
| **97** | `test_db_trigger_enforces_message_content_immutability` | Post-commit attempt to alter masked `content` on a sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION`. | Content immutable; audit integrity preserved. |
| **98** | `test_db_trigger_permits_message_encryption_content_update` | Updating operational fields (`encrypted_raw_content`, metadata) on a message succeeds cleanly without triggering immutability violation. | Operational updates succeed; security features unhindered. |
| **99** | `test_redis_event_acceptance_rejects_identity_mismatch` | Worker delivers event where `event_id` does not match `uuid5(NAMESPACE, "v1:" .. conv .. ":" .. seq)`. Lua script returns `{0, "IDENTITY_MISMATCH"}`. | Event rejected; Redis hot context untouched; security alert emitted. |
| **100** | `test_redis_event_acceptance_handles_concurrent_reconciliation` | Inbound event arrives during reconciliation execution. Redis single-threaded Lua engine serializes execution; resulting buffer contains both reconciled and live turns strictly ordered. | Timeline returns strictly contiguous, ascending sequence scores; zero lost events. |
| **101** | `test_redis_gap_detection_on_out_of_order_arrival` | Watermark 8. Event 12 arrives (9–11 missing). Lua sets $W_{max}=12$, leaves $W_{window\_contig}=8$, and returns `APPLIED_WITH_GAP`. | Gap registered in Redis metadata; outbox worker alerts reconciliation. |
| **102** | `test_redis_gap_triggers_authoritative_postgres_fallback` | Reader observing $W_{window\_contig} < W_{max}$ flags `INCOMPLETE_TIMELINE`, bypasses Redis, queries PostgreSQL directly, and serves 100% complete history. | Complete context served to prompt; zero partial history presentation. |
| **103** | `test_redis_gap_repaired_by_delayed_event_arrival` | In a gapped timeline ($W_{window\_contig}=8, W_{max}=12$), delayed events 9, 10, 11 arrive. Contiguous watermark $W_{window\_contig}$ advances to 12. | Continuity restored; gap flag cleared. |
| **104** | `test_redis_gap_repaired_by_background_reconciliation` | Outbox worker runs range reconciliation for missing turns $[9, 11]$ from PostgreSQL and merges into Redis via Lua. | Missing turns merged; contiguous chain repaired. |
| **105** | `test_redis_restart_reconstructs_gapless_history` | Redis cache wiped while PostgreSQL has turns 1–12. Reader runs reconciliation from `turn_count=12`, queries turns 3–12, and populates gapless Redis timeline with $W_{max}=12, W_{window\_contig}=12$. | Redis timeline restored gapless; 100% complete context. |
| **106** | `test_db_trigger_rejects_nulling_committed_turn_sequence_number` | Attempting to `UPDATE messages SET turn_sequence_number = NULL` on an already sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION` via `IS DISTINCT FROM`. | Sequence cannot be reset to NULL; turn immutability preserved. |
| **107** | `test_db_trigger_rejects_nulling_committed_message_content` | Attempting to `UPDATE messages SET content = NULL` on an already sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION` via `IS DISTINCT FROM`. | Content cannot be cleared to NULL; audit trail preserved. |
| **108** | `test_db_trigger_enforces_sender_id_immutability_on_committed_turn` | Attempting to mutate or clear `sender_id` on an already sequenced message raises `MESSAGE_IMMUTABILITY_VIOLATION`. | Sender attribution permanently immutable after turn commitment. |
| **109** | `test_db_trigger_rejects_unauthorized_legacy_sequence_assignment` | Ordinary application transaction executing `UPDATE messages SET turn_sequence_number = 1 WHERE turn_sequence_number IS NULL` fails with `LEGACY_SEQUENCE_MUTATION_FORBIDDEN`. | Legacy messages cannot be assigned sequences post-migration. |
| **110** | `test_migration_backfill_succeeds_before_triggers_installed` | Migration 002 executes verified backfill DDL before triggers are installed; verified records updated cleanly. | Initial migration backfill succeeds. |
| **111** | `test_retained_window_completeness_identifies_missing_turns` | With $W_{max}=12, \text{window\_start}=3$, timeline containing turns $[3, 4, 6, \dots, 12]$ fails contiguity check ($W_{window\_contig}=4 < 12$). | Missing turn 5 detected; flags `INCOMPLETE_TIMELINE`. |
| **112** | `test_retained_window_ignores_intentionally_evicted_turns_below_window_start` | With $W_{max}=15, \text{window\_start}=6$, absence of turns 1–5 from timeline does NOT trigger a gap flag because $6 \dots 15$ are contiguous. | Window $[6, 15]$ evaluated as complete; intentional eviction recognized. |
| **113** | `test_conversations_with_zero_turn_count_remain_unsequenced_legacy` | Legacy conversation with existing messages and `turn_count = 0` retains `turn_sequence_number = NULL` during backfill. | Messages remain `NULL`; zero manufactured numbering on unproven history. |
| **114** | `test_new_turns_on_zero_turn_count_legacy_conversation_allocate_from_one` | First Phase 4 turn on a legacy conversation with `turn_count = 0` allocates sequence 1 without altering legacy messages. | New turn receives sequence 1; legacy records intact. |
| **115** | `test_db_trigger_rejects_insert_with_mismatched_turn_sequence_number` | Application session attempts `INSERT INTO messages` with `turn_sequence_number = 99` when `turn_count = 2`. Fails with `SEQUENCE_ALLOCATION_MISMATCH`. | Database trigger rejects unallocated sequence insertion. |
| **116** | `test_db_trigger_rejects_insert_with_sequence_on_system_message` | Application session attempts `INSERT INTO messages` with `sender_type='SYSTEM'` and `turn_sequence_number=1`. Fails with `NON_DIALOG_MESSAGE_SEQUENCE_FORBIDDEN`. | Non-dialog messages strictly forbidden from carrying turn sequence numbers. |
| **117** | `test_db_trigger_rejects_insert_with_sequence_on_human_agent_message` | Application session attempts `INSERT INTO messages` with `sender_type='HUMAN_AGENT'` and `turn_sequence_number=1`. Fails with `NON_DIALOG_MESSAGE_SEQUENCE_FORBIDDEN`. | Human agent messages strictly forbidden from carrying automated turn sequences. |
| **118** | `test_db_trigger_permits_insert_with_null_sequence_on_system_message` | Application session inserts `SYSTEM` message with `turn_sequence_number=NULL`. Insertion succeeds cleanly. | Valid operational messages insert without impediment. |
| **119** | `test_db_trigger_rejects_deletion_of_numbered_message_with_active_outbox` | Application session executes `DELETE FROM messages WHERE id = :msg_id` on a numbered message with active outbox event. Rejection enforced. | Deletion fails closed. |
| **120** | `test_db_trigger_rejects_deletion_of_numbered_message_after_outbox_pruned` | Application session executes `DELETE FROM messages WHERE id = :msg_id` on a numbered message whose outbox event was pruned after 7 days. Trigger `tg_enforce_message_turn_immutability` raises `NUMBERED_MESSAGE_DELETION_PROHIBITED`. | Numbered message protected permanently; audit continuity intact. |
| **121** | `test_w_window_contig_evaluates_exact_retained_window_range` | With $W_{max}=12, \text{window\_start}=3$, verifies $W_{window\_contig}$ accurately tracks continuity from 3 to 12. | Retained-window contiguity accurately evaluated. |
| **122** | `test_w_global_contig_matches_authoritative_turn_count_in_postgres` | Verifies $W_{global\_contig} \equiv \text{turn\_count}$ across committed transactions in PostgreSQL. | PostgreSQL sequence continuity confirmed gapless. |
| **123** | `test_api_response_completeness_flag_accurate_on_postgres_fallback` | When Redis cache experiences gap, reader falls back to PostgreSQL and returns complete context with `context_window_complete=True`. | API consumers receive accurate completeness indication. |
| **124** | `test_atomic_turn_pair_rejects_commit_with_customer_message_only` | Application session inserts `CUSTOMER` message with `turn_sequence_number = N` and attempts `await db.commit()` without `AI_AGENT` message. Deferred constraint trigger `tg_enforce_turn_pair_completeness` raises `INCOMPLETE_TURN_PAIR`. | PostgreSQL aborts transaction at commit; zero rows persisted in `messages`. |
| **125** | `test_atomic_turn_pair_rejects_commit_with_ai_message_only` | Application session inserts `AI_AGENT` message with `turn_sequence_number = N` and attempts `await db.commit()` without `CUSTOMER` message. Trigger raises `INCOMPLETE_TURN_PAIR`. | Transaction aborted; zero messages persisted. |
| **126** | `test_atomic_turn_pair_rejects_commit_without_outbox_event` | Application session inserts both `CUSTOMER` and `AI_AGENT` messages with `turn_sequence_number = N`, but omits inserting into `chat_outbox_events`. At `commit()`, trigger raises `MISSING_OUTBOX_EVENT`. | Transaction aborted; pair rejected without transactional outbox record. |
| **127** | `test_atomic_turn_pair_rejects_commit_without_turn_count_increment` | Application session inserts pair with sequence $N$ while `conversations.turn_count` remains $N-1$. At `commit()`, trigger raises `UNALLOCATED_TURN_SEQUENCE`. | Transaction aborted; turn sequence cannot exceed allocated turn count. |
| **128** | `test_atomic_turn_pair_rollback_on_worker_crash_leaves_clean_database` | Simulated process termination or unhandled exception during Tx2 between customer message insert and AI message insert triggers transaction rollback. | Database verifies zero rows inserted in `messages`, `conversations.turn_count` unchanged, and idempotency status remains `IN_PROGRESS` until lease expiry. |
| **129** | `test_atomic_turn_pair_concurrency_race_serialized_by_conversation_row_lock` | Two concurrent workers racing on the same conversation serialize on `UPDATE conversations ... WHERE id = :id`. | The second worker blocks until the first commits, preventing interleaved turn sequences or torn pairs. |
| **130** | `test_restricted_app_role_cannot_disable_triggers` | Session connected as `app_user` executes `ALTER TABLE messages DISABLE TRIGGER ALL`. | PostgreSQL raises `ERROR: must be owner of table messages` (SQLSTATE `42501`). |
| **131** | `test_restricted_app_role_cannot_drop_or_alter_tables` | Session connected as `app_user` executes `ALTER TABLE messages DROP COLUMN turn_sequence_number` or `DROP TABLE messages`. | PostgreSQL raises `ERROR: permission denied for table messages`. |
| **132** | `test_restricted_app_role_cannot_drop_or_replace_trigger_functions` | Session connected as `app_user` executes `DROP FUNCTION fn_enforce_message_turn_immutability()` or `CREATE OR REPLACE FUNCTION`. | PostgreSQL raises `ERROR: permission denied`. |
| **133** | `test_restricted_app_role_cannot_directly_update_turn_count` | Session connected as `app_user` executes direct `UPDATE conversations SET turn_count = 999 WHERE id = :id`. | PostgreSQL raises `ERROR: permission denied for column "turn_count" of relation "conversations"` (SQLSTATE `42501`) or `INVALID_TURN_COUNT_MUTATION`. |
| **134** | `test_restricted_app_role_allocates_turn_via_security_definer_function` | Session connected as `app_user` executes `SELECT fn_allocate_next_turn_sequence(:id)`. | Function successfully executes as `migration_admin`, acquires row lock, and increments `turn_count` by exactly 1. |
| **135** | `test_restricted_app_role_cannot_delete_from_outbox` | Session connected as `app_user` executes `DELETE FROM chat_outbox_events WHERE event_id = :id`. | PostgreSQL raises `ERROR: permission denied for table chat_outbox_events` (SQLSTATE `42501`). |
| **136** | `test_restricted_app_role_cannot_modify_immutable_outbox_columns` | Session connected as `app_user` executes `UPDATE chat_outbox_events SET sequence_number = 99 WHERE event_id = :id` or `SET payload = ...`. | PostgreSQL raises `permission denied` or `OUTBOX_IMMUTABILITY_VIOLATION`. |
| **137** | `test_restricted_app_role_cannot_delete_from_idempotency` | Session connected as `app_user` executes `DELETE FROM chat_idempotency_records WHERE id = :id`. | PostgreSQL raises `ERROR: permission denied for table chat_idempotency_records` (SQLSTATE `42501`). |
| **138** | `test_restricted_app_role_cannot_modify_completed_idempotency_record` | Session connected as `app_user` executes `UPDATE chat_idempotency_records SET status = 'IN_PROGRESS' WHERE status = 'COMPLETED'`. | PostgreSQL trigger raises `COMPLETED_IDEMPOTENCY_IMMUTABLE`. |
| **139** | `test_restricted_app_role_enforces_deletion_prohibition_on_messages` | Session connected as `app_user` attempts `DELETE FROM messages WHERE id = :id` for numbered message. | PostgreSQL raises `permission denied for table messages` or `NUMBERED_MESSAGE_DELETION_PROHIBITED`. |
| **140** | `test_restricted_app_role_cannot_insert_unallocated_turn_sequence` | Session connected as `app_user` attempts direct `INSERT INTO messages` with `turn_sequence_number != conversations.turn_count`. | Trigger raises `SEQUENCE_ALLOCATION_MISMATCH`. |
| **141** | `test_real_postgres_role_separation_runtime_vs_migration` | Verifies that session connected as `migration_admin` is the table owner and can run DDL, while session connected as `app_user` has `rolsuper = False` and is blocked from DDL, arbitrary turn_count updates, and deletes. | Real PostgreSQL role separation verified on live server. |
---

## 11. Implementation Sequence & Dual Authorization Boundary

```
[Phase 4 Implementation Workflow]
                 │
                 ▼
[Step 1: STOP — Request Dual Authorization for Migration 002 & Phase 4 Implementation]
                 │  (Awaiting User Authorization)
                 ▼
[Step 2: Apply Database Migration]
                 │  • Create backend/alembic/versions/002_chat_idempotency_and_outbox.py
                 │  • Run: alembic upgrade head
                 ▼
[Step 3: Update SQLAlchemy Domain Models & Config]
                 │  • Add ChatIdempotencyRecord, ChatOutboxEvent, and Message.turn_sequence_number
                 │  • Add RESTRICT foreign keys, partial unique indexes, and triggers to models
                 │  • Add IDEMPOTENCY_HMAC settings, key ring validator, and startup check to backend/app/core/config.py
                 ▼
[Step 4: Create Multilingual Orchestrator & Outbox Worker]
                 │  • Implement backend/app/modules/multilingual/orchestrator.py
                 │  • Implement backend/app/modules/multilingual/outbox_worker.py
                 │  • Implement two-phase reservation protocol, independent heartbeat session, CAS FAILED recovery,
                 │    dedicated HMAC-SHA256 fingerprinting, AES-256-GCM payload encryption,
                 │    atomic sequence allocation via UPDATE conversations RETURNING turn_count,
                 │    transactional outbox staging in Tx2 with RESTRICT message validation,
                 │    FOR UPDATE SKIP LOCKED worker polling, conversation-scoped Redis ZSET Lua timeline,
                 │    dual-watermark gap detection, owner-verified distributed locking, and dead-letter re-drive
                 ▼
[Step 5: Register Dependency Provider]
                 │  • Update backend/app/api/deps.py with get_multilingual_orchestrator()
                 ▼
[Step 6: Update Chat Router & Contracts]
                 │  • Update backend/app/api/v1/chat.py with ChatMessageRequest & ChatMessageData
                 ▼
[Step 7: Author Integration Test Suite]
                 │  • Create backend/tests/test_task6_phase4.py (141 tests)
                 ▼
[Step 8: Execute Full Regression Suite]
                 │  • Verify 141/141 Phase 4 tests, 247/247 Task 6 tests, 561/561 platform tests
```

---

## 12. Verification & Acceptance Criteria
1. Explicit dual authorization granted for migration `002_chat_idempotency_and_outbox.py` and Phase 4 implementation.
2. Migration applies cleanly in PostgreSQL (`alembic upgrade head`) and supports downgrade.
3. `backend/tests/test_task6_phase4.py` passes all 141/141 tests against real PostgreSQL and Redis using real database roles (`app_user` and `migration_admin`).
4. Combined Task 6 suite (`test_task6_phase1.py`, `test_task6_phase2.py`, `test_task6_phase3.py`, `test_task6_phase4.py`) passes 247/247 tests.
5. Full platform regression suite (`backend/tests/`) passes 561/561 tests with zero regressions, zero failures, and zero warnings.
6. `storage/indices/active_index.json` confirmed absent (`False`).
