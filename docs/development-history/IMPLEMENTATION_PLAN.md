# DAY 08 IMPLEMENTATION PLAN — TASK 5
## Sentiment Analysis, Frustration Trajectory & Tone Adaptation Engine

**Document Version:** 1.0.0  
**Status:** DRAFT — PENDING USER AUTHORIZATION  
**Author:** Lead Platform Engineer  
**Target Module:** `backend/app/modules/sentiment` (Module 5 / Task 5)  
**Parent Roadmap:** 16-Day Enterprise Support Platform Engineering Roadmap (Day 08)  

---

## 1. Executive Summary & Objective

Day 08 focuses on the implementation and validation of **Task 5 (Module 5: Sentiment Analysis & Dynamic Tone Adaptation Engine)**. 
Building upon the completed foundations of Day 01–07 (PostgreSQL 16 persistence, FAISS vector retrieval, Advanced RAG orchestration, and Multilingual conversational pipeline), this module empowers the platform to:
1. Classify incoming customer messages in real-time across six fine-grained emotional categories (`positive`, `neutral`, `negative`, `frustrated`, `urgent`, `sarcastic`) with a calibrated confidence score.
2. Accurately detect sarcasm and passive-aggressive tones, mathematically preventing sarcastic remarks from being miscategorized as positive feedback.
3. Compute emotional velocity and frustration trajectory slopes over sliding conversational dialogue windows (e.g. the last 5 turns).
4. Dynamically modulate LLM system instructions to respond with empathetic, concise, and de-escalating phrasing without compromising or weakening underlying corporate policy constraints, warranties, or liability limits.
5. Identify immediate high-risk escalation triggers (severe legal threats, physical safety/self-harm concerns, regulatory complaints, or sustained extreme frustration).
6. Provide after-hours and on-call routing foundations (seamless handoff to on-call queues and alerting webhooks when human desks are closed).
7. Persist structured, immutable audit records into the existing `sentiment_logs` and `escalation_audits` tables in PostgreSQL 16.
8. Wire cleanly into Step 8 of the canonical conversational endpoint (`POST /api/v1/chat/message`).

---

## 2. Invariant Architecture & Existing Foundation

### 2.1 Database Assets (Already Provisioned & Verified)
The database schema provisioned during Day 02 and Migration 002 already contains the required tables, foreign keys, indexes, and constraints:

1. **`sentiment_logs` Table:**
   - `id`: UUID Primary Key.
   - `message_id`: UUID (Foreign Key to `messages.id` on delete CASCADE, UNIQUE).
   - `sentiment_class`: `VARCHAR(30)` (`positive`, `neutral`, `negative`, `frustrated`, `urgent`, `sarcastic`).
   - `confidence_score`: `FLOAT` ($\in [0.0, 1.0]$).
   - `frustration_index`: `FLOAT` ($\in [0.0, 1.0]$).
   - `urgency_index`: `FLOAT` ($\in [0.0, 1.0]$).
   - `trajectory_slope`: `FLOAT` (rate of change over sliding window).
   - `requires_escalation`: `BOOLEAN` (indexed).
   - `escalation_reason`: `VARCHAR(255)` (nullable).
   - `evaluated_at`: `TIMESTAMPTZ`.

2. **`escalation_audits` Table:**
   - `id`: UUID Primary Key.
   - `ticket_id`: UUID (Foreign Key to `tickets.id` on delete CASCADE).
   - `trigger_condition`: `VARCHAR(100)` (`SLA_BREACH`, `SENTIMENT_FRUSTRATION`, `HIGH_RISK_SAFETY`, `INACTIVITY_TIMEOUT`, `MANUAL_OVERRIDE`).
   - `previous_state`: `VARCHAR(50)`.
   - `new_state`: `VARCHAR(50)`.
   - `escalation_reason`: `TEXT`.
   - `escalation_payload`: `JSONB`.
   - `escalated_by_user_id`: UUID (Foreign Key to `users.id` on delete SET NULL).
   - `triggered_at`: `TIMESTAMPTZ`.

