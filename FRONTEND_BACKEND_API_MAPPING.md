# FRONTEND TO BACKEND API MAPPING SPECIFICATION
## Comprehensive Screen-to-Endpoint Integration Contract

**Document Version:** 1.0.0  
**Audit Date:** 2026-11-05  
**API Standard:** RESTful JSON & Bidirectional WebSocket (RFC 6455)  
**Base URL:** `/api/v1`  
**Security Model:** Dual Authentication Boundary (Session Token vs. OAuth2 Bearer JWT)  

---

## 1. Overview & Architecture Standards

This document establishes the authoritative contract mapping every planned frontend view, user interaction, and UI component directly to backend API endpoints defined in [`docs/architecture/API_SPECIFICATION.md`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/docs/architecture/API_SPECIFICATION.md).

### 1.1 Dual Authentication Demarcation
To eliminate privilege escalation, authentication is strictly split across two orthogonal headers:
1. **Customer Portal (`X-Session-Token: sess_<uuid>_<hex>`):**
   - Authorizes external customers accessing their own conversation history, sending chat messages, and uploading return evidence.
   - 30-minute idle TTL enforced by Redis and PostgreSQL.
2. **Staff / Agent Console (`Authorization: Bearer <jwt_token>`):**
   - Authorizes internal enterprise agents, supervisors, and platform administrators.
   - JWT payload contains `sub`, `email`, `department`, and `roles` (`AGENT`, `SUPERVISOR`, `ADMIN`, `AUDITOR`).
   - Enforced by backend dependency `require_role(...)`.
3. **Mutual Exclusivity:** The backend rejects any request supplying both headers simultaneously with HTTP `401 Unauthorized`.

### 1.2 Envelope Standards
- **Success:**
  ```json
  {
    "success": true,
    "data": { ... },
    "meta": {
      "timestamp": "2026-11-05T23:45:00.000Z",
      "request_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    }
  }
  ```
- **Error (RFC 7807 Problem Details):**
  ```json
  {
    "success": false,
    "error": {
      "code": "ERROR_CODE_STRING",
      "message": "Human-readable description",
      "details": { ... },
      "request_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    }
  }
  ```

---

## 2. Screen-to-API Master Mapping Matrix

| UI Screen | User Interaction / Feature | UI Component | Method | Backend Endpoint | Auth Header | Current Backend Implementation Status |
| :--- | :--- | :--- | :---: | :--- | :--- | :--- |
| **Customer Portal** | Start or restore customer session | `SessionBootstrap` | `POST` | `/api/v1/auth/customer-session` | None (Public) | **Planned (Day 14)** (Simulated via test seed tokens) |
| **Customer Portal** | Send natural language support query | `MessageInput` / `ChatBubble` | `POST` | `/api/v1/chat/message` | `X-Session-Token` | **IMPLEMENTED & VERIFIED (Day 06/07)** |
| **Customer Portal** | Real-time bidirectional streaming | `StreamingView` | `WS` | `/api/v1/chat/stream/{session_token}` | Query `?token=` | **Planned (Day 14)** |
| **Customer Portal** | Upload receipt / invoice / evidence | `DropZone` / `EvidencePreview` | `POST` | `/api/v1/evidence/upload` | `X-Session-Token` | **Planned (Day 10 - Task 2)** |
| **Customer Portal** | Poll background OCR status | `EvidenceStatusIndicator` | `GET` | `/api/v1/evidence/{evidence_id}` | `X-Session-Token` | **Planned (Day 10 - Task 2)** |
| **Agent Workspace** | Staff login & credential validation | `LoginForm` | `POST` | `/api/v1/auth/login` | None (Public) | **Planned (Day 12/14)** |
| **Agent Workspace** | Fetch ticket list with filters | `TicketQueueTable` / `Kanban` | `GET` | `/api/v1/tickets` | `Authorization: Bearer` | **Planned (Day 12 - Task 3)** |
| **Agent Workspace** | View ticket detail & masked summary | `TicketDetailPane` | `GET` | `/api/v1/tickets/{ticket_id}` | `Authorization: Bearer` | **Planned (Day 12 - Task 3)** |
| **Agent Workspace** | Change status (e.g. pause SLA) | `StatusDropdown` | `PATCH` | `/api/v1/tickets/{ticket_id}/status` | `Authorization: Bearer` | **Planned (Day 13 - Task 3)** |
| **Agent Workspace** | Manual ticket synthesis override | `ManualTicketModal` | `POST` | `/api/v1/tickets` | `Authorization: Bearer` | **Planned (Day 12 - Task 3)** |
| **Agent Workspace** | Supervisor recalculate SLA | `SLARecalculateButton` | `POST` | `/api/v1/tickets/sla/recalculate` | `Authorization: Bearer` | **Planned (Day 13 - Task 3)** |
| **Knowledge Ops** | Trigger document sync & staging | `SyncDocumentsButton` | `POST` | `/api/v1/knowledge/sync` | `Authorization: Bearer` | **Planned (Day 14)** (Core logic implemented in Task 1) |
| **Knowledge Ops** | Activate candidate vector index | `ActivateIndexModal` | `POST` | `/api/v1/knowledge/activate-candidate` | `Authorization: Bearer` | **Planned (Day 14)** (Core logic implemented in Task 1) |
| **Knowledge Ops** | Emergency index rollback | `RollbackButton` | `POST` | `/api/v1/knowledge/rollback` | `Authorization: Bearer` | **Planned (Day 14)** (Core logic implemented in Task 1) |
| **System Health** | Liveness probe check | `HealthStatusPill` | `GET` | `/api/v1/health/live` | Public | **IMPLEMENTED (Day 01)** |
| **System Health** | Dependency readiness check | `DependencyGrid` | `GET` | `/api/v1/health/ready` | Public | **IMPLEMENTED (Day 01)** |

