# FRONTEND MASTER AUDIT
## React 18 + TypeScript + Vite Web Client Architecture

**Document Version:** 1.0.0  
**Audit Date:** 2026-11-05  
**Scope:** `frontend/` directory, architectural specifications, build infrastructure, and planned UI tier  
**Target:** AI-Customer-Support-Platform  

---

## 1. Executive Summary

This document provides a comprehensive technical audit of the frontend tier of the **AI-Customer-Support-Platform**.
The frontend is designed per architectural specifications (**ADR-06**, **ADR-09**, and `docs/architecture/PROJECT_STRUCTURE.md`) as a modern Single Page Application (SPA) powered by **React 18**, **TypeScript 5**, **Vite 5**, **react-router-dom v6**, and **Axios v1**, styled with an accessible, high-contrast **Vanilla CSS Design Token** system.

### Current Overall Status
* **Provisioning Level:** Minimal scaffold only.
* **Present on Disk:**
  * `frontend/package.json`
  * `frontend/Dockerfile`
* **Missing on Disk:**
  * `index.html` (DOM entrypoint)
  * `vite.config.ts` (Vite bundler configuration)
  * `tsconfig.json` & `tsconfig.node.json` (TypeScript compilation configuration)
  * `.dockerignore`
  * `nginx.conf` (referenced by Dockerfile production stage)
  * `src/` directory (all application code, styles, pages, components, hooks, services, and types)
  * `node_modules/` (dependencies not yet installed)

---

## 2. Inventory of Existing Files

### 2.1 `frontend/package.json`
* **Package Name:** `customer-support-ai-platform-frontend`
* **Version:** `0.1.0`
* **Type:** `module` (ESM)
* **Scripts:**
  * `"dev": "vite"`
  * `"build": "tsc && vite build"`
  * `"preview": "vite preview"`
* **Dependencies Configured:**
  * `react`: `^18.3.1`
  * `react-dom`: `^18.3.1`
  * `react-router-dom`: `^6.26.1`
  * `axios`: `^1.7.7`
* **DevDependencies Configured:**
  * `@types/react`: `^18.3.5`
  * `@types/react-dom`: `^18.3.0`
  * `@vitejs/plugin-react`: `^4.3.1`
  * `typescript`: `^5.5.3`
  * `vite`: `^5.4.2`
* **Evaluation:** The package manifest correctly aligns with ADR-06 and ADR-09. All required production and development dependencies are specified at modern, stable releases.

### 2.2 `frontend/Dockerfile`
* **Stage 1 (Builder):** Uses `node:20-alpine`, runs `npm ci` and `npm run build` targeting `/app/dist`.
* **Stage 2 (Runtime):** Uses `nginx:alpine`, copies build output to `/usr/share/nginx/html`, exposes port `80`, and references `nginx.conf`.
* **Evaluation:** Production-ready multi-stage container build pattern. Requires `nginx.conf` to handle client-side routing fallback (`try_files $uri $uri/ /index.html;`) when deployed.

---

## 3. Detailed Audit of Missing Files & Components

Below is the exhaustive matrix of missing frontend infrastructure and application modules required to realize the planned architecture:

### 3.1 Build & Environment Configuration (Missing)

| Target Path | Category | Purpose & Planned Content |
| :--- | :--- | :--- |
| `frontend/index.html` | Entrypoint | HTML5 document shell, root `<div id="root"></div>`, viewport meta tags, font preloading, and `<script type="module" src="/src/main.tsx">`. |
| `frontend/vite.config.ts` | Tooling | Vite configuration with `@vitejs/plugin-react`, path aliases (`@/` $\to$ `/src`), build output directory, and development server proxy (`/api` $\to$ `http://localhost:8000`). |
| `frontend/tsconfig.json` | Compiler | Strict TypeScript config (`target: ES2022`, `lib: ["DOM", "DOM.Iterable", "ESNext"]`, `strict: true`, `jsx: react-jsx`, `moduleResolution: bundler`). |
| `frontend/tsconfig.node.json` | Compiler | Node TypeScript config for Vite configuration files. |
| `frontend/nginx.conf` | Deployment | Nginx server block with SPA routing fallback (`try_files $uri /index.html`), gzip compression, and security headers. |
| `frontend/.dockerignore` | Deployment | Prevents copying `node_modules`, `dist`, `.git`, and local environment files into Docker contexts. |

---

