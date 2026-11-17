import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Card } from '../components/common/Card';
import { Badge, type BadgeVariant } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import {
  AlertTriangleIcon,
  ShieldCheckIcon,
  ClockIcon,
  CheckCircleIcon,
  SearchIcon,
  SparklesIcon,
} from '../components/common/Icons';
import {
  analyticsApi,
  escalationApi,
  ticketsApi,
  type AfterHoursStatusResponse,
  type EscalationAuditItem,
  type OverviewDashboardResponse,
  type DashboardIncidentItem,
  type TicketDetailResponse,
  type RoutingHistoryItem,
} from '../services/api';
import { useAuth } from '../context/AuthContext';

// Unified Escalation Record
export interface EscalationRecord {
  id: string;
  ticketId: string;
  ticketUuid: string;
  title: string;
  customer: string;
  tier: string;
  category: string;
  priority: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
  priorityScore: number;
  severity: string;
  status: string;
  assignedTeam: string;
  slaElapsed: string;
  slaTotal: string;
  slaRemaining: string;
  progress: number;
  triggerCondition: string;
  escalationReason: string;
  triggeredAt: string;
  evidenceStatus: string;
  evidenceSnippet: string;
  evidenceDoc: string;
  suggestedAction: string;
}

export const EscalationCenterView: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { activeSession, currentUser } = useAuth();

  const isStaff =
    activeSession?.auth_type === 'JWT' ||
    (currentUser?.roles || activeSession?.roles || []).some((r) =>
      ['AGENT', 'SUPERVISOR', 'ADMIN', 'GLOBAL_ADMIN', 'SUPER_ADMIN'].includes(r.toUpperCase())
    );

  // Active View Tab
  const [activeViewTab, setActiveViewTab] = useState<'registry' | 'audits' | 'afterHours'>('registry');

  // Overview / Database-backed telemetry
  const [overviewData, setOverviewData] = useState<OverviewDashboardResponse | null>(null);
  const [isLoadingOverview, setIsLoadingOverview] = useState<boolean>(true);
  const [overviewError, setOverviewError] = useState<string | null>(null);

  // Audits Stream State
  const [audits, setAudits] = useState<EscalationAuditItem[]>([]);
  const [isLoadingAudits, setIsLoadingAudits] = useState<boolean>(true);
  const [auditsError, setAuditsError] = useState<string | null>(null);

  // After-hours Status State
  const [afterHours, setAfterHours] = useState<AfterHoursStatusResponse | null>(null);
  const [isLoadingAfterHours, setIsLoadingAfterHours] = useState<boolean>(true);

  // Filters State
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [filterPriority, setFilterPriority] = useState<string>('ALL');
  const [filterStatus, setFilterStatus] = useState<string>('ALL');
  const [filterTeam, setFilterTeam] = useState<string>('ALL');
  const [filterSlaRisk, setFilterSlaRisk] = useState<string>('ALL');
  const [sortBy, setSortBy] = useState<'priority_desc' | 'priority_asc' | 'sla_urgent' | 'ticket_id'>('priority_desc');
  const [auditTriggerFilter, setAuditTriggerFilter] = useState<string>('ALL');

  // Pagination State
  const [currentPage, setCurrentPage] = useState<number>(1);
  const pageSize = 10;

  // Manual Scan Unresolved State (Staff Only)
  const [isScanning, setIsScanning] = useState<boolean>(false);
  const [actionFeedback, setActionFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null);

  // Manual Evaluate Turn State
  const [showEvalModal, setShowEvalModal] = useState<boolean>(false);
  const [evalConvId, setEvalConvId] = useState<string>('');
  const [evalContent, setEvalContent] = useState<string>('');
  const [isEvaluating, setIsEvaluating] = useState<boolean>(false);

  // Selected Ticket Inspection State
  const [selectedTicket, setSelectedTicket] = useState<EscalationRecord | null>(null);
  const [ticketDetail, setTicketDetail] = useState<TicketDetailResponse | null>(null);
  const [routingHistory, setRoutingHistory] = useState<RoutingHistoryItem[]>([]);
  const [isLoadingDetail, setIsLoadingDetail] = useState<boolean>(false);
  const [detailModalTab, setDetailModalTab] = useState<'overview' | 'routing' | 'evidence'>('overview');

  // Selected Audit for Detailed Inspection (Audits Tab)
  const [selectedAudit, setSelectedAudit] = useState<EscalationAuditItem | null>(null);

  // 1. Fetch Overview (authoritative telemetry counts & priority queue)
  const fetchOverview = useCallback(async () => {
    setIsLoadingOverview(true);
    setOverviewError(null);
    try {
      const data = await analyticsApi.getOverview({ time_window: '30d' });
      setOverviewData(data);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        err?.message ||
        'Failed to load escalation telemetry.';
      setOverviewError(msg);
    } finally {
      setIsLoadingOverview(false);
    }
  }, []);

  // 2. Fetch Audits
  const fetchAudits = useCallback(async () => {
    setIsLoadingAudits(true);
    setAuditsError(null);
    try {
      const params: any = { limit: 100 };
      if (auditTriggerFilter !== 'ALL') {
        params.trigger_condition = auditTriggerFilter;
      }
      const records = await escalationApi.listAudits(params);
      setAudits(records);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to load escalation audit records.';
      setAuditsError(msg);
      setAudits([]);
    } finally {
      setIsLoadingAudits(false);
    }
  }, [auditTriggerFilter]);

  // 3. Fetch After-Hours
  const fetchAfterHours = useCallback(async () => {
    setIsLoadingAfterHours(true);
    try {
      const res = await escalationApi.getAfterHoursStatus();
      setAfterHours(res);
    } catch {
      setAfterHours(null);
    } finally {
      setIsLoadingAfterHours(false);
    }
  }, []);

  useEffect(() => {
    fetchOverview();
    fetchAudits();
    fetchAfterHours();
  }, [fetchOverview, fetchAudits, fetchAfterHours, activeSession?.token]);

  // Fetch ticket details when a ticket is inspected
  const handleInspectTicket = useCallback(async (record: EscalationRecord) => {
    setSelectedTicket(record);
    setIsLoadingDetail(true);
    setTicketDetail(null);
    setRoutingHistory([]);
    setDetailModalTab('overview');

    try {
      // Load real ticket detail
      const detail = await ticketsApi.getTicket(record.ticketUuid);
      setTicketDetail(detail);

      // Load routing history
      try {
        const rh = await ticketsApi.getRoutingHistory(record.ticketUuid);
        setRoutingHistory(rh.events || []);
      } catch {
        setRoutingHistory([]);
      }
    } catch {
      // If getTicket returns error, fallback to record telemetry
      setTicketDetail(null);
    } finally {
      setIsLoadingDetail(false);
    }
  }, []);

  // Check URL params on initial mount
  useEffect(() => {
    const inspectParam = searchParams.get('inspect') || searchParams.get('id');
    if (inspectParam && overviewData?.priority_queue) {
      const match = overviewData.priority_queue.find(
        (pq) => pq.ticket_number === inspectParam || pq.ticket_id === inspectParam
      );
      if (match) {
        handleInspectTicket({
          id: match.id,
          ticketId: match.ticket_number,
          ticketUuid: match.ticket_id,
          title: match.title,
          customer: match.customer,
          tier: match.tier,
          category: match.category,
          priority: match.priority as any,
          priorityScore: match.priority_score,
          severity: match.severity,
          status: match.status,
          assignedTeam: match.assigned_team,
          slaElapsed: match.sla_elapsed,
          slaTotal: match.sla_total,
          slaRemaining: match.sla_remaining,
          progress: match.progress,
          triggerCondition: 'MANUAL_OVERRIDE',
          escalationReason: match.evidence_snippet || match.title,
          triggeredAt: new Date().toISOString(),
          evidenceStatus: match.evidence_status,
          evidenceSnippet: match.evidence_snippet,
          evidenceDoc: match.evidence_doc,
          suggestedAction: match.suggested_action,
        });
      }
    }
  }, [searchParams, overviewData, handleInspectTicket]);

  // Build unified escalation registry list from priority queue and audits
  const allEscalations: EscalationRecord[] = useMemo(() => {
    const auditMap = new Map<string, EscalationAuditItem>();
    audits.forEach((a) => {
      if (a.ticket_id) auditMap.set(a.ticket_id, a);
    });

    const records: EscalationRecord[] = [];

    // From overview priority queue
    if (overviewData?.priority_queue) {
      overviewData.priority_queue.forEach((item: DashboardIncidentItem) => {
        const audit = auditMap.get(item.ticket_id);
        records.push({
          id: item.id,
          ticketId: item.ticket_number,
          ticketUuid: item.ticket_id,
          title: item.title,
          customer: item.customer,
          tier: item.tier,
          category: item.category,
          priority: item.priority as any,
          priorityScore: item.priority_score,
          severity: item.severity,
          status: item.status,
          assignedTeam: item.assigned_team,
          slaElapsed: item.sla_elapsed,
          slaTotal: item.sla_total,
          slaRemaining: item.sla_remaining,
          progress: item.progress,
          triggerCondition: audit?.trigger_condition || 'MANUAL_OVERRIDE',
          escalationReason: audit?.escalation_reason || item.evidence_snippet || item.title,
          triggeredAt: audit?.triggered_at ? audit.triggered_at.toString() : new Date().toISOString(),
          evidenceStatus: item.evidence_status,
          evidenceSnippet: item.evidence_snippet,
          evidenceDoc: item.evidence_doc,
          suggestedAction: item.suggested_action,
        });
      });
    }

    // Include audits whose ticket wasn't in the top priority queue
    const coveredTicketIds = new Set(records.map((r) => r.ticketUuid));
    audits.forEach((a) => {
      if (a.ticket_id && !coveredTicketIds.has(a.ticket_id)) {
        coveredTicketIds.add(a.ticket_id);
        records.push({
          id: `audit-${a.id}`,
          ticketId: `TCK-${a.ticket_id.slice(0, 8).toUpperCase()}`,
          ticketUuid: a.ticket_id,
          title: a.escalation_reason || `Escalation on ${a.trigger_condition}`,
          customer: 'Guest Customer',
          tier: 'STANDARD',
          category: 'ESCALATION',
          priority: a.trigger_condition.includes('SAFETY') ? 'CRITICAL' : 'HIGH',
          priorityScore: a.trigger_condition.includes('SAFETY') ? 4.5 : 3.5,
          severity: a.trigger_condition.includes('SAFETY') ? 'SEV-1 CRITICAL' : 'SEV-2 HIGH',
          status: a.new_state || 'ESCALATED',
          assignedTeam: 'General Support',
          slaElapsed: '30m elapsed',
          slaTotal: '3h window',
          slaRemaining: '2h 30m remaining',
          progress: 17,
          triggerCondition: a.trigger_condition,
          escalationReason: a.escalation_reason,
          triggeredAt: a.triggered_at.toString(),
          evidenceStatus: 'Automated Audit Captured',
          evidenceSnippet: a.escalation_reason,
          evidenceDoc: 'audit-payload.json',
          suggestedAction: 'Supervisor Review',
        });
      }
    });

    // Default Sort: Priority score descending, SLA progress descending
    return records.sort((a, b) => b.priorityScore - a.priorityScore || b.progress - a.progress);
  }, [overviewData, audits]);

  // Filtered & Sorted Escalations
  const filteredEscalations = useMemo(() => {
    const list = allEscalations.filter((rec) => {
      // Exclude closed or deleted cases from the active registry
      if (rec.status === 'CLOSED') return false;

      // Search
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const matchId = rec.ticketId.toLowerCase().includes(q) || rec.ticketUuid.toLowerCase().includes(q);
        const matchCust = rec.customer.toLowerCase().includes(q);
        const matchTitle = rec.title.toLowerCase().includes(q);
        if (!matchId && !matchCust && !matchTitle) return false;
      }

      // Priority
      if (filterPriority !== 'ALL' && rec.priority !== filterPriority) {
        return false;
      }

      // Status
      if (filterStatus !== 'ALL' && rec.status.toUpperCase() !== filterStatus) {
        return false;
      }

      // Assigned Team
      if (filterTeam !== 'ALL' && rec.assignedTeam.toLowerCase() !== filterTeam.toLowerCase()) {
        return false;
      }

      // SLA Risk
      if (filterSlaRisk !== 'ALL') {
        if (filterSlaRisk === 'BREACHED' && !rec.slaRemaining.includes('BREACHED')) return false;
        if (filterSlaRisk === 'WARNING' && rec.progress <= 60 && !rec.slaRemaining.includes('BREACHED')) return false;
        if (filterSlaRisk === 'NORMAL' && rec.progress > 60) return false;
      }

      return true;
    });

    // Dynamic Multi-Criterion Sorting
    list.sort((a, b) => {
      if (sortBy === 'priority_desc') return b.priorityScore - a.priorityScore;
      if (sortBy === 'priority_asc') return a.priorityScore - b.priorityScore;
      if (sortBy === 'sla_urgent') return b.progress - a.progress;
      if (sortBy === 'ticket_id') return a.ticketId.localeCompare(b.ticketId);
      return b.priorityScore - a.priorityScore;
    });

    return list;
  }, [allEscalations, searchQuery, filterPriority, filterStatus, filterTeam, filterSlaRisk, sortBy]);

  // Paginated Escalations
  const paginatedEscalations = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredEscalations.slice(start, start + pageSize);
  }, [filteredEscalations, currentPage, pageSize]);

  const totalPages = Math.max(1, Math.ceil(filteredEscalations.length / pageSize));

  // Reset Filters Helper
  const hasActiveFilters = searchQuery !== '' || filterPriority !== 'ALL' || filterStatus !== 'ALL' || filterTeam !== 'ALL' || filterSlaRisk !== 'ALL' || sortBy !== 'priority_desc';
  const handleResetFilters = () => {
    setSearchQuery('');
    setFilterPriority('ALL');
    setFilterStatus('ALL');
    setFilterTeam('ALL');
    setFilterSlaRisk('ALL');
    setSortBy('priority_desc');
    setCurrentPage(1);
  };

  // Run Batch Scan
  const handleScanUnresolved = async () => {
    if (!isStaff || isScanning) return;
    setIsScanning(true);
    setActionFeedback(null);
    try {
      const res = await escalationApi.scanUnresolved(50);
      setActionFeedback({
        type: 'success',
        message: `Batch scan completed. Escalated ${res.escalated_count} unresolved case(s).`,
      });
      fetchOverview();
      fetchAudits();
    } catch (err: any) {
      const msg = err?.response?.data?.error?.message || err?.response?.data?.detail || 'Failed to execute batch scan.';
      setActionFeedback({ type: 'error', message: msg });
    } finally {
      setIsScanning(false);
    }
  };

  // Evaluate Conversation Turn
  const handleEvaluateTurn = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!evalConvId.trim() || isEvaluating) return;
    setIsEvaluating(true);
    setActionFeedback(null);
    try {
      const res = await escalationApi.evaluateEscalation({
        conversation_id: evalConvId.trim(),
        message_content: evalContent.trim() || undefined,
      });
      setActionFeedback({
        type: res.should_escalate ? 'error' : 'success',
        message: res.should_escalate
          ? `Escalation Triggered! Condition: ${res.trigger_condition}. Reason: ${res.reason}`
          : 'No escalation needed. Conversation within acceptable parameters.',
      });
      fetchOverview();
      fetchAudits();
    } catch (err: any) {
      const msg = err?.response?.data?.error?.message || err?.response?.data?.detail || 'Failed to evaluate conversation turn.';
      setActionFeedback({ type: 'error', message: msg });
    } finally {
      setIsEvaluating(false);
    }
  };

  const getPriorityVariant = (priority: string): BadgeVariant => {
    const p = (priority || '').toUpperCase();
    if (p === 'CRITICAL' || p === 'URGENT') return 'danger';
    if (p === 'HIGH') return 'warning';
    if (p === 'MEDIUM') return 'neutral';
    return 'neutral';
  };

  const getTriggerVariant = (condition: string): BadgeVariant => {
    const c = (condition || '').toUpperCase();
    if (c.includes('SAFETY') || c.includes('LEGAL') || c.includes('THREAT')) return 'danger';
    if (c.includes('FRUSTRATION') || c.includes('SLOPE') || c.includes('NEGATIVE')) return 'warning';
    if (c.includes('AFTER_HOURS')) return 'cyan';
    return 'neutral';
  };

  const formatTriggerDescription = (condition: string): string => {
    const c = (condition || '').toUpperCase();
    if (c === 'MANUAL_OVERRIDE') return 'Manual Override';
    if (c === 'RULE_A_SLOPE') return 'Negative Sentiment Slope';
    if (c === 'RULE_A_REPEATED') return 'Repeated Frustration';
    if (c === 'RULE_B_UNRESOLVED') return 'Unresolved Timeout (15m)';
    if (c === 'AFTER_HOURS') return 'After-Hours Coverage';
    if (c === 'SAFETY_POLICY') return 'Safety & Legal Compliance';
    return condition.replace(/_/g, ' ');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '28px', paddingBottom: '120px' }}>
      {/* =========================================================================
          1. HEADER
          ========================================================================= */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '16px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <h1 style={{ fontSize: '1.875rem', fontWeight: 700, letterSpacing: '-0.03em', color: 'var(--text-primary)' }}>
              Escalation Management
            </h1>
            <Badge variant="cyan" size="sm">
              Live Telemetry
            </Badge>
          </div>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)', marginTop: '6px', maxWidth: '640px' }}>
            Monitor, prioritize and resolve cases requiring elevated attention.
          </p>
        </div>

        {/* Staff Action Buttons */}
        {isStaff && (
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setShowEvalModal(true)}
              style={{ fontSize: '0.75rem', padding: '6px 14px' }}
            >
              Evaluate Turn
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={isScanning}
              onClick={handleScanUnresolved}
              style={{ fontSize: '0.75rem', padding: '6px 14px' }}
            >
              ⚡ Scan Unresolved (Rule B)
            </Button>
          </div>
        )}
      </div>

      {/* Action Feedback Banner */}
      {actionFeedback && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            background: actionFeedback.type === 'success' ? 'var(--accent-emerald-light)' : 'var(--accent-rose-light)',
            color: actionFeedback.type === 'success' ? 'var(--accent-emerald)' : 'var(--accent-rose)',
            fontSize: '0.8125rem',
            border: `1px solid ${actionFeedback.type === 'success' ? 'rgba(16, 185, 129, 0.3)' : 'rgba(244, 63, 94, 0.3)'}`,
          }}
        >
          {actionFeedback.message}
        </div>
      )}

      {/* =========================================================================
          2. SUMMARY CARDS (Database-Backed Counts)
          ========================================================================= */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '16px' }}>
        {/* Card 1: Active Escalations */}
        <div
          className="glass-card-nested"
          style={{
            padding: '18px 20px',
            borderLeft: '4px solid var(--accent-rose)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Active Escalations
            </span>
            <AlertTriangleIcon size={18} style={{ color: 'var(--accent-rose)' }} />
          </div>
          <div style={{ fontSize: '2rem', fontWeight: 700, color: 'var(--text-primary)', margin: '8px 0 2px' }}>
            {overviewData ? overviewData.escalated_tickets : isLoadingOverview ? '--' : 0}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--accent-rose)', fontWeight: 600 }}>
            {overviewData ? `${overviewData.escalated_tickets} Cases in window` : 'Authoritative count'}
          </span>
        </div>

        {/* Card 2: Critical Cases */}
        <div
          className="glass-card-nested"
          style={{
            padding: '18px 20px',
            borderLeft: '4px solid var(--accent-amber)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Critical Cases
            </span>
            <ShieldCheckIcon size={18} style={{ color: 'var(--accent-amber)' }} />
          </div>
          <div style={{ fontSize: '2rem', fontWeight: 700, color: 'var(--text-primary)', margin: '8px 0 2px' }}>
            {allEscalations.filter((e) => ['CRITICAL', 'HIGH'].includes(e.priority)).length}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--accent-amber)', fontWeight: 600 }}>
            Requires immediate supervisor review
          </span>
        </div>

        {/* Card 3: Approaching SLA */}
        <div
          className="glass-card-nested"
          style={{
            padding: '18px 20px',
            borderLeft: '4px solid var(--accent-cyan)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Approaching SLA
            </span>
            <ClockIcon size={18} style={{ color: 'var(--accent-cyan)' }} />
          </div>
          <div style={{ fontSize: '2rem', fontWeight: 700, color: 'var(--text-primary)', margin: '8px 0 2px' }}>
            {overviewData ? overviewData.sla_warning_count : isLoadingOverview ? '--' : 0}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--accent-cyan)', fontWeight: 600 }}>
            Within SLA warning threshold
          </span>
        </div>

        {/* Card 4: Breached SLAs */}
        <div
          className="glass-card-nested"
          style={{
            padding: '18px 20px',
            borderLeft: '4px solid var(--accent-emerald)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Breached SLAs
            </span>
            <CheckCircleIcon size={18} style={{ color: 'var(--accent-emerald)' }} />
          </div>
          <div style={{ fontSize: '2rem', fontWeight: 700, color: 'var(--text-primary)', margin: '8px 0 2px' }}>
            {overviewData ? overviewData.sla_breached_count : isLoadingOverview ? '--' : 0}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--accent-emerald)', fontWeight: 600 }}>
            {overviewData && overviewData.sla_breached_count === 0 ? 'Zero breaches detected' : 'Target breached'}
          </span>
        </div>
      </div>

      {/* Sub-Navigation Tabs */}
      <div style={{ display: 'flex', gap: '8px', borderBottom: '1px solid rgba(255, 255, 255, 0.08)', paddingBottom: '12px' }}>
        <button
          type="button"
          onClick={() => setActiveViewTab('registry')}
          className="glass-outline-pill"
          style={{
            padding: '8px 18px',
            fontSize: '0.8125rem',
            fontWeight: 600,
            color: activeViewTab === 'registry' ? '#ffffff' : 'var(--text-muted)',
            background: activeViewTab === 'registry' ? 'var(--accent-primary)' : 'transparent',
            borderColor: activeViewTab === 'registry' ? 'var(--accent-primary)' : 'var(--border-card)',
            cursor: 'pointer',
          }}
        >
          Escalation Registry ({allEscalations.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveViewTab('audits')}
          className="glass-outline-pill"
          style={{
            padding: '8px 18px',
            fontSize: '0.8125rem',
            fontWeight: 600,
            color: activeViewTab === 'audits' ? '#ffffff' : 'var(--text-muted)',
            background: activeViewTab === 'audits' ? 'var(--accent-primary)' : 'transparent',
            borderColor: activeViewTab === 'audits' ? 'var(--accent-primary)' : 'var(--border-card)',
            cursor: 'pointer',
          }}
        >
          Immutable Audit Trail ({audits.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveViewTab('afterHours')}
          className="glass-outline-pill"
          style={{
            padding: '8px 18px',
            fontSize: '0.8125rem',
            fontWeight: 600,
            color: activeViewTab === 'afterHours' ? '#ffffff' : 'var(--text-muted)',
            background: activeViewTab === 'afterHours' ? 'var(--accent-primary)' : 'transparent',
            borderColor: activeViewTab === 'afterHours' ? 'var(--accent-primary)' : 'var(--border-card)',
            cursor: 'pointer',
          }}
        >
          After-Hours & On-Call Schedule
        </button>
      </div>

      {/* =========================================================================
          VIEW TAB 1: ESCALATION REGISTRY
          ========================================================================= */}
      {activeViewTab === 'registry' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          {/* Filters Bar */}
          <div
            className="glass-card-nested"
            style={{
              padding: '16px 20px',
              display: 'flex',
              flexWrap: 'wrap',
              gap: '14px',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            {/* Search Input */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: '240px', flex: '1 1 240px' }}>
              <div
                style={{
                  position: 'relative',
                  width: '100%',
                }}
              >
                <SearchIcon
                  size={16}
                  style={{
                    position: 'absolute',
                    left: '12px',
                    top: '50%',
                    transform: 'translateY(-50%)',
                    color: 'var(--text-muted)',
                  }}
                />
                <input
                  type="text"
                  placeholder="Search by ticket ID or customer..."
                  value={searchQuery}
                  onChange={(e) => {
                    setSearchQuery(e.target.value);
                    setCurrentPage(1);
                  }}
                  style={{
                    width: '100%',
                    padding: '8px 12px 8px 36px',
                    borderRadius: 'var(--radius-lg)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.8125rem',
                    outline: 'none',
                  }}
                />
              </div>
            </div>

            {/* Filter Dropdowns & Sorting */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              {/* Priority */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Priority:</span>
                <select
                  value={filterPriority}
                  onChange={(e) => {
                    setFilterPriority(e.target.value);
                    setCurrentPage(1);
                  }}
                  style={{
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.75rem',
                    outline: 'none',
                  }}
                >
                  <option value="ALL">All Priorities</option>
                  <option value="CRITICAL">Critical</option>
                  <option value="HIGH">High</option>
                  <option value="MEDIUM">Medium</option>
                  <option value="LOW">Low</option>
                </select>
              </div>

              {/* Status */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Status:</span>
                <select
                  value={filterStatus}
                  onChange={(e) => {
                    setFilterStatus(e.target.value);
                    setCurrentPage(1);
                  }}
                  style={{
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.75rem',
                    outline: 'none',
                  }}
                >
                  <option value="ALL">All Statuses</option>
                  <option value="ESCALATED">Escalated</option>
                  <option value="OPEN">Open</option>
                  <option value="IN_PROGRESS">In Progress</option>
                  <option value="RESOLVED">Resolved</option>
                </select>
              </div>

              {/* Assigned Team */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Team:</span>
                <select
                  value={filterTeam}
                  onChange={(e) => {
                    setFilterTeam(e.target.value);
                    setCurrentPage(1);
                  }}
                  style={{
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.75rem',
                    outline: 'none',
                  }}
                >
                  <option value="ALL">All Teams</option>
                  <option value="General Support">General Support</option>
                  <option value="Billing">Billing</option>
                  <option value="Technical Support">Technical Support</option>
                  <option value="Hardware Support">Hardware Support</option>
                </select>
              </div>

              {/* SLA Risk */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>SLA Risk:</span>
                <select
                  value={filterSlaRisk}
                  onChange={(e) => {
                    setFilterSlaRisk(e.target.value);
                    setCurrentPage(1);
                  }}
                  style={{
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.75rem',
                    outline: 'none',
                  }}
                >
                  <option value="ALL">All SLA States</option>
                  <option value="BREACHED">Breached Only</option>
                  <option value="WARNING">Approaching (&gt;60%)</option>
                  <option value="NORMAL">Healthy Margin</option>
                </select>
              </div>

              {/* Sort By */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Sort:</span>
                <select
                  value={sortBy}
                  onChange={(e) => setSortBy(e.target.value as any)}
                  style={{
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-input)',
                    border: '1px solid var(--border-card)',
                    color: 'var(--text-primary)',
                    fontSize: '0.75rem',
                    outline: 'none',
                  }}
                >
                  <option value="priority_desc">Priority (Highest First)</option>
                  <option value="priority_asc">Priority (Lowest First)</option>
                  <option value="sla_urgent">SLA (Most Urgent)</option>
                  <option value="ticket_id">Ticket ID (Ascending)</option>
                </select>
              </div>

              {/* Reset button */}
              {hasActiveFilters && (
                <button
                  type="button"
                  onClick={handleResetFilters}
                  className="glass-outline-pill"
                  style={{
                    padding: '6px 12px',
                    fontSize: '0.75rem',
                    color: 'var(--accent-rose)',
                    borderColor: 'rgba(239, 68, 68, 0.4)',
                    cursor: 'pointer',
                  }}
                >
                  Reset Filters
                </button>
              )}
            </div>
          </div>

          {/* Escalation Registry Table Card */}
          <div className="glass-hero-container" style={{ padding: '24px 28px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '10px' }}>
              <div>
                <h2 style={{ fontSize: '1.25rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
                  Urgent & Critical Escalations Registry
                </h2>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                  Showing {filteredEscalations.length} total matched escalation(s) sorted by priority score and SLA risk.
                </p>
              </div>

              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Page {currentPage} of {totalPages}
              </div>
            </div>

            {isLoadingOverview ? (
              <div style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
                <ClockIcon size={32} style={{ margin: '0 auto 12px', color: 'var(--accent-primary)' }} />
                <p style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>Loading Escalation Registry...</p>
                <p style={{ fontSize: '0.75rem', marginTop: '4px' }}>Querying active escalation queue and SLA tracking engines.</p>
              </div>
            ) : overviewError ? (
              <div style={{ padding: '40px', textAlign: 'center', color: 'var(--accent-rose)' }}>
                <AlertTriangleIcon size={32} style={{ margin: '0 auto 12px' }} />
                <p style={{ fontWeight: 600 }}>Failed to Load Escalation Registry</p>
                <p style={{ fontSize: '0.75rem', marginTop: '4px' }}>{overviewError}</p>
                <div style={{ marginTop: '16px' }}>
                  <Button size="sm" variant="outline" onClick={fetchOverview}>Retry</Button>
                </div>
              </div>
            ) : filteredEscalations.length === 0 ? (
              <div style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
                <CheckCircleIcon size={32} style={{ margin: '0 auto 12px', color: 'var(--accent-emerald)' }} />
                <p style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>No Escalations Matched</p>
                <p style={{ fontSize: '0.8125rem', marginTop: '6px' }}>
                  {hasActiveFilters ? 'Try adjusting your search query or filter selections.' : 'Operational queue clear. Zero cases requiring escalation.'}
                </p>
                {hasActiveFilters && (
                  <div style={{ marginTop: '16px' }}>
                    <Button size="sm" variant="outline" onClick={handleResetFilters}>Clear Filters</Button>
                  </div>
                )}
              </div>
            ) : (
              <>
                <div
                  tabIndex={0}
                  role="region"
                  aria-label="Escalation cases registry table"
                  style={{ overflowX: 'auto', outline: 'none' }}
                >
                  <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.8125rem', minWidth: '980px' }}>
                    <thead style={{ position: 'sticky', top: 0, zIndex: 10, background: 'rgba(13, 23, 46, 0.96)', backdropFilter: 'blur(12px)' }}>
                      <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.1)', color: 'var(--text-muted)' }}>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '150px' }}>TICKET ID</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '180px' }}>CUSTOMER</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '120px' }}>CATEGORY</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '140px' }}>PRIORITY</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '170px' }}>SLA REMAINING</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '200px' }}>TRIGGER CONDITION</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '140px' }}>ASSIGNED TEAM</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '110px' }}>STATUS</th>
                        <th style={{ padding: '12px 14px', fontWeight: 600, width: '100px', textAlign: 'right' }}>ACTION</th>
                      </tr>
                    </thead>
                    <tbody>
                      {paginatedEscalations.map((item) => {
                        const isCritical = item.priority === 'CRITICAL';
                        const isHigh = item.priority === 'HIGH';

                        return (
                          <tr
                            key={item.id}
                            style={{
                              borderBottom: '1px solid var(--border-subtle)',
                              borderLeft: isCritical ? '4px solid var(--accent-rose)' : isHigh ? '4px solid var(--accent-amber)' : '4px solid transparent',
                              background: isCritical ? 'rgba(244, 63, 94, 0.04)' : isHigh ? 'rgba(245, 158, 11, 0.02)' : 'transparent',
                              transition: 'all 0.2s ease',
                            }}
                          >
                            {/* Ticket ID */}
                            <td style={{ padding: '14px', fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                              <span
                                style={{
                                  color: 'var(--accent-cyan)',
                                  background: 'rgba(6, 182, 212, 0.1)',
                                  padding: '3px 8px',
                                  borderRadius: 'var(--radius-sm)',
                                  border: '1px solid rgba(6, 182, 212, 0.25)',
                                  display: 'inline-block',
                                }}
                              >
                                {item.ticketId}
                              </span>
                            </td>

                            {/* Customer */}
                            <td style={{ padding: '14px' }}>
                              <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{item.customer}</div>
                              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                                Tier: <span style={{ color: 'var(--accent-cyan)' }}>{item.tier}</span>
                              </div>
                            </td>

                            {/* Category */}
                            <td style={{ padding: '14px', color: 'var(--text-secondary)' }}>
                              {item.category}
                            </td>

                            {/* Priority */}
                            <td style={{ padding: '14px' }}>
                              <Badge variant={getPriorityVariant(item.priority)} size="sm">
                                {item.priority} ({item.priorityScore.toFixed(2)})
                              </Badge>
                            </td>

                            {/* SLA Remaining */}
                            <td style={{ padding: '14px' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: item.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-amber)', fontWeight: 600 }}>
                                <ClockIcon size={13} />
                                <span>{item.slaRemaining}</span>
                              </div>
                              <div style={{ height: '4px', width: '90px', borderRadius: 'var(--radius-full)', background: 'rgba(255, 255, 255, 0.08)', overflow: 'hidden', marginTop: '4px' }}>
                                <div
                                  style={{
                                    height: '100%',
                                    borderRadius: 'var(--radius-full)',
                                    background: item.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-cyan)',
                                    width: `${Math.min(100, item.progress)}%`,
                                  }}
                                />
                              </div>
                              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                                {item.progress}% consumed
                              </div>
                            </td>

                            {/* Trigger Condition (Human-Friendly Description) */}
                            <td style={{ padding: '14px' }}>
                              <Badge variant={getTriggerVariant(item.triggerCondition)} size="sm">
                                {formatTriggerDescription(item.triggerCondition)}
                              </Badge>
                            </td>

                            {/* Assigned Team */}
                            <td style={{ padding: '14px', color: 'var(--text-secondary)' }}>
                              {item.assignedTeam}
                            </td>

                            {/* Status */}
                            <td style={{ padding: '14px' }}>
                              <Badge variant="cyan" size="sm">
                                {item.status}
                              </Badge>
                            </td>

                            {/* Action */}
                            <td style={{ padding: '14px', textAlign: 'right' }}>
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => handleInspectTicket(item)}
                                style={{
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  gap: '6px',
                                  fontSize: '0.75rem',
                                  padding: '5px 12px',
                                }}
                              >
                                <SearchIcon size={12} />
                                <span>Inspect</span>
                              </Button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Pagination Controls */}
                {totalPages > 1 && (
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      marginTop: '20px',
                      paddingTop: '16px',
                      borderTop: '1px solid var(--border-subtle)',
                      flexWrap: 'wrap',
                      gap: '10px',
                    }}
                  >
                    <div style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>
                      Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, filteredEscalations.length)} of {filteredEscalations.length} records
                    </div>

                    <div style={{ display: 'flex', gap: '8px' }}>
                      <button
                        type="button"
                        disabled={currentPage === 1}
                        onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                        className="glass-outline-pill"
                        style={{
                          padding: '6px 14px',
                          fontSize: '0.75rem',
                          cursor: currentPage === 1 ? 'not-allowed' : 'pointer',
                          opacity: currentPage === 1 ? 0.4 : 1,
                        }}
                      >
                        ← Previous
                      </button>
                      <button
                        type="button"
                        disabled={currentPage === totalPages}
                        onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                        className="glass-outline-pill"
                        style={{
                          padding: '6px 14px',
                          fontSize: '0.75rem',
                          cursor: currentPage === totalPages ? 'not-allowed' : 'pointer',
                          opacity: currentPage === totalPages ? 0.4 : 1,
                        }}
                      >
                        Next →
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* =========================================================================
          VIEW TAB 2: IMMUTABLE AUDIT TRAIL
          ========================================================================= */}
      {activeViewTab === 'audits' && (
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '10px' }}>
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                Deterministic Escalation Audits (Immutable Trail)
              </h3>
              <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                Live audit events from Rule A (Slope/Repeated), Rule B (Unresolved 15m), and Rules C & D (After-hours).
              </p>
            </div>

            {/* Trigger Filter Pills */}
            <div style={{ display: 'flex', gap: '4px', background: 'var(--surface-glass-base)', padding: '4px', borderRadius: 'var(--radius-md)', border: 'var(--border-subtle)' }}>
              {['ALL', 'MANUAL_OVERRIDE', 'HIGH_RISK_SAFETY', 'SENTIMENT_FRUSTRATION', 'AFTER_HOURS_HOLD'].map((cond) => (
                <button
                  key={cond}
                  onClick={() => setAuditTriggerFilter(cond)}
                  style={{
                    padding: '4px 10px',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: '0.6875rem',
                    fontWeight: 600,
                    border: 'none',
                    cursor: 'pointer',
                    background: auditTriggerFilter === cond ? 'var(--accent-primary)' : 'transparent',
                    color: auditTriggerFilter === cond ? '#ffffff' : 'var(--text-secondary)',
                    transition: 'all var(--transition-fast)',
                  }}
                >
                  {cond.replace(/_/g, ' ')}
                </button>
              ))}
            </div>
          </div>

          {isLoadingAudits ? (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.875rem' }}>
              Loading escalation audit records...
            </div>
          ) : auditsError ? (
            <div style={{ padding: '16px', background: 'var(--accent-rose-light)', color: 'var(--accent-rose)', borderRadius: 'var(--radius-md)', fontSize: '0.8125rem' }}>
              {auditsError}
            </div>
          ) : audits.length === 0 ? (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.8125rem' }}>
              No escalation records found for the selected filter.
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.8125rem' }}>
                <thead>
                  <tr style={{ borderBottom: 'var(--border-subtle)', color: 'var(--text-muted)' }}>
                    <th style={{ padding: '10px 12px' }}>CONVERSATION / TICKET</th>
                    <th style={{ padding: '10px 12px' }}>TRIGGER CONDITION</th>
                    <th style={{ padding: '10px 12px' }}>STATE CHANGE</th>
                    <th style={{ padding: '10px 12px' }}>ESCALATION REASON</th>
                    <th style={{ padding: '10px 12px' }}>TIMESTAMP</th>
                    <th style={{ padding: '10px 12px' }}>DETAILS</th>
                  </tr>
                </thead>
                <tbody>
                  {audits.map((a) => (
                    <tr key={a.id} style={{ borderBottom: 'var(--border-subtle)' }}>
                      <td style={{ padding: '12px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>
                        <div>{a.conversation_id ? a.conversation_id.substring(0, 8) + '...' : 'System'}</div>
                        {a.ticket_id && (
                          <div style={{ fontSize: '0.6875rem', color: 'var(--accent-primary)' }}>
                            Ticket: {a.ticket_id.substring(0, 8)}...
                          </div>
                        )}
                      </td>
                      <td style={{ padding: '12px' }}>
                        <Badge variant={getTriggerVariant(a.trigger_condition)} size="sm">
                          {a.trigger_condition}
                        </Badge>
                      </td>
                      <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>
                        {a.previous_state} → <strong style={{ color: 'var(--accent-rose)' }}>{a.new_state}</strong>
                      </td>
                      <td style={{ padding: '12px', color: 'var(--text-primary)', maxWidth: '320px', lineHeight: 1.4 }}>
                        {a.escalation_reason}
                      </td>
                      <td style={{ padding: '12px', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                        {new Date(a.triggered_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                      </td>
                      <td style={{ padding: '12px' }}>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setSelectedAudit(a)}
                          style={{ fontSize: '0.6875rem', padding: '4px 8px' }}
                        >
                          Inspect
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {/* =========================================================================
          VIEW TAB 3: AFTER-HOURS & ON-CALL SCHEDULE
          ========================================================================= */}
      {activeViewTab === 'afterHours' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: '20px' }}>
          {/* Card 1: Safety Alarms */}
          <Card variant="highlight" style={{ borderLeft: '4px solid var(--accent-rose)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <AlertTriangleIcon size={24} style={{ color: 'var(--accent-rose)' }} />
                <div>
                  <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                    High-Risk Safety Alarms
                  </h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    {audits.filter((a) => a.trigger_condition.toUpperCase().includes('SAFETY') || a.trigger_condition.toUpperCase().includes('LEGAL')).length} safety or legal escalation audit(s)
                  </p>
                </div>
              </div>
              <Badge
                variant={
                  audits.filter((a) => a.trigger_condition.toUpperCase().includes('SAFETY') || a.trigger_condition.toUpperCase().includes('LEGAL')).length > 0
                    ? 'danger'
                    : 'neutral'
                }
              >
                {audits.filter((a) => a.trigger_condition.toUpperCase().includes('SAFETY') || a.trigger_condition.toUpperCase().includes('LEGAL')).length > 0
                  ? 'Alarms Active'
                  : 'Clear'}
              </Badge>
            </div>
          </Card>

          {/* Card 2: Business Hours & On-Call Status */}
          <Card variant="base">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <ShieldCheckIcon
                  size={24}
                  style={{ color: afterHours?.is_after_hours ? 'var(--accent-amber)' : 'var(--accent-emerald)' }}
                />
                <div>
                  <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                    {isLoadingAfterHours ? 'Checking Hours...' : afterHours?.is_after_hours ? 'After-Hours Operational' : 'Business Hours Active'}
                  </h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    {afterHours?.reason || 'Calendar evaluation based on operational business hours'}
                  </p>
                </div>
              </div>
              <Badge variant={afterHours?.is_after_hours ? 'warning' : 'success'}>
                {afterHours?.is_after_hours ? 'After-Hours' : 'Open'}
              </Badge>
            </div>

            {afterHours && (
              <div style={{ marginTop: '12px', paddingTop: '10px', borderTop: 'var(--border-subtle)', display: 'flex', justifyContent: 'space-between', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                <span>Timezone: {afterHours.calendar_timezone}</span>
                {afterHours.next_eligible_business_time && (
                  <span>Next Opening: {new Date(afterHours.next_eligible_business_time).toLocaleString()}</span>
                )}
              </div>
            )}
          </Card>
        </div>
      )}

      {/* =========================================================================
          AUTHORITATIVE TICKET INSPECTION DOSSIER MODAL
          ========================================================================= */}
      {selectedTicket && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0, 0, 0, 0.7)',
            backdropFilter: 'blur(8px)',
            WebkitBackdropFilter: 'blur(8px)',
            padding: '20px',
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setSelectedTicket(null);
          }}
        >
          <div
            className="glass-card-nested"
            style={{
              width: '740px',
              maxWidth: '95vw',
              maxHeight: '90vh',
              overflowY: 'auto',
              background: 'linear-gradient(145deg, rgba(15, 23, 42, 0.95) 0%, rgba(30, 41, 59, 0.95) 100%)',
              border: '1px solid rgba(255, 255, 255, 0.15)',
              boxShadow: '0 25px 80px rgba(0, 0, 0, 0.7)',
              borderRadius: 'var(--radius-2xl)',
              padding: '28px',
              display: 'flex',
              flexDirection: 'column',
              gap: '20px',
            }}
          >
            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', borderBottom: '1px solid rgba(255, 255, 255, 0.1)', paddingBottom: '16px' }}>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: '0.875rem',
                      fontWeight: 700,
                      color: 'var(--accent-cyan)',
                      background: 'rgba(6, 182, 212, 0.12)',
                      padding: '2px 8px',
                      borderRadius: 'var(--radius-sm)',
                    }}
                  >
                    {selectedTicket.ticketId}
                  </span>
                  <Badge variant={getPriorityVariant(selectedTicket.priority)} size="sm">
                    {selectedTicket.priority} ({selectedTicket.priorityScore.toFixed(2)})
                  </Badge>
                  <Badge variant="cyan" size="sm">
                    STATUS: {selectedTicket.status}
                  </Badge>
                </div>
                <h3 style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: '8px' }}>
                  {ticketDetail?.title || selectedTicket.title}
                </h3>
              </div>

              <button
                type="button"
                onClick={() => setSelectedTicket(null)}
                className="glass-outline-pill"
                style={{
                  width: '32px',
                  height: '32px',
                  padding: 0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: 'pointer',
                  fontSize: '1rem',
                  color: 'var(--text-muted)',
                }}
              >
                ✕
              </button>
            </div>

            {/* Modal Subtabs */}
            <div style={{ display: 'flex', gap: '8px', borderBottom: '1px solid rgba(255, 255, 255, 0.08)', paddingBottom: '8px' }}>
              <button
                type="button"
                onClick={() => setDetailModalTab('overview')}
                style={{
                  padding: '6px 14px',
                  borderRadius: 'var(--radius-md)',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  border: 'none',
                  cursor: 'pointer',
                  background: detailModalTab === 'overview' ? 'var(--accent-primary)' : 'transparent',
                  color: detailModalTab === 'overview' ? '#ffffff' : 'var(--text-muted)',
                }}
              >
                Overview & SLA
              </button>
              <button
                type="button"
                onClick={() => setDetailModalTab('routing')}
                style={{
                  padding: '6px 14px',
                  borderRadius: 'var(--radius-md)',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  border: 'none',
                  cursor: 'pointer',
                  background: detailModalTab === 'routing' ? 'var(--accent-primary)' : 'transparent',
                  color: detailModalTab === 'routing' ? '#ffffff' : 'var(--text-muted)',
                }}
              >
                Routing History ({routingHistory.length})
              </button>
              <button
                type="button"
                onClick={() => setDetailModalTab('evidence')}
                style={{
                  padding: '6px 14px',
                  borderRadius: 'var(--radius-md)',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  border: 'none',
                  cursor: 'pointer',
                  background: detailModalTab === 'evidence' ? 'var(--accent-primary)' : 'transparent',
                  color: detailModalTab === 'evidence' ? '#ffffff' : 'var(--text-muted)',
                }}
              >
                Evidence Verification
              </button>
            </div>

            {/* Tab 1: Overview & SLA */}
            {detailModalTab === 'overview' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                {isLoadingDetail && (
                  <div style={{ fontSize: '0.75rem', color: 'var(--accent-cyan)' }}>
                    Loading authoritative ticket data...
                  </div>
                )}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px' }}>
                  <div style={{ padding: '12px', borderRadius: 'var(--radius-lg)', background: 'rgba(0, 0, 0, 0.25)', border: '1px solid var(--border-subtle)' }}>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Customer</span>
                    <p style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-primary)', marginTop: '2px' }}>
                      {selectedTicket.customer}
                    </p>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--accent-cyan)' }}>Tier: {selectedTicket.tier}</span>
                  </div>

                  <div style={{ padding: '12px', borderRadius: 'var(--radius-lg)', background: 'rgba(0, 0, 0, 0.25)', border: '1px solid var(--border-subtle)' }}>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Assigned Team</span>
                    <p style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-primary)', marginTop: '2px' }}>
                      {ticketDetail?.assigned_team || selectedTicket.assignedTeam}
                    </p>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Category: {selectedTicket.category}</span>
                  </div>

                  <div style={{ padding: '12px', borderRadius: 'var(--radius-lg)', background: 'rgba(0, 0, 0, 0.25)', border: '1px solid var(--border-subtle)' }}>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>SLA Status</span>
                    <p style={{ fontSize: '0.875rem', fontWeight: 700, color: selectedTicket.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-amber)', marginTop: '2px' }}>
                      {selectedTicket.slaRemaining}
                    </p>
                    <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>{selectedTicket.progress}% consumed</span>
                  </div>
                </div>

                {/* Summary / Reason */}
                <div>
                  <span style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
                    Escalation Summary & Incident Context
                  </span>
                  <div
                    style={{
                      marginTop: '6px',
                      padding: '12px 14px',
                      borderRadius: 'var(--radius-md)',
                      background: 'rgba(0, 0, 0, 0.3)',
                      border: '1px solid var(--border-subtle)',
                      fontSize: '0.8125rem',
                      lineHeight: 1.55,
                      color: 'var(--text-secondary)',
                    }}
                  >
                    {ticketDetail?.masked_summary || ticketDetail?.description || selectedTicket.escalationReason}
                  </div>
                </div>

                {/* SLA Progress Bar */}
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '6px' }}>
                    <span>SLA Progress: {selectedTicket.slaElapsed}</span>
                    <span>Window: {selectedTicket.slaTotal}</span>
                  </div>
                  <div
                    style={{
                      height: '8px',
                      width: '100%',
                      borderRadius: 'var(--radius-full)',
                      background: 'rgba(255, 255, 255, 0.08)',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        height: '100%',
                        borderRadius: 'var(--radius-full)',
                        background:
                          selectedTicket.progress > 60
                            ? 'linear-gradient(90deg, var(--accent-primary) 0%, var(--accent-rose) 100%)'
                            : 'linear-gradient(90deg, var(--accent-primary) 0%, var(--accent-cyan) 100%)',
                        width: `${selectedTicket.progress}%`,
                      }}
                    />
                  </div>
                </div>
              </div>
            )}

            {/* Tab 2: Routing History */}
            {detailModalTab === 'routing' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                {routingHistory.length === 0 ? (
                  <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.8125rem' }}>
                    No previous routing transitions recorded for this ticket.
                  </div>
                ) : (
                  routingHistory.map((rh, idx) => (
                    <div
                      key={rh.event_id || idx}
                      style={{
                        padding: '12px 14px',
                        borderRadius: 'var(--radius-md)',
                        background: 'rgba(0, 0, 0, 0.25)',
                        border: '1px solid var(--border-subtle)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        fontSize: '0.75rem',
                      }}
                    >
                      <div>
                        <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{rh.event_type || 'TRANSITION'}</span>
                        <p style={{ color: 'var(--text-muted)', marginTop: '2px' }}>
                          {rh.event_payload?.reason || rh.event_payload?.action || 'Automated escalation routing policy applied.'}
                        </p>
                      </div>
                      <span style={{ color: 'var(--accent-cyan)', fontFamily: 'var(--font-mono)' }}>
                        {rh.created_at ? new Date(rh.created_at).toLocaleString() : ''}
                      </span>
                    </div>
                  ))
                )}
              </div>
            )}

            {/* Tab 3: Evidence Verification */}
            {detailModalTab === 'evidence' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div style={{ padding: '14px', borderRadius: 'var(--radius-lg)', background: 'rgba(0, 0, 0, 0.25)', border: '1px solid var(--border-subtle)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                    <SparklesIcon size={16} style={{ color: 'var(--accent-cyan)' }} />
                    <span style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                      Cryptographic Evidence Verification Pipeline
                    </span>
                  </div>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                    Status: <strong style={{ color: 'var(--accent-emerald)' }}>{selectedTicket.evidenceStatus}</strong>
                  </p>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
                    Attached Payload: <code style={{ color: 'var(--accent-cyan)' }}>{selectedTicket.evidenceDoc}</code>
                  </p>
                  <div
                    style={{
                      marginTop: '10px',
                      padding: '10px 12px',
                      borderRadius: 'var(--radius-sm)',
                      background: 'rgba(0, 0, 0, 0.35)',
                      fontFamily: 'var(--font-mono)',
                      fontSize: '0.75rem',
                      color: 'var(--text-secondary)',
                    }}
                  >
                    "{selectedTicket.evidenceSnippet}"
                  </div>
                </div>
              </div>
            )}

            {/* Footer Buttons */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: '1px solid rgba(255, 255, 255, 0.1)', paddingTop: '16px', marginTop: '4px' }}>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setSelectedTicket(null)}
              >
                Close
              </Button>

              <Button
                variant="primary"
                size="sm"
                onClick={() => {
                  setSelectedTicket(null);
                  navigate('/tickets');
                }}
                style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
              >
                <span>Open in Tickets Management</span>
                <span>→</span>
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Inspect Audit Detail Modal (Audits Tab) */}
      {selectedAudit && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0, 0, 0, 0.7)',
            backdropFilter: 'blur(6px)',
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setSelectedAudit(null);
          }}
        >
          <div
            className="glass-card-nested"
            style={{
              width: '560px',
              maxWidth: '90vw',
              padding: '24px',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                Audit Record Inspection
              </div>
              <Button size="sm" variant="ghost" onClick={() => setSelectedAudit(null)}>
                ✕
              </Button>
            </div>

            <div style={{ fontSize: '0.75rem', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>Audit ID: </span>
                <span style={{ fontFamily: 'var(--font-mono)' }}>{selectedAudit.id}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>Trigger Condition: </span>
                <Badge variant={getTriggerVariant(selectedAudit.trigger_condition)} size="sm">
                  {selectedAudit.trigger_condition}
                </Badge>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>Transition: </span>
                <span>{selectedAudit.previous_state} → {selectedAudit.new_state}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>Reason: </span>
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{selectedAudit.escalation_reason}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>Triggered At: </span>
                <span>{new Date(selectedAudit.triggered_at).toLocaleString()}</span>
              </div>
            </div>

            <div>
              <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '4px' }}>
                AUDIT PAYLOAD
              </div>
              <pre
                style={{
                  padding: '10px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'rgba(0, 0, 0, 0.3)',
                  fontSize: '0.6875rem',
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--accent-cyan)',
                  maxHeight: '180px',
                  overflowY: 'auto',
                }}
              >
                {JSON.stringify(selectedAudit.escalation_payload, null, 2)}
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* Manual Evaluate Turn Modal */}
      {showEvalModal && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0, 0, 0, 0.7)',
            backdropFilter: 'blur(6px)',
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setShowEvalModal(false);
          }}
        >
          <form
            onSubmit={handleEvaluateTurn}
            className="glass-card-nested"
            style={{
              width: '520px',
              maxWidth: '90vw',
              padding: '24px',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                Evaluate Turn for Escalation
              </div>
              <Button size="sm" variant="ghost" onClick={() => setShowEvalModal(false)}>
                ✕
              </Button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Conversation UUID:</label>
              <input
                type="text"
                required
                placeholder="UUID of target conversation thread..."
                value={evalConvId}
                onChange={(e) => setEvalConvId(e.target.value)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--surface-glass-input)',
                  border: '1px solid var(--border-card)',
                  color: 'var(--text-primary)',
                  fontSize: '0.75rem',
                  fontFamily: 'var(--font-mono)',
                  outline: 'none',
                }}
              />

              <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Customer Message Content (Optional):</label>
              <textarea
                rows={3}
                placeholder="Simulate customer message to evaluate slope or safety triggers..."
                value={evalContent}
                onChange={(e) => setEvalContent(e.target.value)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--surface-glass-input)',
                  border: '1px solid var(--border-card)',
                  color: 'var(--text-primary)',
                  fontSize: '0.75rem',
                  outline: 'none',
                }}
              />
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '8px' }}>
              <Button size="sm" variant="outline" onClick={() => setShowEvalModal(false)}>
                Cancel
              </Button>
              <Button size="sm" variant="primary" type="submit" loading={isEvaluating}>
                Run Evaluation
              </Button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
};