---

## 3. Deep-Dive Screen Integration Contracts

### 3.1 Screen: `CustomerPortal.tsx`

#### A. Send Chat Message
* **Endpoint:** `POST /api/v1/chat/message`
* **Trigger:** Customer types inquiry and presses Enter or clicks Send.
* **Headers:**
  * `Content-Type: application/json`
  * `X-Session-Token: sess_<token>`
  * `X-Request-ID: <client-uuid>`
* **Request Body (`ChatMessageRequest`):**
  ```json
  {
    "message": "Can I return a opened laptop purchased 20 days ago?",
    "conversation_id": "f811c7f0-6891-4f5d-a257-648a71569ce2",
    "preferred_language": "en",
    "product_line": "ELECTRONICS",
    "idempotency_key": "idem_4f89d301ba87"
  }
  ```
* **Response Body (`ChatMessageResponse`):**
  ```json
  {
    "success": true,
    "data": {
      "message_id": "861de5fb-c502-4a29-a18b-e02a0f6ec8a0",
      "reply_text": "Computers and laptops can be returned within 15 days of delivery. Because 20 days have passed, this item is outside the standard return window.",
      "is_resolved": true,
      "refusal_triggered": false,
      "refusal_reason": null,
      "clarification_needed": false,
      "detected_language": "en",
      "detected_language_confidence": 0.99,
      "primary_language": "en",
      "context_restored": false,
      "turn_sequence_number": 3,
      "conversation_id": "f811c7f0-6891-4f5d-a257-648a71569ce2",
      "citations": [
        {
          "citation_id": 1,
          "document_code": "POL-RET-US-2026",
          "document_title": "US Standard Return Policy 2026",
          "version_number": 1,
          "effective_date": "2026-01-01",
          "chunk_excerpt": "Computers, laptops, and tablets must be returned within 15 calendar days."
        }
      ],
      "confidence_score": 0.94,
      "model_used": "gemini-2.5-flash",
      "total_latency_ms": 780.5
    },
    "meta": {
      "timestamp": "2026-11-05T23:45:00.000Z",
      "request_id": "client-generated-uuid"
    }
  }
  ```

#### B. Upload Multimodal Evidence
* **Endpoint:** `POST /api/v1/evidence/upload`
* **Trigger:** Customer drags invoice or photo into `DropZone`.
* **Headers:**
  * `Content-Type: multipart/form-data`
  * `X-Session-Token: sess_<token>`
* **Form Data:**
  * `file`: Binary file (`.png`, `.jpg`, `.pdf`, max 20MB)
  * `ticket_id`: UUID (optional)
  * `conversation_id`: UUID
