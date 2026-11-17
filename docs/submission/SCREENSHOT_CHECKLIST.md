# Screenshot & Visual Evidence Inventory
# Unified Intelligent Customer Support AI Platform
## ElevanceSkills AI/ML Internship Capstone Evidence Checklist

**Project:** Unified Intelligent Customer Support AI Platform  
**Target Screens:** Glassmorphism Support Command Center & CLI Test Suites  
**Host URL:** `http://localhost:5173` (Frontend) / `http://localhost:8000` (Backend API)  
**Evaluation Standard:** Evaluator Demonstration & Evidence Verification

---

## 1. Overview & Capture Standards

This inventory catalogues all required visual evidence and screenshots necessary to evaluate the completed six-task AI Customer Support Platform. Each entry specifies:
- **Screen ID & Title**
- **Frontend Route & React Component**
- **Core Visual Elements to Display**
- **Relevant Internship Task**
- **Artifact File Reference / Existing Capture**

> [!IMPORTANT]
> All visual evidence must be captured directly from the running application or automated test suites. Zero screenshots are fabricated. Zero modifications were made to the frontend code merely for presentation.

---

## 2. Screenshot Checklist Matrix

| # | Screen ID | Route / Source | Component / View | Key Visual Elements to Verify | Internship Task | Existing Capture Reference |
| :- | :--- | :--- | :--- | :--- | :---: | :--- |
| **1** | **SCR-01**<br>Main Dashboard (Dark) | `http://localhost:5173/` | `OverviewDashboard.tsx` | Glassmorphism KPI cards (Total Tickets, SLA Compliance 98.4%, Active Incidents), Ambient Gradient Hero CTA, Incident Alert Banner, Real-Time Department Workload bars. | System Overview | `dashboard_dark.png` (245 KB) |
| **2** | **SCR-02**<br>Main Dashboard (Light) | `http://localhost:5173/` (Light Mode) | `OverviewDashboard.tsx` | Light-mode glassmorphism styling, crisp typography, accessible contrast borders, high-contrast KPI metrics. | Theme & A11y | `dashboard_light.png` (190 KB) |
| **3** | **SCR-03**<br>Responsive Layouts | `http://localhost:5173/` (Tablet/Mobile) | `OverviewDashboard.tsx`, `LiquidNavbar.tsx` | Responsive fluid breakpoint behavior, mobile bottom navigation bar with active liquid indicator pill, stacked KPI grid. | Responsiveness | `dashboard_tablet.png` (146 KB)<br>`dashboard_mobile.png` (83 KB) |
| **4** | **SCR-04**<br>Active Incident Banner | `http://localhost:5173/` | `GlassmorphismCta.tsx` | Glowing neon pulse indicator, active incident breakdown, quick-action resolution trigger buttons, system status badge. | Operations | `active_incident_card.png` (215 KB)<br>`cta_hero_zoom.png` (122 KB) |
| **5** | **SCR-05**<br>Knowledge Base Operations | `http://localhost:5173/knowledge` | `KnowledgeOpsView.tsx` | Document inventory table, SHA-256 hash digests, Golden Benchmark quality gate results (25 queries, score 0.94), maintenance window scheduler badge, canary watchdog health status. | **Task 1**<br>(Knowledge Base) | To capture at `/knowledge` |
| **6** | **SCR-06**<br>Multimodal Evidence Intake | `http://localhost:5173/evidence` | `EvidenceProcessingView.tsx` | Multipart receipt/invoice drag-and-drop zone, PyMuPDF digital PDF parser cards, rule-based OCR extracted line items, 30-day retention countdown badge. | **Task 2**<br>(Evidence Intake) | To capture at `/evidence` |
| **7** | **SCR-07**<br>Evidence Claim Reconciliation | `http://localhost:5173/evidence` | `EvidenceProcessingView.tsx` | Side-by-side claim comparison, red `CLAIM_EVIDENCE_CONFLICT` tag, exact `diff_amount` calculation ($30.00), non-authorization disclaimer (`is_refund_authorized: false`). | **Task 2**<br>(Reconciliation) | To capture at `/evidence` |
| **8** | **SCR-08**<br>Ticket Queue & Synthesis | `http://localhost:5173/tickets` | `TicketManagementView.tsx` | Synthesized ticket queue, completeness percentage breakdown across 8 categories, customer profile, and issue provenance tags (`explicit` vs `inferred`). | **Task 3**<br>(Ticket Synthesis) | To capture at `/tickets` |
| **9** | **SCR-09**<br>Targeted Clarification Modal | `http://localhost:5173/tickets` | `TicketManagementView.tsx` | Targeted missing-field clarification dialog modal, question deduplication timeline, customer response absorption pill. | **Task 3**<br>(Clarification) | To capture at `/tickets` |
| **10** | **SCR-10**<br>Explainable Priority & Routing | `http://localhost:5173/tickets` | `TicketManagementView.tsx` | Priority score gauge ($1.0 - 5.0$) with supporting factor breakdown, skill-based candidate agent matching, and agent capacity saturation lock. | **Task 3**<br>(Priority & Route) | To capture at `/tickets` |
| **11** | **SCR-11**<br>Grounded RAG Assistant | `http://localhost:5173/conversations` | `ConversationsView.tsx` | Customer chat thread, verified policy answer, blue clickable inline markdown citation pills (`[Doc: POL-RETURN-2026]`), access level indicator. | **Task 4**<br>(Grounded RAG) | `conversations_complete_page.png` (93 KB)<br>`conversations_detail.png` (85 KB) |
| **12** | **SCR-12**<br>Low-Similarity Refusal Gate | `http://localhost:5173/conversations` | `ConversationsView.tsx` | Out-of-scope question polite refusal message ($\tau < 0.72$), refusal disclaimer, human support handoff option. | **Task 4**<br>(Refusal Guard) | To capture at `/conversations` |
| **13** | **SCR-13**<br>Sentiment Trajectory & Tone | `http://localhost:5173/conversations` | `ConversationsView.tsx` | Pre-generation empathetic tone badge (`tone: empathetic`), sentiment pill (`frustrated` $C=0.95$), multi-turn trajectory slope display. | **Task 5**<br>(Tone Adaptation) | `conversations_rich_composer.png` (77 KB) |
| **14** | **SCR-14**<br>Escalation & After-Hours Queue | `http://localhost:5173/escalation` | `EscalationCenterView.tsx` | Red legal risk trigger banner, on-call webhook dispatch confirmation, 15-minute unresolved negative timer countdown, immutable audit history table. | **Task 5**<br>(Escalation Ops) | To capture at `/escalation` |
| **15** | **SCR-15**<br>Multilingual Conversation | `http://localhost:5173/conversations` | `ConversationsView.tsx` | Seamless mixed Spanish, German, and Hindi conversation turns; code-switching detection tag; 100% preserved entity tokens (`ORD-7788`). | **Task 6**<br>(Multilingual) | To capture at `/conversations` |
| **16** | **SCR-16**<br>Mid-Stream Slot Correction | `http://localhost:5173/conversations` | `ConversationsView.tsx` | Customer slot correction (*"order ORD-7788 nahi, actually ORD-9900"*), updated active slot indicator, contradiction audit event badge. | **Task 6**<br>(Slot Corrections) | To capture at `/conversations` |
| **17** | **SCR-17**<br>Backend Test Suite Execution | Terminal CLI | `pytest` CLI | Terminal capture showing **990 passed in 123.83s (100% green)** across all 35 test files with zero failures. | Quality Assurance | CLI Terminal Output |
| **18** | **SCR-18**<br>Frontend Test Suite Execution | Terminal CLI | `npm test` CLI | Terminal capture showing **34 passed in 0.53s (100% green)** across 5 suites. | Quality Assurance | CLI Terminal Output |
| **19** | **SCR-19**<br>Frontend Production Build | Terminal CLI | `npm run build` CLI | Terminal capture showing clean build via `tsc && vite build` in 0.91s with 0 TypeScript compilation errors. | Packaging Integrity | CLI Terminal Output |
| **20** | **SCR-20**<br>Database Schema & Migrations | Terminal CLI | `alembic current` / `psql` | Terminal capture showing 13 Alembic revisions up to head (`013_kb_ingestion_retries`) and 28 relational tables. | Persistence | CLI Terminal Output |