### 3.2 Global Styling & Theme System (Missing: `src/index.css`)
Per **ADR-06**, styling utilizes a semantic Vanilla CSS Design Token system without heavy utility CSS dependencies. The design system requires:
* **Color Tokens:** Brand primaries, status accents (`success`, `warning`, `danger`, `info`), neutral surfaces (`surface-1`, `surface-2`, `surface-elevated`).
* **Glassmorphism Tokens:** Background blurs (`backdrop-filter: blur(12px)`), translucent border highlights, subtle drop shadows.
* **Typography:** System font stacks (`Inter`, system-ui), fluid modular type scales, strict line heights.
* **Accessibility (WCAG 2.1 AA):** High-contrast color ratios ($\ge 4.5:1$ for normal text, $\ge 3:1$ for large text/icons), explicit `:focus-visible` outline rings, and dark/light mode toggle support (`data-theme="dark"`).
* **Responsive Breakpoints:** Mobile (`< 768px`), Tablet (`768px - 1024px`), Desktop (`> 1024px`).

---

### 3.3 Core Application Shell (Missing)

| Target Path | Component | Description |
| :--- | :--- | :--- |
| `src/main.tsx` | Root Mount | `ReactDOM.createRoot` mounting `App` inside `React.StrictMode` with global CSS imported. |
| `src/App.tsx` | Router Shell | Top-level `BrowserRouter`, navigation bar, authentication state provider, route guards, and error boundaries. |

---

### 3.4 Application Pages / Screens (Missing: `src/pages/`)

The architecture defines four primary application view containers:

```text
src/pages/
├── CustomerPortal.tsx   # Customer conversational chat, streaming & evidence upload
├── AgentWorkspace.tsx   # Enterprise agent & supervisor ticket command center
├── KnowledgeOps.tsx     # Knowledge index versions, promotion & rollback management
└── SystemHealth.tsx     # Platform telemetry, SLA watchdog & dependency health
```

1. **`CustomerPortal.tsx` (Customer Support Hub):**
   - **Target Persona:** External customers (authenticated via `X-Session-Token` or session creation).
   - **Capabilities:**
     - Conversational multi-turn chat with streaming responses.
     - Multilingual language preference toggle (`en`, `es`, `fr`, `de`, `hi`).
     - Real-time resolution state indicators and satisfaction feedback.
     - Multimodal evidence drag-and-drop zone with instant $\le 3$s fast-path verification indicator or async 202 progress tracker.
     - Session persistence across browser reloads (30-minute idle lifecycle).

2. **`AgentWorkspace.tsx` (Agent & Supervisor Command Center):**
   - **Target Persona:** Enterprise support staff (authenticated via OAuth2 Bearer JWT with roles `AGENT`, `SUPERVISOR`, `ADMIN`).
   - **Capabilities:**
     - Real-time ticket queue with dynamic priority badges ($P \in [1, 5]$).
     - Live SLA countdown timers with visual color coding (Normal $\to$ 75% Warning $\to$ Breach).
     - `PENDING_CUSTOMER` pause indicators and duration tracking.
     - PII-masked executive handoff summaries and customer trajectory graphs.
     - Manual status transitions (`IN_PROGRESS`, `PENDING_CUSTOMER`, `RESOLVED`, `ESCALATED`).
     - Multimodal evidence inspection modal with OCR text preview and confidence scores.

3. **`KnowledgeOps.tsx` (Knowledge Base Operations):**
   - **Target Persona:** Administrators & Supervisors (`ADMIN`, `SUPERVISOR`).
   - **Capabilities:**
     - Active vs. candidate FAISS index version table (`faiss_index_v1`, `faiss_index_v2`).
     - Golden benchmark scorecard displaying accuracy, latency, and hallucination refusal rates.
     - Maintenance window scheduled activation countdown.
     - One-click emergency rollback trigger with automated confirmation dialog.

4. **`SystemHealth.tsx` (Operational Observability):**
   - **Target Persona:** System administrators and DevOps engineers.
   - **Capabilities:**
     - Live dependency status probes (PostgreSQL 16, Redis 7, FAISS active index, Gemini LLM gateway).
     - Active worker queue depths (`indexing_queue`, `multimodal_queue`, `sla_watchdog_queue`).
     - Global SLA compliance rate and breach metrics.

---

### 3.5 Reusable Component Hierarchy (Missing: `src/components/`)

