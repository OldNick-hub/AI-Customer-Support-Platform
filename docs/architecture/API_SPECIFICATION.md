# API Specification Document
## Unified Intelligent Customer Support AI Platform

**Document Version:** 1.1.0  
**API Standard:** RESTful JSON & Bidirectional WebSocket (RFC 6455)  
**Base URL:** `/api/v1`  
**Security:** Dual Authentication Boundary (Session Tokens for Customers, OAuth2 Bearer JWT for Staff)  
**Status:** Approved Architectural Baseline (Day 01 Audit Corrections Applied)  

---

## 1. Global API Conventions

### 1.1 Content Types & Headers
- All HTTP request payloads must specify: `Content-Type: application/json` (except file uploads which use `multipart/form-data`).
- All HTTP responses return `Content-Type: application/json; charset=utf-8`.
- Distributed Tracing: The client may pass `X-Request-ID: <uuid>`. If omitted, the gateway generates one and echoes it in the response header `X-Request-ID`.

### 1.2 Unified Success Envelope Format
```json
{
  "success": true,
  "data": { ... },
  "meta": {
    "timestamp": "2026-11-01T01:15:00.000Z",
    "request_id": "c1f7b889-7e23-41a4-9781-817bf4083a3f"
  }
}
```

### 1.3 Standard Error Format (RFC 7807 Problem Details)
```json
{
  "success": false,
  "error": {
    "code": "CLAIM_EVIDENCE_CONFLICT",
    "message": "The invoice amount ($149.99) does not match the refund claim ($250.00).",
    "details": {
      "claimed_amount": 250.00,
      "detected_amount": 149.99,
      "confidence": 0.98
    },
    "request_id": "c1f7b889-7e23-41a4-9781-817bf4083a3f"
  }
}
```

---

## 2. Authentication, Session Management & Endpoint Authorization Matrix

### 2.1 Dual Authentication Model
1. **Customer Session Authentication:**
   - Transport: Header `X-Session-Token: sess_<uuid>_<random_hex>` or query param `?token=` on WebSocket.
   - Lifecycle: 30-minute idle TTL. Returning within 24 hours restores prior context summary.
   - Scope: Strictly limited to customer's own conversation turns, attachments, and feedback.
2. **Staff Enterprise Authentication:**
   - Transport: Header `Authorization: Bearer <jwt_token>`.
   - Payload: Subject UUID (`sub`), email, department, and roles (`AGENT`, `SUPERVISOR`, `ADMIN`).
   - Scope: Enforced via FastAPI dependency injection guards (`require_role(...)`).

### 2.2 Endpoint-Level Authorization Matrix