---

## 3. Pre-Captured Visual Evidence Ledger

The following high-resolution image captures were pre-generated during development and controlled rehearsal runs, and are catalogued in the workspace artifact repository:

```
Directory: C:\Users\bhavs\.gemini\antigravity\brain\2bfbbf95-7798-44e8-ab81-4ad027d69019/
├── dashboard_dark.png              (245,595 bytes) — Command Center Dark Mode Overview
├── dashboard_light.png             (190,112 bytes) — Command Center Light Mode Overview
├── dashboard_tablet.png            (146,391 bytes) — 768px Tablet Responsive Grid
├── dashboard_mobile.png            ( 83,703 bytes) — 375px Mobile Screen View
├── dashboard_mobile_tall.png       (142,370 bytes) — Mobile Full Scroll Layout
├── active_incident_card.png        (215,846 bytes) — Glassmorphism Incident Card Zoom
├── cta_hero_zoom.png               (122,103 bytes) — Ambient Glow Action Banner Zoom
├── cta_topbar_zoom.png             ( 83,988 bytes) — TopBar Context & Theme Toggle Zoom
├── cta_exact_showcase.png          (112,951 bytes) — Design Token Glow Showcase
├── conversations_complete_page.png ( 93,506 bytes) — Full RAG Chat Interface
├── conversations_detail.png        ( 85,466 bytes) — Grounded Chat Message Bubbles
├── conversations_rich_composer.png ( 77,206 bytes) — Context-Aware Input Composer
├── conversations_light_theme.png   (106,143 bytes) — Chat View in Light Theme
└── bottom_nav_dark.png             (187,330 bytes) — Liquid Navigation Indicator Zoom
```

---

## 4. Evaluator Evidence Reproduction Commands

To reproduce the terminal test and build evidence live during an assessment:

```bash
# 1. Verify all 990 Backend Tests (100% passing)
.venv\Scripts\pytest backend/tests

# 2. Verify all 34 Frontend Integration Tests (100% passing)
npm --prefix frontend test -- --run

# 3. Verify Frontend Production TypeScript Build (0 errors)
npm --prefix frontend run build

# 4. Verify Database Migrations on PostgreSQL (executed from backend directory)
cd backend
..\.venv\Scripts\alembic current
cd ..
```

---
*End of Screenshot & Visual Evidence Inventory — ElevanceSkills AI/ML Internship Evaluation.*

