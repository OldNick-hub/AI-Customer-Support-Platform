# Module Dependencies & Integration Strategy
## Unified Intelligent Customer Support AI Platform

**Document Version:** 1.1.0  
**Design Pattern:** Modular Monolith with Domain-Driven Interservice Contracts  
**Status:** Approved Architectural Baseline (Day 01 Audit Corrections Applied)  

---

## 1. Six-Module Dependency Diagram

The six modules are organized in a **Directed Acyclic Graph (DAG)** to eliminate circular dependencies, ensure predictable execution order, and maintain strict loose coupling.

```mermaid
graph TD
    subgraph Foundation ["Foundational Infrastructure & Common Kernel"]
        DB[(PostgreSQL Store)]
        REDIS[(Redis Cache & Pub/Sub Broker)]
        LLM_GW[Unified LLM Gateway]
        PII_MASK[Sensitive Data Masker & AEAD]
    end

    subgraph KnowledgeTier ["Knowledge & Policy Subsystem"]
        M1["Module 1: Automated KB Pipeline\n- Ingestion, Deduplication, 15/30/60m Retry\n- Golden QA Gate (0% Crit Policy Reg)\n- Maintenance Window Swap & Watchdog"]
        M4["Module 4: Advanced RAG Assistant\n- Authorized Metadata Pre-Filter\n- Temporal Policy (effective_as_of_date)\n- Grounded Citations & Hallucination Guard"]
    end

    subgraph ConversationalTier ["Customer Interaction Subsystem"]
        M6["Module 6: Multilingual Engine\n- Language Detection & Code-Switching\n- Deterministic Entity Tokenization & Restore\n- Context Memory (10+ turns)"]
        M5["Module 5: Sentiment & Escalation\n- 6-Class Multi-Class + Sarcasm\n- Frustration Trajectory Slope\n- Adaptive Tone Modulator"]
        M2["Module 2: Multimodal Analyzer\n- Persistent Staging & Fast 202 Handshake\n- OCR & Claim Cross-Verification\n- Visual Injection Guard"]
    end

    subgraph HelpdeskTier ["Resolution & Dispatch Subsystem"]
        M3["Module 3: Intelligent Ticketing\n- Dynamic Priority Scoring\n- Calendar & Holiday SLA\n- PENDING_CUSTOMER Pause Handling\n- Runtime SLA Recalculation"]
    end

    %% Dependencies
    M1 -->|Generates Active Vector Index (BIGINT)| M4
    M6 -->|Passes Translated & Preserved Tokens| M5
    M5 -->|Passes Sentiment & Tone Context| M4
    M4 -->|Passes Unresolved Queries & Citations| M3
    M2 -->|Passes Extracted & Verified Entities| M3
    M5 -->|Passes 15m Inactivity & High-Risk Alerts| M3

    %% Foundational bindings
    M1 -.-> DB
    M4 -.-> DB
    M6 -.-> REDIS
    M5 -.-> LLM_GW
    M4 -.-> LLM_GW
    M2 -.-> LLM_GW
    M3 -.-> DB
    M2 -.-> PII_MASK
    M3 -.-> PII_MASK
```

---

## 2. Interservice Contracts & Data Flow

### 2.1 Contract: Module 6 (Multilingual) $\rightarrow$ Module 5 (Sentiment)
- **Interface:** `MultilingualEngineInterface.process_incoming_turn()`
- **Output Schema:**
```python
from uuid import UUID
from pydantic import BaseModel

class NormalizedTurnPayload(BaseModel):
    conversation_id: UUID
    session_token: str
    original_text: str
    normalized_english_text: str
    detected_language: str
    language_confidence: float
    code_switching_detected: bool
    preserved_entities: list[dict[str, str]] # [{'type': 'ORDER_ID', 'value': 'ORD-991'}]
    token_map: dict[str, str] # {'__ENTITY_ORDER_0__': 'ORD-991'} for deterministic restoration
    dialog_turn_index: int
```
- **Rationale:** Sentiment analysis models yield higher calibration on normalized text, while preserving customer entities (names, order IDs, SKUs) through deterministic placeholders.

### 2.2 Contract: Module 5 (Sentiment) $\rightarrow$ Module 4 (RAG)
- **Interface:** `SentimentEngineInterface.evaluate_trajectory()`
- **Output Schema:**
```python
class SentimentContext(BaseModel):
    sentiment_class: str # positive, neutral, negative, frustrated, urgent, sarcastic
    confidence: float
    frustration_index: float # 0.0 to 1.0
    urgency_index: float
    trajectory_slope: float # Negative indicates deteriorating customer emotion
    adaptive_tone_directive: str # 'empathetic_reassurance', 'concise_factual', etc.
    immediate_escalation_flag: bool
    escalation_reason: str | None
```
- **Rationale:** Module 4 injects the `adaptive_tone_directive` into the LLM prompt without altering factual retrieved policy chunks. Sarcastic sentiment prevents misplaced positive modulation.