3. **`users` Table & On-Call Flags:**
   - `is_on_call`: `BOOLEAN` (indexed).
   - `department`: `VARCHAR(100)`.
   - `skills`: `JSONB` array of domain skills (`hardware`, `returns`, `warranty`, `escalations`, `quality_audit`, `vip`).
   - `max_active_tickets`: `INTEGER`.

4. **`sla_calendars` Table:**
   - Operational business hours (`work_start_time`, `work_end_time`, `work_days`, `holidays`, `timezone`).

### 2.2 Security & Least-Privilege Invariants
- Runtime application operations connect strictly using PostgreSQL role `app_user`.
- Role `app_user` has `SELECT` and `INSERT` grants on `sentiment_logs` and `escalation_audits`.
- **0 DELETE** grants on sentiment or audit logs.
- Sensitive customer PII must be masked prior to LLM sentiment prompting or external alerting payloads.

---

## 3. Detailed Component Architecture

The module will be housed under `backend/app/modules/sentiment/`:

```text
backend/app/modules/sentiment/
├── __init__.py
├── classifier.py          # Multi-class sentiment & sarcasm classification engine
├── trajectory.py          # Sliding-window frustration slope & velocity calculator
├── tone_adapter.py        # System prompt modifier for dynamic empathy & tone modulation
├── escalation.py          # Immediate risk detector, after-hours check & escalation manager
├── schemas.py             # Pydantic v2 DTOs (SentimentResult, EscalationDecision, TrajectoryMetrics)
└── service.py             # High-level SentimentService facade coordinating pipeline
```

### 3.1 Multi-Class Sentiment Classifier (`classifier.py`)
- **Classes:** `positive`, `neutral`, `negative`, `frustrated`, `urgent`, `sarcastic`.
- **Latency Budget:** $\le 200$ ms.
- **Engine:** Lightweight LLM classification using `LLMGateway` (`gemini-2.5-flash`) with structured JSON schema output, falling back to a deterministic rule-based lexical analyzer if the LLM gateway fails or times out.
- **Sarcasm Handling:** Specialized prompt cues and lexical indicators (e.g. contradictory sentiment adjectives like *"Wonderful, another delivery delay"*). Sarcasm is categorized as `sarcastic`, with an inherent high frustration score ($\ge 0.70$) and low valence score, completely preventing false positive misclassification.

### 3.2 Trajectory & Frustration Velocity Engine (`trajectory.py`)
- Evaluates up to the last 5 customer message turns from the current conversation.
- Assigns emotional valence scores:
  - `positive`: $+1.0$
  - `neutral`: $0.0$
  - `negative`: $-0.5$
  - `sarcastic`: $-0.7$
  - `frustrated`: $-0.9$
  - `urgent`: $-0.6$
- Computes trajectory slope $m$:
  $$m = \frac{\Delta \text{Frustration}}{\Delta \text{Turn}}$$
- Flags positive acceleration into frustration (e.g., transition from `neutral` $\to$ `negative` $\to$ `frustrated`).

### 3.3 Dynamic Tone Adaptation Engine (`tone_adapter.py`)
- Modulates the system instructions passed to the RAG generator (`generator.py`):
  - **Neutral / Positive:** Professional, concise, standard support tone.
  - **Frustrated / Sarcastic:** Heightened empathy, de-escalating apologies for inconvenience, transparent acknowledgments, direct and immediate next steps without condescending phrases.
  - **Urgent / Escalated:** Ultra-direct, actionable, immediate contact verification and SLA commitments.
- **Critical Policy Guardrail:** Tone adaptation **never** alters underlying business rules, policy exceptions, return timeframes, or financial allowances.

### 3.4 Immediate Risk & After-Hours Escalation Engine (`escalation.py`)
- **Deterministic High-Risk Triggers:**
  - `LEGAL_THREAT`: Attorney involvement, litigation, regulatory reporting (FTC, CFPB, GDPR, CCPA).
  - `SAFETY_CONCERN`: Physical injury, safety hazards, electrical sparks, battery swelling, self-harm.
  - `SUSTAINED_EXTREME_FRUSTRATION`: Confidence $> 0.85$ on `frustrated` across $\ge 2$ consecutive customer turns.
