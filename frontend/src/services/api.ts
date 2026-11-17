/**
 * Centralized API Client & Service Adapters.
 *
 * Configures Axios instance for /api/v1 with:
 * 1. Strict single-identity authentication interceptors (never sends conflicting tokens).
 * 2. Distributed tracing correlation ID headers (X-Correlation-ID / X-Request-ID).
 * 3. Centralized error handling for HTTP 401, 403, and network errors.
 * 4. Modular service interfaces for Auth, Tickets, and Conversations.
 */

import axios, {
  type AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
  type AxiosResponse,
} from 'axios';

// =============================================================================
// Interfaces & Schemas
// =============================================================================

export interface AuthSession {
  persona: string;
  auth_type: 'SESSION_TOKEN' | 'JWT';
  token: string;
  header_name: string;
  header_value: string;
  user_id_or_email: string;
  customer_id?: string | null;
  roles: string[];
  customer_tier?: string | null;
  display_name: string;
  expires_in_seconds?: number;
}

export interface CurrentUser {
  auth_type: string;
  user_id_or_email?: string | null;
  customer_id?: string | null;
  session_id?: string | null;
  roles: string[];
}

export interface TicketListItem {
  id: string;
  ticket_number: string;
  conversation_id?: string | null;
  customer_id: string;
  assigned_agent_id?: string | null;
  assigned_team?: string | null;
  title: string;
  description: string;
  masked_summary: string;
  priority: string;
  priority_score: number;
  status: string;
  category: string;
  synthesis_status: string;
  routing_status: string;
  resolution_status: string;
  sla_target_minutes: number;
  sla_warning_at?: string | null;
  sla_breach_at: string;
  sla_breached: boolean;
  created_at: string;
  updated_at: string;
}