| Endpoint Path | Method | Auth Type | Minimum Role / Scope | Rejection Semantics | Description |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `/api/v1/auth/customer-session` | POST | Public | Anyone | 400 Bad Request | Create or reconnect customer session. |
| `/api/v1/auth/login` | POST | Public | Anyone | 401 Unauthorized | Staff login; issues JWT token. |
| `/api/v1/chat/message` | POST | Session / JWT | `CUSTOMER`, `AGENT` | 401 (missing), 403 (unauthorized) | Send multi-turn conversational chat turn. |
| `/api/v1/chat/stream/{session_token}` | WS | Session Token | `CUSTOMER`, `AGENT` | 4401 WS Close Code | Bidirectional streaming chat. |
| `/api/v1/evidence/upload` | POST | Session / JWT | `CUSTOMER`, `AGENT` | 401 (missing), 413 (size), 415 (MIME) | Persistent staging & fast OCR / async 202. |
| `/api/v1/evidence/{evidence_id}` | GET | Session / JWT | `CUSTOMER` (own), `AGENT` | 401, 403, 404 | Poll background evidence OCR status. |
| `/api/v1/tickets` | POST | JWT / System | `AGENT`, `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | Synthesize support ticket. |
| `/api/v1/tickets` | GET | JWT Bearer | `AGENT`, `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | Search, filter & paginate ticket queue. |
| `/api/v1/tickets/{ticket_id}` | GET | JWT Bearer | `AGENT`, `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | View ticket detail & masked brief. |
| `/api/v1/tickets/{ticket_id}/status` | PATCH | JWT Bearer | `AGENT`, `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | Update status (e.g. `PENDING_CUSTOMER`). |
| `/api/v1/tickets/sla/recalculate` | POST | JWT Bearer | `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | Trigger runtime SLA recalculation. |
| `/api/v1/knowledge/sync` | POST | JWT Bearer | `ADMIN` Only | 401 Unauthorized, 403 Forbidden | Initiate document sync & candidate build. |
| `/api/v1/knowledge/activate-candidate`| POST | JWT Bearer | `ADMIN` Only | 401, 403, 409 (outside maint) | Promote candidate index in maint window. |
| `/api/v1/knowledge/rollback` | POST | JWT Bearer | `SUPERVISOR`, `ADMIN` | 401 Unauthorized, 403 Forbidden | Immediate vector index rollback. |
| `/api/v1/health/live` | GET | Public | None | None | Liveness probe. |
| `/api/v1/health/ready` | GET | Public | None | 503 if dependencies fail | Readiness probe (DB, Redis, FAISS). |

---

## 3. Conversational Chat & Advanced RAG (Modules 4, 5, 6)

### `POST /api/v1/chat/message`
Synchronous multi-turn conversational interaction combining multilingual translation, sentiment & sarcasm classification, authorized metadata-filtered RAG search, and adaptive tone synthesis.

- **Headers:** 
  - `X-Session-Token: sess_8f2931a0e88c4d2fb2d3c52a0a25619d` (for customer) OR `Authorization: Bearer <jwt>` (for staff)
- **Request Payload:**
```json
{
  "conversation_id": "f51270c1-3d71-4ce8-8f55-22d7650f1122",
  "message": "Mi pedido ORD-88912 no ha llegado y estoy muy frustrado con el retraso.",
  "access_tier": "REGISTERED",
  "effective_as_of_date": null
}
```

#### Temporal Retrieval & Metadata Pre-Filtering Rules:
1. **Current Policies (`effective_as_of_date = null`):**
   - RAG queries strictly match active policies valid as of current date: `effective_from <= CURRENT_DATE` and (`effective_until IS NULL OR effective_until >= CURRENT_DATE`) and `is_current = TRUE`.
   - Proactively excludes expired policies (`effective_until < CURRENT_DATE`) and future-dated draft policies (`effective_from > CURRENT_DATE`).
2. **Historical Policy Auditing (`effective_as_of_date = "YYYY-MM-DD"`):**
   - Enabled for agents/auditors checking terms in effect when an order was placed.
   - Queries match: `effective_from <= effective_as_of_date` and (`effective_until IS NULL OR effective_until >= effective_as_of_date`).
3. **Three-Layer Defense-in-Depth Metadata Isolation Guarantee:**
   - **Layer 1 (PostgreSQL Pre-Filter):** PostgreSQL executes an indexed SQL query filtering chunks by caller `access_tier` (`PUBLIC`, `REGISTERED`, `VIP`, `INTERNAL_AGENT`), `region` (`US`, `EU`, `GLOBAL`), and temporal validity window (`effective_from <= date <= effective_until`). Returns a whitelist of 64-bit integer `faiss_vector_id`s. If candidate set is empty, FAISS search is bypassed immediately.
   - **Layer 2 (FAISS Pre-Filtered Search):** The candidate IDs are converted to `faiss.IDSelectorBatch(valid_ids)` and passed via `faiss.SearchParameters(sel=selector)` to `faiss.IndexIDMap2(faiss.IndexFlatIP(384))`. FAISS strictly restricts distance calculations to the authorized set.
   - **Layer 3 (Application-Layer Security Gate):** The application cross-verifies all returned IDs against the candidate whitelist, strips any unassigned IDs (`-1`), and validates that chunk access tiers $\le$ caller tier before loading chunk text from PostgreSQL. **Zero unauthorized chunks can enter the LLM context.**

- **Response `200 OK`:**
```json
{
  "success": true,
  "data": {
    "message_id": "0192d6e3-1384-72b7-8ce6-7a718b5b5971",
    "reply_text": "Lamento mucho la demora con su pedido ORD-88912. He verificado nuestra política de envíos para su región [1]. Según la normativa vigente, los envíos estándar pueden tardar hasta 5 días hábiles. Si el plazo expiró, podemos gestionar el reemplazo inmediato.",
    "detected_language": "es",
    "language_confidence": 0.99,
    "code_switching_detected": false,
    "preserved_entities": [
      { "type": "ORDER_ID", "value": "ORD-88912" }
    ],
    "sentiment": {
      "class": "frustrated",
      "confidence": 0.94,
      "frustration_index": 0.88,
      "tone_applied": "empathetic_reassurance",
      "escalation_recommended": false
    },
    "citations": [
      {
        "citation_id": 1,
        "document_code": "POL-SHIPPING-LATAM-2026",
        "document_title": "Directiva de Tiempos de Entrega y Envíos Regionales",
        "version_number": 2,
        "effective_date": "2026-01-15",
        "chunk_excerpt": "Los envíos estándar a zonas metropolitanas tienen un plazo garantizado de 3 a 5 días hábiles..."
      }
    ],
    "clarification_needed": false
  }
}
```

---

## 4. Multimodal Evidence Analyzer (Module 2)

### `POST /api/v1/evidence/upload`
Uploads and parses customer invoices, screenshots, or receipts. Files are immediately validated, cryptographically hashed, and written to persistent staging disk before task dispatch.

- **Access Level:** Customer / Agent
- **Content-Type:** `multipart/form-data`
- **Form Parameters:**
  - `file`: Binary file (PDF, PNG, JPG, WebP $\le 20$MB).
  - `conversation_id`: UUID of ongoing conversation.
  - `claimed_order_id`: String (optional, e.g. "ORD-99124").
  - `claimed_amount`: Float (optional, e.g. 149.99).

- **Two-Tier Latency & Background Processing Model:**
  - **Tier 1 (Synchronous Fast-Path $\le 3.0$s):** For lightweight single images ($\le 1$MB), OCR, PII masking, entity extraction, and claim validation complete within 3.0 seconds, returning `200 OK` directly for seamless user experience.
  - **Tier 2 (Asynchronous Queueing with 30s Background Ceiling):** Uploads requiring $>3.0$ seconds (or identified upfront as multi-page PDFs or high-resolution images $>1$MB) are immediately committed to persistent staging and dispatched to Celery, returning `202 Accepted` within $<3.0$ seconds. This prevents HTTP connection pool starvation and gateway 504 timeouts. The Celery worker handles deep OCR/vision extraction for tasks requiring up to 30 seconds (with a 60s soft and 90s hard Celery timeout).
  - **Customer Task Notification:** When queued asynchronously, the customer immediately receives an explicit task status notification: *"Your document is being analyzed in the background. You will be notified as soon as analysis is complete."*. Real-time completion updates are pushed via WebSocket (`ws/chat/{conversation_id}`) with polling fallback via `GET /api/v1/evidence/{evidence_id}`.

- **Response `200 OK` (Fast-Path Synchronous $\le 3.0$s):**
  Returned for single small images where OCR and claim checks complete in $\le 3.0$ seconds.
```json
{
  "success": true,
  "data": {
    "evidence_id": "4a719283-8472-46a2-9721-65f8c6b73812",
    "status": "VERIFIED",
    "mime_type": "image/png",
    "file_size_bytes": 482120,
    "security_verdict": {
      "is_safe": true,
      "prompt_injection_detected": false
    },
    "extracted_entities": {
      "order_id": "ORD-99124",
      "date": "2026-10-24",
      "total_amount": 149.99,
      "currency": "USD",
      "product_sku": "SKU-MONITOR-4K",
      "error_code": "ERR_DISPLAY_PORT_NO_SIGNAL"
    },
    "claim_verification": {
      "matches_order_id": true,
      "matches_amount": true,
      "status": "VERIFIED_MATCH",
      "discrepancy_details": null
    },
    "clarification_required": false
  }
}
```

- **Response `202 Accepted` (Immediate Asynchronous Handshake):**
  Returned immediately for multi-page PDFs, high-res files, or when sync time exceeds 3.0s. The HTTP connection is NOT held for 30s.
```json
{
  "success": true,
  "data": {
    "evidence_id": "4a719283-8472-46a2-9721-65f8c6b73812",
    "status": "PROCESSING_BACKGROUND",
    "task_id": "celery-task-9a721-8273-bbcd",
    "estimated_seconds": 15,
    "customer_notification": "Your document is being analyzed in the background. You will be notified as soon as analysis is complete.",
    "poll_url": "/api/v1/evidence/4a719283-8472-46a2-9721-65f8c6b73812"
  }
}
```

### `GET /api/v1/evidence/{evidence_id}`
Polls the processing status of an asynchronously queued evidence artifact.
- **Access Level:** Customer (uploader) / Agent
- **Response `200 OK`:** Returns full entity and verification payload once status reaches `VERIFIED`, `CLAIM_CONFLICT`, or `FLAGGED_UNSAFE`.

---

## 5. Intelligent Ticket Management (Module 3)

### `POST /api/v1/tickets`
Converts an unresolved or escalated conversation into a structured support ticket. Enforces mandatory completeness checks and calculates dynamic priority.
- **Access Level:** Agent / System Automated
- **Request Payload:**
```json
{
  "conversation_id": "f51270c1-3d71-4ce8-8f55-22d7650f1122",
  "customer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "category": "HARDWARE_DEFECT",
  "mandatory_fields": {
    "order_id": "ORD-99124",
    "product_sku": "SKU-MONITOR-4K",
    "issue_description": "Monitor displays 'ERR_DISPLAY_PORT_NO_SIGNAL' after 10 minutes of use.",
    "preferred_contact_method": "EMAIL"
  }
}
```
- **Response `201 Created`:**
```json
{
  "success": true,
  "data": {
    "ticket_id": "7f8b9c0d-1e2f-4a5b-8c9d-0e1f2a3b4c5d",
    "ticket_number": "TCK-202611-00042",
    "status": "OPEN",
    "priority": "HIGH",
    "priority_score": 4.15,
    "assigned_agent": {
      "id": "agent-id-1234",
      "full_name": "Marcus Vance",
      "department": "Hardware Escalations"
    },
    "sla_metrics": {
      "calendar": "US_COMMERCIAL_HOURS",
      "target_minutes": 240,
      "sla_warning_at": "2026-11-01T04:15:00.000Z",
      "sla_breach_at": "2026-11-01T05:15:00.000Z",
      "is_paused": false
    },
    "masked_summary": "Customer Jane Doe reported persistent signal loss on SKU-MONITOR-4K. Extracted error: ERR_DISPLAY_PORT_NO_SIGNAL. Customer sentiment showed sustained frustration (0.88). Payment details masked: Visa ending in 1234."
  }
}
```

### `PATCH /api/v1/tickets/{ticket_id}/status`
Updates ticket status. Supporting `PENDING_CUSTOMER` pause semantics.
- **Access Level:** Agent, Supervisor, Admin
- **Request Payload:**
```json
{
  "status": "PENDING_CUSTOMER",
  "reason": "Requested higher resolution screenshot of receipt."
}
```
- **Response `200 OK`:**
```json
{
  "success": true,
  "data": {
    "ticket_id": "7f8b9c0d-1e2f-4a5b-8c9d-0e1f2a3b4c5d",
    "previous_status": "OPEN",
    "current_status": "PENDING_CUSTOMER",
    "sla_paused_at": "2026-11-01T02:30:00.000Z",
    "sla_countdown_active": false
  }
}
```

---

## 6. Knowledge Base Pipeline & Governance (Module 1)

### `POST /api/v1/knowledge/sync`
Triggers an automated synchronization pass to detect new/modified files, check SHA-256 hashes, quarantine invalid files, and initiate candidate index building.
- **Access Level:** Admin Only
- **Request Payload:**
```json
{
  "source_directory": "/storage/raw_documents",
  "force_rebuild": false
}
```
- **Response `202 Accepted`:**
```json
{
  "success": true,
  "data": {
    "job_id": "kb-sync-job-20261101-001",
    "status": "SCANNING_AND_INDEXING",
    "message": "Change detection initiated. Candidate index evaluation scheduled. Retries configured for 15, 30, and 60 minutes."
  }
}
```

### `POST /api/v1/knowledge/activate-candidate`
Promotes a verified candidate FAISS index to active status. Activation is strictly constrained to the configured maintenance window unless explicitly overridden.
- **Access Level:** Admin Only
- **Request Payload:**
```json
{
  "candidate_version_tag": "idx_20261101_v2",
  "override_maintenance_window": false
}
```
- **Response `200 OK`:**
```json
{
  "success": true,
  "data": {
    "active_version": "idx_20261101_v2",
    "previous_version": "idx_20261025_v1",
    "grounding_score": 0.94,
    "critical_policy_regression_rate": 0.0,
    "switchover_timestamp": "2026-11-01T02:00:00.000Z",
    "pubsub_reload_channel": "channel:knowledge:index_reload",
    "watchdog_active_until": "2026-11-01T02:05:00.000Z",
    "watchdog_state_persisted": true
  }
}
```

### `POST /api/v1/knowledge/rollback`
Instantly reverts active vector index to the prior stable snapshot.
- **Access Level:** Admin / Automated Watchdog
- **Request Payload:**
```json
{
  "target_version_tag": "idx_20261025_v1",
  "reason": "Health check synthetic canary queries failed post-activation."
}
```
- **Response `200 OK`:**
```json
{
  "success": true,
  "data": {
    "active_version": "idx_20261025_v1",
    "rolled_back_at": "2026-11-01T02:02:15.000Z",
    "status": "RESTORED",
    "pubsub_notified": true
  }
}
```