- **Business Hours & After-Hours Detection:**
  - Evaluates current timestamp against the active `SLACalendar` (defaulting to `settings.BUSINESS_HOURS_TIMEZONE`, `09:00–17:00`, Monday–Friday, excluding holidays).
  - During business hours: routes to active on-duty queue.
  - After hours: checks `is_on_call` agents and dispatches structured on-call payload (webhooks / on-call notifications).
- **Audit Persistence:** Records an entry in `escalation_audits` and sets `conversations.is_escalated = True`.

---

## 4. Proposed Implementation Phases

### Phase 1: Core Classifier & Trajectory Calculator
- Implement `schemas.py` with strict Pydantic v2 DTOs.
- Implement `classifier.py` with multi-class and sarcasm detection, including fallback heuristics.
- Implement `trajectory.py` for sliding-window mathematical slope calculation over dialog history.
- Write unit tests in `backend/tests/test_task5_phase1.py`.

### Phase 2: Dynamic Tone Adapter & Prompt Injection Defenses
- Implement `tone_adapter.py` integrating with `RAGGenerator`.
- Ensure tone instructions do not allow adversarial prompt injection overrides through sentiment fields.
- Write tone adaptation and guardrail tests in `backend/tests/test_task5_phase2.py`.

### Phase 3: Immediate Escalation, After-Hours Routing & Audit Persistence
- Implement `escalation.py` with safety keyword scanners, sustained frustration threshold gates, and operational calendar evaluation.
- Integrate PostgreSQL persistence for `sentiment_logs` and `escalation_audits` under `app_user` permissions.
- Implement after-hours on-call routing logic and dispatch triggers.
- Write escalation and audit trail tests in `backend/tests/test_task5_phase3.py`.

### Phase 4: Full Chat Endpoint Integration & Regression Validation
- Connect `SentimentService` into Step 8 of `MultilingualOrchestrator` (`backend/app/modules/multilingual/orchestrator.py`).
- Validate end-to-end flow from `POST /api/v1/chat/message`:
  Customer Input $\to$ Language Detection $\to$ Translation $\to$ RAG $\to$ Sentiment Evaluation $\to$ Tone Modulation $\to$ Outbound Translation $\to$ Outbox Persistence.
- Run complete regression suite across all 561 existing tests + new Day 08 tests.

---

## 5. Testing & Verification Strategy

| Test Category | Target Coverage | Key Assertions |
| :--- | :--- | :--- |
| **Sentiment Unit Tests** | 6 classes + sarcasm | Sarcasm is never classified as positive; confidence $\in [0, 1]$. |
| **Trajectory Tests** | Slope calculation | 5-turn sliding window detects worsening customer mood. |
| **Tone Adaptation Tests** | Prompt modulation | Empathy injected without altering policy return constraints. |
| **Escalation Trigger Tests** | Legal / Safety gates | Legal threats immediately flip `requires_escalation = True`. |
| **After-Hours Routing Tests**| Timezone & calendar | Weekend / evening calls route to `is_on_call = True` staff. |
| **Database Persistence Tests**| PostgreSQL live | `sentiment_logs` and `escalation_audits` records committed. |
| **Regression Suite** | 561 platform tests | 100% pass rate maintained under least-privilege `app_user`. |

---

## 6. Risks, Boundaries & Safeguards

1. **No Breaking Database Schema Changes:** All required tables (`sentiment_logs`, `escalation_audits`, `sla_calendars`, `users`, `conversations`) exist and are tested.
2. **Deterministic Fallback:** If the LLM gateway latency exceeds 200ms or fails, the lexical sentiment fallback ensures zero downtime and safe fail-closed escalation.
3. **Immutability Protection:** `sentiment_logs` and `escalation_audits` have strict triggers and cannot be deleted or altered by `app_user`.
4. **Preservation of Existing Functionality:** Multilingual orchestration, idempotency guarantees, and FAISS vector index remain 100% intact.