export interface TicketListResponse {
  items: TicketListItem[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface TicketDetailResponse extends TicketListItem {
  priority_explanation?: string | null;
  cluster_group_id?: string | null;
  sla_calendar_id: string;
  sla_warning_sent: boolean;
  sla_paused_at?: string | null;
  total_paused_duration_seconds: number;
  resolved_at?: string | null;
  closed_at?: string | null;
}

export interface ConversationListItem {
  id: string;
  customer_id?: string | null;
  session_id: string;
  primary_language: string;
  status: string;
  turn_count: number;
  overall_sentiment_score: number;
  is_escalated: boolean;
  started_at: string;
  last_message_at: string;
  closed_at?: string | null;
  custom_title?: string | null;
}

export interface ConversationListResponse {
  items: ConversationListItem[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface MessageItem {
  id: string;
  conversation_id: string;
  sender_type: string;
  sender_id?: string | null;
  content: string;
  detected_language: string;
  turn_sequence_number?: number | null;
  created_at: string;
}

export interface ConversationMessagesResponse {
  conversation_id: string;
  items: MessageItem[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface CitationDTO {
  citation_id: number;
  document_code: string;
  document_title: string;
  version_number: number;
  effective_date: string;
  chunk_excerpt: string;
}

export interface ClaimVerificationDTO {
  total_claims: number;
  verified_claims: number;
  all_claims_verified: boolean;
}

export interface ChatMessageData {
  message_id: string;
  reply_text: string;
  is_resolved: boolean;
  refusal_triggered: boolean;
  refusal_reason?: string | null;
  citations: CitationDTO[];
  confidence_score: number;
  effective_as_of_date_applied: string;
  suggested_ticket_category?: string | null;
  claim_verification?: ClaimVerificationDTO | null;
  model_used?: string | null;
  retrieval_latency_ms?: number | null;
  generation_latency_ms?: number | null;
  total_latency_ms?: number | null;
  detected_language: string;
  detected_language_confidence?: number | null;
  primary_language: string;
  context_restored?: boolean;
  clarification_needed?: boolean;
  technical_error?: boolean;
  technical_error_code?: string | null;
  turn_sequence_number?: number | null;
  conversation_id?: string | null;
  context_window_complete?: boolean;
  is_escalated: boolean;
  escalation_reason?: string | null;
}

export interface ChatMessageResponse {
  success: boolean;
  data: ChatMessageData;
  meta: {
    timestamp: string;
    request_id: string;
    caller_auth_type: string;
  };
}

export interface ChatMessageRequest {
  message: string;
  conversation_id?: string | null;
  effective_as_of_date?: string | null;
  product_line?: string | null;
  document_code?: string | null;
  preferred_language?: string | null;
  idempotency_key?: string | null;
}

export interface SynthesizeTicketRequest {
  ticket_id: string;
  force_resynthesis?: boolean;
}

export interface SynthesizeTicketResponse {
  ticket_id: string;
  synthesis_status: string;
  synthesis: Record<string, any>;
  completeness: Record<string, any>;
}

export interface SynthesisDetailResponse {
  ticket_id: string;
  synthesis_status: string;
  synthesis_id?: string | null;
  problem_statement?: string | null;
  product_or_service?: string | null;
  reference_identifiers: Array<Record<string, string>>;
  issue_category?: string | null;
  issue_subcategory?: string | null;
  resolution_sought?: string | null;
  actions_already_attempted: string[];
  missing_fields: string[];
  contradictions: Array<Record<string, string>>;
  completeness_score: number;
  extraction_confidence: number;
  extraction_provenance: Record<string, string>;
  llm_model_used?: string | null;
  synthesis_version: number;
}

export interface ClarificationListItem {
  id: string;
  round_number: number;
  status: string;
  questions: Array<Record<string, any>>;
  targeted_missing_fields: string[];
  completeness_score_before: number;
  completeness_score_after?: number | null;
  created_at: string;
  responded_at?: string | null;
}

export interface ClarificationListResponse {
  ticket_id: string;
  rounds: ClarificationListItem[];
}

export interface ClarificationRespondRequest {
  clarification_request_id: string;
  customer_response: string;
}

export interface ClarificationRespondResponse {
  ticket_id: string;
  clarification_request_id: string;
  round_number: number;
  response_absorbed: boolean;
  ambiguous_response: boolean;
  new_completeness: Record<string, any>;
  should_continue_clarification: boolean;
  synthesis_status: string;
}

export interface PriorityEvaluateResponse {
  ticket_id: string;
  priority: string;
  priority_score: number;
  priority_floor: string;
  reason: string;
  supporting_facts: string[];
}

export interface RouteTicketResponse {
  ticket_id: string;
  target_department: string;
  assigned_agent_id?: string | null;
  assigned_agent_name?: string | null;
  routing_status: string;
  reason: string;
  candidates_evaluated: number;
  candidate_details: Array<Record<string, any>>;
}

export interface AssignTicketRequest {
  agent_id: string;
  reason: string;
  force_override?: boolean;
}

export interface AssignTicketResponse {
  ticket_id: string;
  assigned_agent_id: string;
  assigned_agent_name: string;
  assigned_team: string;
  routing_status: string;
  active_tickets: number;
  max_active_tickets: number;
  reason: string;
  capacity_overridden: boolean;
}

export interface RoutingHistoryItem {
  event_id: string;
  event_type: string;
  event_payload: Record<string, any>;
  created_at?: string | null;
}

export interface RoutingHistoryResponse {
  ticket_id: string;
  events: RoutingHistoryItem[];
}

export interface TeamWorkloadItem {
  department: string;
  active_agents: number;
  total_capacity: number;
  active_tickets: number;
  utilization_pct: number;
}

export interface TeamWorkloadResponse {
  teams: TeamWorkloadItem[];
}

export interface ResolutionActionItem {
  action_id: string;
  action_type: string;
  action_key: string;
  action_params: Record<string, any>;
  status: string;
  is_sensitive: boolean;
  requires_approval: boolean;
  approved_by?: string | null;
  execution_attempts: number;
  execution_result: Record<string, any>;
}

export interface EvaluateResolutionResponse {
  ticket_id: string;
  resolution_id: string;
  status: string;
  playbook_category: string;
  recommended_resolution: string;
  confidence_score: number;
  eligibility_decision: string;
  requires_human_approval: boolean;
  supporting_evidence: string[];
  rejection_or_escalation_reason?: string | null;
  actions: ResolutionActionItem[];
}

export interface ProposeActionRequest {
  action_type: string;
  action_params?: Record<string, any>;
  is_sensitive?: boolean;
  requires_approval?: boolean;
}

export interface ExecuteActionResponse {
  action_id: string;
  status: string;
  execution_attempts: number;
  result: Record<string, any>;
}

export interface VerifyResolutionResponse {
  verified: boolean;
  resolution_status: string;
  ticket_status: string;
  failures?: string[] | null;
  escalated?: boolean | null;
  verified_at?: string | null;
  actions_executed?: number | null;
}

export interface ResolutionHistoryResponse {
  ticket_id: string;
  events: RoutingHistoryItem[];
}

// =============================================================================
// Escalation Interfaces
// =============================================================================

export interface EscalationEvaluateRequest {
  conversation_id: string;
  message_id?: string | null;
  message_content?: string | null;
}

export interface EscalationEvaluateResponse {
  should_escalate: boolean;
  trigger_condition?: string | null;
  reason?: string | null;
  is_after_hours: boolean;
  routing_destination: string;
  next_eligible_business_time?: string | null;
  audit_record_id?: string | null;
  already_escalated: boolean;
}

export interface AfterHoursStatusResponse {
  is_after_hours: boolean;
  reason: string;
  calendar_timezone: string;
  local_time: string;
  next_eligible_business_time: string;
}

export interface EscalationAuditItem {
  id: string;
  conversation_id?: string | null;
  ticket_id?: string | null;
  trigger_condition: string;
  previous_state: string;
  new_state: string;
  escalation_reason: string;
  escalation_payload: Record<string, any>;
  triggered_at: string;
}

export interface ScanUnresolvedResponse {
  scanned_at: string;
  escalated_count: number;
  audit_ids: string[];
}

// =============================================================================
// Evidence & Reconciliation Interfaces
// =============================================================================

export interface ReceiptLineItem {
  product_name: string;
  quantity?: number | null;
  unit_price?: number | null;
  total_price?: number | null;
  confidence: number;
}

export interface StructuredInvoiceData {
  order_id?: string | null;
  invoice_number?: string | null;
  invoice_date?: string | null;
  transaction_date?: string | null;
  total_amount?: number | null;
  currency?: string | null;
  tax_amount?: number | null;
  product_name?: string | null;
  product_quantity?: number | null;
  product_price?: number | null;
  merchant_name?: string | null;
  line_items: ReceiptLineItem[];
  overall_confidence: number;
  field_confidence: Record<string, number>;
  uncertain_fields: string[];
  missing_fields: string[];
  warnings: string[];
  page_count: number;
  is_scanned: boolean;
  provider_used: string;
  sha256_hash?: string | null;
  pii_redacted_text: string;
}

export interface EvidenceUploadResponse {
  evidence_id: string;
  conversation_id: string;
  customer_id?: string | null;
  file_name: string;
  mime_type: string;
  file_size_bytes: number;
  sha256_hash: string;
  status: string;
  is_malicious: boolean;
  quarantine_reason?: string | null;
  retention_expires_at: string;
  created_at: string;
  structured_entities?: StructuredInvoiceData | null;
}

export interface EvidenceDetailResponse {
  evidence_id: string;
  conversation_id?: string | null;
  customer_id?: string | null;
  message_id?: string | null;
  ticket_id?: string | null;
  file_name: string;
  mime_type: string;
  file_size_bytes: number;
  sha256_hash: string;
  status: string;
  is_malicious: boolean;
  quarantine_reason?: string | null;
  processing_error?: string | null;
  retention_expires_at: string;
  created_at: string;
  processed_at?: string | null;
}

export interface EvidenceExtractionResponse {
  evidence_id: string;
  status: string;
  extracted_at: string;
  ocr_confidence: number;
  pii_redacted_text: string;
  entities: StructuredInvoiceData;
}

export interface ReconciliationRequest {
  conversation_id: string;
  evidence_id: string;
  secondary_evidence_id?: string | null;
  claim_text: string;
  message_id?: string | null;
  idempotency_key?: string | null;
  async_job?: boolean;
}

export interface ReconciliationResponse {
  verification_id: string;
  evidence_id: string;
  secondary_evidence_id?: string | null;
  conversation_id: string;
  message_id?: string | null;
  job_id?: string | null;
  job_status: string;
  reconciliation_status: string;
  overall_match_status: string;
  overall_confidence: number;
  summary: string;
  clarification_status: string;
  clarification_prompt?: string | null;
  field_comparisons: Array<Record<string, any>>;
  extracted_claims: Array<Record<string, any>>;
  security_flags: string[];
  is_quarantined: boolean;
  verification_level: 'EXTRACTED' | 'DOCUMENT_CORROBORATED' | 'PAYMENT_INDEPENDENTLY_VERIFIED';
  is_refund_authorized: boolean;
  requires_authoritative_verification: boolean;
  created_at?: string | null;
  verified_at?: string | null;
}

export interface PurgeResult {
  status: string;
  purged_count?: number;
  message?: string;
  reset_count?: number;
}

// =============================================================================
// Analytics & Operational Intelligence Interfaces
// =============================================================================

export interface AnalyticsFilterParams {
  time_window?: 'today' | '7d' | '30d' | '90d' | 'custom';
  start_date?: string;
  end_date?: string;
  category?: string;
  priority?: string;
  department?: string;
}

export interface ReportingPeriodMetadata {
  time_window: string;
  start_date: string;
  end_date: string;
  timezone: string;
}

export interface DashboardIncidentItem {
  id: string;
  ticket_id: string;
  ticket_number: string;
  title: string;
  customer: string;
  tier: string;
  category: string;
  priority: string;
  severity: string;
  status: string;
  assigned_team: string;
  priority_score: number;
  sla_elapsed: string;
  sla_total: string;
  sla_remaining: string;
  progress: number;
  sentiment_score: number;
  sentiment_label: string;
  latest_activity: string;
  evidence_status: string;
  evidence_snippet: string;
  evidence_doc: string;
  suggested_action: string;
  is_breached?: boolean;
  is_overdue?: boolean;
  overdue_duration?: string;
  sla_target_minutes?: number;
  sla_deadline?: string;
}

export interface DepartmentWorkloadItem {
  department: string;
  active_agents: number;
  total_capacity: number;
  active_tickets: number;
  utilization_pct: number;
}

export interface OverviewDashboardResponse {
  total_tickets: number;
  open_tickets: number;
  resolved_tickets: number;
  escalated_tickets: number;
  urgent_tickets: number;
  sla_compliance_pct: number;
  sla_breached_count: number;
  sla_warning_count: number;
  avg_resolution_time_minutes: number;
  active_conversations_count: number;
  priority_distribution: Record<string, number>;
  status_distribution: Record<string, number>;
  priority_queue: DashboardIncidentItem[];
  department_workload: DepartmentWorkloadItem[];
  reporting_period: ReportingPeriodMetadata;
}

export interface DailyTrendItem {
  date: string;
  created: number;
  resolved: number;
  escalated: number;
}

export interface CategoryDistributionItem {
  category: string;
  count: number;
  percentage: number;
}

export interface PriorityDistributionItem {
  priority: string;
  count: number;
  percentage: number;
}

export interface SlaBreakdownItem {
  label: string;
  total: number;
  compliant: number;
  breached: number;
  compliance_pct: number;
}

export interface LanguageDistributionItem {
  language: string;
  code: string;
  query_count: number;
  percentage: number;
}

export interface EvidenceAnalyticsItem {
  total_uploaded: number;
  verified_count: number;
  conflict_count: number;
  unsafe_count: number;
  status_distribution: Record<string, number>;
  reconciliation_distribution: Record<string, number>;
}

export interface PerformanceAnalyticsResponse {
  ticket_trends: DailyTrendItem[];
  category_distribution: CategoryDistributionItem[];
  priority_distribution: PriorityDistributionItem[];
  sla_by_category: SlaBreakdownItem[];
  sla_by_priority: SlaBreakdownItem[];
  overall_sla_compliance_pct: number;
  total_breaches: number;
  avg_resolution_time_minutes: number;
  operational_workload: DepartmentWorkloadItem[];
  evidence_analytics: EvidenceAnalyticsItem;
  language_distribution: LanguageDistributionItem[];
  reporting_period: ReportingPeriodMetadata;
}

export interface OperationalInsightItem {
  id: string;
  rule_name: string;
  severity: 'CRITICAL' | 'WARNING' | 'INFO';
  title: string;
  description: string;
  supporting_metric: string;
  time_window: string;
  target_route: string;
  suggested_action: string;
}

export interface OperationalIntelligenceResponse {
  insights: OperationalInsightItem[];
  evaluated_at: string;
  rules_applied: string[];
}

// =============================================================================
// Storage & Session Lifecycle
// =============================================================================

export const AUTH_STORAGE_KEY = 'ai_support_dev_auth';

let currentAuthSession: AuthSession | null = null;
let onUnauthorizedHandler: (() => void) | null = null;

export const setOnUnauthorized = (handler: (() => void) | null) => {
  onUnauthorizedHandler = handler;
};

export const getApiAuthSession = (): AuthSession | null => {
  if (currentAuthSession) return currentAuthSession;
  try {
    if (typeof localStorage !== 'undefined') {
      const raw = localStorage.getItem(AUTH_STORAGE_KEY);
      if (raw) {
        currentAuthSession = JSON.parse(raw);
        return currentAuthSession;
      }
    }
  } catch {
    // Storage read fallback
  }
  return null;
};

export const setApiAuthSession = (session: AuthSession | null) => {
  currentAuthSession = session;
  try {
    if (typeof localStorage !== 'undefined') {
      if (session) {
        localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
      } else {
        localStorage.removeItem(AUTH_STORAGE_KEY);
      }
    }
  } catch {
    // Storage write fallback
  }
};

export const clearApiAuthSession = () => {
  setApiAuthSession(null);
};

// =============================================================================
// Distributed Tracing Correlation ID
// =============================================================================

export const generateCorrelationId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `corr-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
};

// =============================================================================
// Axios Client Instance
// =============================================================================

export const api: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request Interceptor: Invariant enforcement
// Strict Invariant: NEVER send both X-Session-Token and Authorization in the same request.
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // 1. Inject Correlation ID if missing
    if (!config.headers.get('X-Correlation-ID') && !config.headers.get('X-Request-ID')) {
      const corrId = generateCorrelationId();
      config.headers.set('X-Correlation-ID', corrId);
      config.headers.set('X-Request-ID', corrId);
    }

    // 2. Authoritative identity header injection
    const session = getApiAuthSession();
    if (session && session.token) {
      if (session.auth_type === 'SESSION_TOKEN') {
        config.headers.set('X-Session-Token', session.token);
        config.headers.delete('Authorization');
      } else if (session.auth_type === 'JWT') {
        config.headers.set('Authorization', `Bearer ${session.token}`);
        config.headers.delete('X-Session-Token');
      }
    } else {
      config.headers.delete('X-Session-Token');
      config.headers.delete('Authorization');
    }

    return config;
  },
  (error: AxiosError) => Promise.reject(error)
);

// Response Interceptor: 401/403 and network error handling
api.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError) => {
    if (error.response) {
      const status = error.response.status;
      if (status === 401) {
        // Clear invalid / expired session immediately
        clearApiAuthSession();
        if (onUnauthorizedHandler) {
          onUnauthorizedHandler();
        }
      }
    }
    return Promise.reject(error);
  }
);

// =============================================================================
// Domain Service Adapters
// =============================================================================

export const authApi = {
  issueDevToken: async (persona: string): Promise<AuthSession> => {
    const res = await api.post<AuthSession>('/auth/dev-token', { persona });
    return res.data;
  },

  getCurrentUser: async (): Promise<CurrentUser> => {
    const res = await api.get<CurrentUser>('/auth/me');
    return res.data;
  },
};

export const ticketsApi = {
  listTickets: async (params?: {
    status?: string;
    priority?: string;
    category?: string;
    customer_id?: string;
    limit?: number;
    offset?: number;
  }): Promise<TicketListResponse> => {
    const res = await api.get<TicketListResponse>('/tickets', { params });
    return res.data;
  },

  getTicket: async (id: string): Promise<TicketDetailResponse> => {
    const res = await api.get<TicketDetailResponse>(`/tickets/${id}`);
    return res.data;
  },

  synthesizeTicket: async (
    ticketId: string,
    forceResynthesis: boolean = false
  ): Promise<SynthesizeTicketResponse> => {
    const res = await api.post<SynthesizeTicketResponse>('/tickets/synthesize', {
      ticket_id: ticketId,
      force_resynthesis: forceResynthesis,
    });
    return res.data;
  },

  getSynthesis: async (ticketId: string): Promise<SynthesisDetailResponse> => {
    const res = await api.get<SynthesisDetailResponse>(`/tickets/${ticketId}/synthesis`);
    return res.data;
  },

  getClarifications: async (ticketId: string): Promise<ClarificationListResponse> => {
    const res = await api.get<ClarificationListResponse>(
      `/tickets/${ticketId}/synthesis/clarifications`
    );
    return res.data;
  },

  generateClarification: async (ticketId: string): Promise<any> => {
    const res = await api.post(`/tickets/${ticketId}/synthesis/clarify`);
    return res.data;
  },

  respondToClarification: async (
    ticketId: string,
    clarificationRequestId: string,
    customerResponse: string
  ): Promise<ClarificationRespondResponse> => {
    const res = await api.post<ClarificationRespondResponse>(
      `/tickets/${ticketId}/synthesis/respond`,
      {
        clarification_request_id: clarificationRequestId,
        customer_response: customerResponse,
      }
    );
    return res.data;
  },

  markSynthesisComplete: async (
    ticketId: string
  ): Promise<{ status: string; ticket_id: string; synthesis_status: string }> => {
    const res = await api.post<{ status: string; ticket_id: string; synthesis_status: string }>(
      `/tickets/${ticketId}/synthesis/complete`
    );
    return res.data;
  },

  evaluatePriority: async (ticketId: string): Promise<PriorityEvaluateResponse> => {
    const res = await api.post<PriorityEvaluateResponse>(`/tickets/${ticketId}/priority/evaluate`);
    return res.data;
  },

  routeTicket: async (ticketId: string): Promise<RouteTicketResponse> => {
    const res = await api.post<RouteTicketResponse>(`/tickets/${ticketId}/route`);
    return res.data;
  },

  assignTicket: async (
    ticketId: string,
    payload: AssignTicketRequest
  ): Promise<AssignTicketResponse> => {
    const res = await api.post<AssignTicketResponse>(`/tickets/${ticketId}/assign`, payload);
    return res.data;
  },

  getRoutingHistory: async (ticketId: string): Promise<RoutingHistoryResponse> => {
    const res = await api.get<RoutingHistoryResponse>(`/tickets/${ticketId}/routing-history`);
    return res.data;
  },

  getTeamsWorkload: async (): Promise<TeamWorkloadResponse> => {
    const res = await api.get<TeamWorkloadResponse>('/tickets/teams/workload');
    return res.data;
  },

  evaluateResolution: async (ticketId: string): Promise<EvaluateResolutionResponse> => {
    const res = await api.post<EvaluateResolutionResponse>(`/tickets/${ticketId}/resolution/evaluate`);
    return res.data;
  },

  getResolution: async (ticketId: string): Promise<EvaluateResolutionResponse> => {
    const res = await api.get<EvaluateResolutionResponse>(`/tickets/${ticketId}/resolution`);
    return res.data;
  },

  proposeAction: async (
    ticketId: string,
    payload: ProposeActionRequest
  ): Promise<ResolutionActionItem> => {
    const res = await api.post<ResolutionActionItem>(`/tickets/${ticketId}/resolution/actions`, payload);
    return res.data;
  },

  approveAction: async (
    ticketId: string,
    actionId: string,
    reason: string
  ): Promise<ResolutionActionItem> => {
    const res = await api.post<ResolutionActionItem>(
      `/tickets/${ticketId}/resolution/actions/${actionId}/approve`,
      { reason }
    );
    return res.data;
  },

  rejectAction: async (
    ticketId: string,
    actionId: string,
    reason: string
  ): Promise<ResolutionActionItem> => {
    const res = await api.post<ResolutionActionItem>(
      `/tickets/${ticketId}/resolution/actions/${actionId}/reject`,
      { reason }
    );
    return res.data;
  },

  updateActionParams: async (
    ticketId: string,
    actionId: string,
    actionParams: Record<string, any>
  ): Promise<ResolutionActionItem> => {
    const res = await api.patch<ResolutionActionItem>(
      `/tickets/${ticketId}/resolution/actions/${actionId}`,
      { action_params: actionParams }
    );
    return res.data;
  },

  executeAction: async (
    ticketId: string,
    actionId: string
  ): Promise<ExecuteActionResponse> => {
    const res = await api.post<ExecuteActionResponse>(
      `/tickets/${ticketId}/resolution/actions/${actionId}/execute`
    );
    return res.data;
  },

  verifyResolution: async (ticketId: string): Promise<VerifyResolutionResponse> => {
    const res = await api.post<VerifyResolutionResponse>(`/tickets/${ticketId}/resolution/verify`);
    return res.data;
  },

  getResolutionHistory: async (ticketId: string): Promise<ResolutionHistoryResponse> => {
    const res = await api.get<ResolutionHistoryResponse>(`/tickets/${ticketId}/resolution/history`);
    return res.data;
  },
};

export const chatApi = {
  sendMessage: async (
    payload: ChatMessageRequest,
    signal?: AbortSignal
  ): Promise<ChatMessageResponse> => {
    const res = await api.post<ChatMessageResponse>('/chat/message', payload, { signal });
    return res.data;
  },
};

export const conversationsApi = {
  listConversations: async (params?: {
    status?: string;
    customer_id?: string;
    limit?: number;
    offset?: number;
  }): Promise<ConversationListResponse> => {
    const res = await api.get<ConversationListResponse>('/conversations', { params });
    return res.data;
  },

  getConversation: async (id: string): Promise<ConversationListItem> => {
    const res = await api.get<ConversationListItem>(`/conversations/${id}`);
    return res.data;
  },

  getConversationMessages: async (
    id: string,
    params?: { limit?: number; offset?: number }
  ): Promise<ConversationMessagesResponse> => {
    const res = await api.get<ConversationMessagesResponse>(`/conversations/${id}/messages`, {
      params,
    });
    return res.data;
  },

  renameConversation: async (
    id: string,
    customTitle: string
  ): Promise<ConversationListItem> => {
    const res = await api.patch<ConversationListItem>(`/conversations/${id}`, {
      custom_title: customTitle,
    });
    return res.data;
  },

  deleteConversation: async (
    id: string
  ): Promise<{ success: boolean; message: string }> => {
    const res = await api.delete<{ success: boolean; message: string }>(`/conversations/${id}`);
    return res.data;
  },
};

export const escalationApi = {
  getAfterHoursStatus: async (checkTime?: string): Promise<AfterHoursStatusResponse> => {
    const res = await api.get<AfterHoursStatusResponse>('/escalation/after-hours/status', {
      params: checkTime ? { check_time: checkTime } : undefined,
    });
    return res.data;
  },

  listAudits: async (params?: {
    conversation_id?: string;
    ticket_id?: string;
    trigger_condition?: string;
    limit?: number;
  }): Promise<EscalationAuditItem[]> => {
    const res = await api.get<EscalationAuditItem[]>('/escalation/audits', { params });
    return res.data;
  },

  evaluateEscalation: async (
    payload: EscalationEvaluateRequest
  ): Promise<EscalationEvaluateResponse> => {
    const res = await api.post<EscalationEvaluateResponse>('/escalation/evaluate', payload);
    return res.data;
  },

  scanUnresolved: async (batchSize: number = 50): Promise<ScanUnresolvedResponse> => {
    const res = await api.post<ScanUnresolvedResponse>('/escalation/scan-unresolved', null, {
      params: { batch_size: batchSize },
    });
    return res.data;
  },
};

export const evidenceApi = {
  uploadEvidence: async (formData: FormData): Promise<EvidenceUploadResponse> => {
    const res = await api.post<EvidenceUploadResponse>('/evidence/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return res.data;
  },

  getEvidence: async (evidenceId: string): Promise<EvidenceDetailResponse> => {
    const res = await api.get<EvidenceDetailResponse>(`/evidence/${evidenceId}`);
    return res.data;
  },

  getEvidenceStatus: async (evidenceId: string): Promise<any> => {
    const res = await api.get(`/evidence/${evidenceId}/status`);
    return res.data;
  },

  getEvidenceExtraction: async (evidenceId: string): Promise<EvidenceExtractionResponse> => {
    const res = await api.get<EvidenceExtractionResponse>(`/evidence/${evidenceId}/extraction`);
    return res.data;
  },

  purgeExpired: async (batchSize: number = 50, includeFailed: boolean = false): Promise<PurgeResult> => {
    const res = await api.post<PurgeResult>('/evidence/purge-expired', null, {
      params: { batch_size: batchSize, include_failed: includeFailed },
    });
    return res.data;
  },

  resetFailedPurges: async (evidenceId?: string): Promise<PurgeResult> => {
    const res = await api.post<PurgeResult>('/evidence/reset-failed-purges', null, {
      params: evidenceId ? { evidence_id: evidenceId } : undefined,
    });
    return res.data;
  },
};

export const reconciliationApi = {
  reconcile: async (payload: ReconciliationRequest): Promise<ReconciliationResponse> => {
    const res = await api.post<ReconciliationResponse>('/evidence/reconcile', payload);
    return res.data;
  },

  getVerification: async (verificationId: string): Promise<ReconciliationResponse> => {
    const res = await api.get<ReconciliationResponse>(`/evidence/reconcile/${verificationId}`);
    return res.data;
  },
};

export const analyticsApi = {
  getOverview: async (params?: AnalyticsFilterParams): Promise<OverviewDashboardResponse> => {
    const res = await api.get<OverviewDashboardResponse>('/analytics/overview', { params });
    return res.data;
  },

  getPerformance: async (params?: AnalyticsFilterParams): Promise<PerformanceAnalyticsResponse> => {
    const res = await api.get<PerformanceAnalyticsResponse>('/analytics/performance', { params });
    return res.data;
  },

  getOperationalIntelligence: async (
    params?: AnalyticsFilterParams
  ): Promise<OperationalIntelligenceResponse> => {
    const res = await api.get<OperationalIntelligenceResponse>('/analytics/operational-intelligence', {
      params,
    });
    return res.data;
  },
};