* **Response Format (Synchronous Fast Path $\le 3$s):**
  ```json
  {
    "success": true,
    "data": {
      "evidence_id": "3c9b7b91-9e7f-44a5-b7f3-e5e6e3c27184",
      "filename": "receipt_bestbuy.png",
      "status": "VERIFIED",
      "is_fast_path": true,
      "extracted_data": {
        "invoice_number": "BB-992014",
        "purchase_date": "2026-10-15",
        "total_amount": 899.99,
        "currency": "USD"
      }
    }
  }
  ```
* **Response Format (Asynchronous Offload >3s):**
  HTTP `202 Accepted` with `task_id` and `poll_url: /api/v1/evidence/{evidence_id}`.

---

### 3.2 Screen: `AgentWorkspace.tsx`

#### A. Ticket Queue Inspection & Filtering
* **Endpoint:** `GET /api/v1/tickets?status=OPEN&priority=CRITICAL&page=1&limit=20`
* **Headers:** `Authorization: Bearer <jwt_token>`
* **Response Body:**
  ```json
  {
    "success": true,
    "data": {
      "items": [
        {
          "id": "c1f7b889-7e23-41a4-9781-817bf4083a3f",
          "ticket_number": "TCK-202611-00042",
          "title": "Damaged Laptop Screen on Delivery",
          "customer_tier": "VIP",
          "priority": "HIGH",
          "priority_score": 4.25,
          "status": "OPEN",
          "sla_target_minutes": 240,
          "sla_warning_at": "2026-11-06T02:00:00Z",
          "sla_breach_at": "2026-11-06T03:00:00Z",
          "sla_paused_at": null,
          "total_paused_duration_seconds": 0,
          "sentiment_trajectory": {
            "current_class": "frustrated",
            "frustration_index": 0.88,
            "slope": 0.32
          }
        }
      ],
      "total": 1,
      "page": 1,
      "pages": 1
    }
  }
  ```

#### B. Update Ticket Status (SLA Pause Handling)
* **Endpoint:** `PATCH /api/v1/tickets/{ticket_id}/status`
* **Trigger:** Agent requests additional information from customer, setting status to `PENDING_CUSTOMER`.
* **Headers:** `Authorization: Bearer <jwt_token>`
* **Request Body:**
  ```json
  {
    "status": "PENDING_CUSTOMER",
    "reason": "Awaiting customer serial number confirmation"
  }
  ```
* **Backend Behavior:**
  - Sets `tickets.status = 'PENDING_CUSTOMER'`.
  - Sets `tickets.sla_paused_at = NOW()`.
  - Records event in `ticket_events`.
  - Emits webhook to agent console.

---

### 3.3 Screen: `KnowledgeOps.tsx`

#### Emergency Index Rollback
* **Endpoint:** `POST /api/v1/knowledge/rollback`
* **Trigger:** Supervisor discovers retrieval anomalies and confirms emergency rollback.
* **Headers:** `Authorization: Bearer <jwt_token>` (Requires `SUPERVISOR` or `ADMIN` role)
* **Request Body:**
  ```json
  {
    "reason": "Anomalous benchmark drift detected on legal policy documents",
    "force": true
  }
  ```
* **Response Body:**
  ```json
  {
    "success": true,
    "data": {
      "restored_version": "faiss_index_v1",
      "rolled_back_at": "2026-11-05T23:50:00Z",
      "active_vector_count": 250,
      "message": "Vector index successfully rolled back to previous stable generation."
    }
  }
  ```

---

## 4. Summary of Integration Gaps & Delivery Sequencing

1. **Active & Available Today:**
   - `POST /api/v1/chat/message`: Fully functional with dual-auth, multilingual, RAG citations, idempotency caching, and sequence validation.
   - `GET /api/v1/health/live` & `GET /api/v1/health/ready`: Fully functional.
2. **Scheduled in Master Plan (Days 08–13):**
   - **Day 08/09 (Task 5):** Sentiment trajectory scoring, tone adaptation, and on-call escalation triggers.
   - **Day 10/11 (Task 2):** Evidence upload (`/api/v1/evidence/upload`), Fast-path OCR, and claim verification.
   - **Day 12/13 (Task 3):** Ticket synthesis (`/api/v1/tickets`), SLA countdowns, and `PENDING_CUSTOMER` pause endpoints.
3. **Frontend Implementation (Day 15):**
   - Day 15 will implement the complete React 18 client matching these exact contracts.