### 2.3 Contract: Module 4 (RAG) $\rightarrow$ Module 3 (Ticketing)
- **Interface:** `RAGAssistantInterface.generate_grounded_answer()`
- **Output Schema:**
```python
from typing import Any

class RAGResolutionResult(BaseModel):
    is_resolved: bool
    refusal_triggered: bool # True if similarity < 0.72 or policy absent
    refusal_reason: str | None
    generated_response: str
    citations: list[dict[str, Any]]
    suggested_ticket_category: str | None
    confidence_score: float
    effective_as_of_date_applied: str | None
```
- **Rationale:** If `refusal_triggered` is `True` or customer expresses dissatisfaction, the orchestrator triggers Module 3 to synthesize a structured ticket.

### 2.4 Contract: Module 2 (Evidence) $\rightarrow$ Module 3 (Ticketing)
- **Interface:** `MultimodalAnalyzerInterface.verify_evidence()`
- **Output Schema:**
```python
from decimal import Decimal

class EvidenceVerificationReport(BaseModel):
    evidence_id: UUID
    status: str # STAGED, VERIFIED_MATCH, PARTIAL_MISMATCH, TOTAL_CONFLICT, LOW_QUALITY, FLAGGED_UNSAFE
    staged_storage_uri: str
    extracted_order_id: str | None
    extracted_amount: Decimal | None
    extracted_product_sku: str | None
    extracted_error_code: str | None
    pii_masked_ocr: str
    discrepancy_summary: str | None
    clarification_prompt: str | None
    is_safe: bool
```
- **Rationale:** Module 3 links the verified entities directly into the ticket description and sets priority.

### 2.5 Contract: Module 1 (Knowledge Pipeline) $\rightarrow$ Module 4 (RAG)
- **Interface:** `KnowledgeGovernanceInterface.get_active_index_metadata()`
- **Output Schema:**
```python
from datetime import datetime

class ActiveIndexMetadata(BaseModel):
    index_version_tag: str
    index_type: str = "IndexIDMap2(IndexFlatIP)"
    vector_dim: int = 384
    total_vectors: int
    vector_id_mapping: str = "BIGINT faiss_vector_id"
    faiss_index_memory_ptr: Any # In-memory reference
    effective_policy_registry: dict[str, Any]
    last_swapped_at: datetime
```
- **Rationale:** Module 4 performs similarity search exclusively against the in-memory `IndexIDMap2` index promoted during the maintenance window and signaled via Redis Pub/Sub.

---

## 3. Orchestration & Facade Architecture

To ensure clean separation between API routers and individual modules, a master orchestrator (`CustomerSupportOrchestrator`) mediates conversational lifecycles:

```python
from typing import Optional
from datetime import date

class CustomerSupportOrchestrator:
    """
    Master facade executing the unified conversational lifecycle:
    M6 (Multilingual) -> M5 (Sentiment) -> M4 (RAG) -> M3 (Ticketing)
    """
    def __init__(
        self,
        multilingual_service: MultilingualService,
        sentiment_service: SentimentService,
        rag_service: RAGService,
        ticketing_service: TicketingService,
        evidence_service: MultimodalService,
    ):
        self.m6 = multilingual_service
        self.m5 = sentiment_service
        self.m4 = rag_service
        self.m3 = ticketing_service
        self.m2 = evidence_service

    async def handle_customer_turn(self, turn_request: CustomerTurnRequest) -> CustomerTurnResponse:
        # Step 1: Normalize language, tokenize entities & maintain session memory (M6)
        normalized = await self.m6.process_incoming_turn(turn_request)
        
        # Step 2: Classify sentiment and calculate trajectory slope (M5)
        sentiment = await self.m5.evaluate_trajectory(normalized)
        
        # Step 3: Check for immediate safety / high-risk escalation
        if sentiment.immediate_escalation_flag:
            ticket = await self.m3.create_emergency_ticket(normalized, sentiment)
            return CustomerTurnResponse.escalated(ticket, sentiment)

        # Step 4: Perform authorized metadata pre-filtering & grounded RAG retrieval (M4)
        # Passes effective_as_of_date and access_tier for temporal & role scoping
        rag_result = await self.m4.generate_grounded_answer(
            normalized=normalized,
            sentiment=sentiment,
            access_tier=turn_request.access_tier,
            effective_as_of_date=turn_request.effective_as_of_date,
        )
        
        # Step 5: If RAG cannot resolve, trigger ticket completeness check (M3)
        if rag_result.refusal_triggered:
            ticket_flow = await self.m3.prepare_ticket_or_prompt_missing(normalized, rag_result)
            return CustomerTurnResponse.handoff_prompt(ticket_flow)

        # Step 6: Restore preserved entity tokens into localized response (M6)
        final_reply = self.m6.restore_entities(rag_result.generated_response, normalized.token_map)
        rag_result.generated_response = final_reply

        return CustomerTurnResponse.answered(rag_result, sentiment)
```

---

## 4. Circular Dependency Prevention Protocol

1. **Layer Rules:**
   - Modules may depend on `core/`, `models/`, `schemas/`, and `services/`.
   - Domain modules (M1 to M6) **MUST NEVER** directly import or call each other.
   - All inter-module communication is coordinated strictly through `CustomerSupportOrchestrator` or Celery task events.
2. **Type Checking:**
   - Enforced in CI via `flake8`, `ruff`, and `mypy` with import boundary checkers.