```text
src/components/
├── common/
│   ├── Button.tsx           # Standard, outline, danger, loading states
│   ├── Input.tsx            # Form inputs with error labels & clear buttons
│   ├── Modal.tsx            # Accessible dialog with ESC listener & focus trap
│   ├── Badge.tsx            # Priority, sentiment, status, language badges
│   ├── Alert.tsx            # Contextual alert banners (info, warning, error)
│   └── Spinner.tsx          # Animated loading indicator
├── chat/
│   ├── ChatBubble.tsx       # Customer vs. Agent/AI messages with citation tooltips
│   ├── MessageInput.tsx     # Auto-expanding text area with send button & file drop
│   ├── CitationBadge.tsx    # Clickable policy source reference pill with document modal
│   └── StreamingCursor.tsx  # Blinking cursor for token-by-token streaming
├── evidence/
│   ├── DropZone.tsx         # Drag-and-drop file upload with MIME type enforcement
│   ├── EvidencePreview.tsx  # Thumbnail, file size, upload progress bar
│   └── OCRInspector.tsx     # Extracted text pane with confidence highlight overlays
├── tickets/
│   ├── TicketCard.tsx       # Kanban item with priority score, customer tier, category
│   ├── SLACountdown.tsx     # Dynamic ticking countdown clock with pause indicator
│   ├── PriorityBadge.tsx    # Color-coded priority chip (Low, Med, High, Critical, Urgent)
│   └── TrajectoryChart.tsx  # Mini SVG sentiment curve showing last 5 turns
└── knowledge/
    ├── VersionRow.tsx       # Index row with active symlink badge and checksum
    ├── Scorecard.tsx        # Benchmark metric tiles (Accuracy, Latency, Refusal)
    └── RollbackModal.tsx    # Safety confirmation prompt for instant rollback
```

---

### 3.6 Custom Hooks & State Management (Missing: `src/hooks/`)

* **`useAuth.ts`:** Manages dual-authentication state: customer session tokens in `localStorage` / `sessionStorage`, and staff JWT access tokens with automatic expiry checks and decoded roles (`roles: string[]`).
* **`useChat.ts`:** Manages multi-turn conversation state: message history, sending messages, handling idempotency keys, loading states, citations, and error recovery.
* **`useWebSocket.ts`:** Manages RFC 6455 persistent WebSocket connection for live streaming chat, reconnection backoff, heartbeat pings, and real-time outbox notifications.
* **`useTickets.ts`:** Manages ticket queue fetching, sorting by priority score, filtering by assigned agent or status, and status mutations.

---

### 3.7 Networking & API Clients (Missing: `src/services/`)

* **`apiClient.ts`:** Configured Axios instance with baseURL `/api/v1`:
  * **Request Interceptor:** Automatically detects auth context: attaches `X-Session-Token` for customer sessions, or `Authorization: Bearer <token>` for staff JWTs. Attaches `X-Request-ID` for distributed tracing.
  * **Response Interceptor:** Automatically unwraps `{ success: true, data: ... }` envelopes; transforms RFC 7807 error responses into typed application errors.
* **`chatService.ts`:** Typed wrapper for `POST /api/v1/chat/message`.
* **`evidenceService.ts`:** Handles `multipart/form-data` uploads for `POST /api/v1/evidence/upload` with Axios `onUploadProgress`.
* **`ticketService.ts`:** Typed wrappers for `GET /api/v1/tickets`, `GET /api/v1/tickets/:id`, and `PATCH /api/v1/tickets/:id/status`.
* **`knowledgeService.ts`:** Typed wrappers for `POST /api/v1/knowledge/sync`, `POST /api/v1/knowledge/activate-candidate`, and `POST /api/v1/knowledge/rollback`.

---

### 3.8 TypeScript Type Definitions (Missing: `src/types/`)

* **`chat.ts`:** `ChatMessageRequest`, `ChatMessageResponse`, `CitationDTO`, `LanguageCode`.
* **`auth.ts`:** `AuthContext`, `UserRole`, `StaffUser`, `CustomerSession`.
* **`ticket.ts`:** `TicketDTO`, `TicketPriority`, `TicketStatus`, `EscalationAuditDTO`, `SLACalendarDTO`.
* **`evidence.ts`:** `EvidenceFileDTO`, `EvidenceStatus`, `ExtractedEntityDTO`.
* **`api.ts`:** `ApiResponse<T>`, `ApiErrorResponse`, `ProblemDetails`.

---

## 4. Architectural Gaps & Recommendations

1. **Immediate Pre-requisite for Frontend Development:**
   A clean initialization step is required to scaffold the missing build files (`vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`, `src/App.tsx`) and install dependencies via `npm install`.
2. **Design System Hygiene:**
   Establish `src/index.css` early with comprehensive design tokens so all subsequent UI components maintain cohesive typography, contrast ratios, and spacing.
3. **Backend API Readiness:**
   While `POST /api/v1/chat/message` is completely implemented and tested, downstream ticketing and evidence endpoints will be implemented during Days 10–13. The frontend service layer should define strict interfaces that match the approved `API_SPECIFICATION.md`.

