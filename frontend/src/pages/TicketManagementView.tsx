import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '../components/common/Card';
import { Badge, type BadgeVariant } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { ClockIcon, AlertTriangleIcon } from '../components/common/Icons';
import {
  ticketsApi,
  type TicketListItem,
  type TicketDetailResponse,
  type PriorityEvaluateResponse,
  type RouteTicketResponse,
  type RoutingHistoryItem,
  type TeamWorkloadItem,
  type EvaluateResolutionResponse,
  type VerifyResolutionResponse,
} from '../services/api';
import { useAuth } from '../context/AuthContext';

type StatusFilter = 'ALL' | 'OPEN' | 'IN_PROGRESS' | 'PENDING_CUSTOMER' | 'ESCALATED' | 'RESOLVED' | 'CLOSED';
type PriorityFilter = 'ALL' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | 'URGENT';
type DetailTab = 'overview' | 'routing' | 'resolution';

export const TicketManagementView: React.FC = () => {
  const { activeSession, currentUser } = useAuth();

  // Role Checks
  const isStaff =
    activeSession?.auth_type === 'JWT' ||
    (currentUser?.roles || activeSession?.roles || []).some((r) =>
      ['AGENT', 'SUPERVISOR', 'ADMIN'].includes(r.toUpperCase())
    );

  const isSupervisorOrAdmin = (currentUser?.roles || activeSession?.roles || []).some((r) =>
    ['SUPERVISOR', 'ADMIN'].includes(r.toUpperCase())
  );

  // Queue State
  const [tickets, setTickets] = useState<TicketListItem[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Filters & Pagination
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>('ALL');
  const [categoryFilter, setCategoryFilter] = useState<string>('ALL');
  const [page, setPage] = useState<number>(0);
  const pageSize = 12;

  // Detail Drawer State
  const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null);
  const [ticketDetail, setTicketDetail] = useState<TicketDetailResponse | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState<boolean>(false);
  const [detailErrorMsg, setDetailErrorMsg] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');

  // Routing & Priority State
  const [priorityResult, setPriorityResult] = useState<PriorityEvaluateResponse | null>(null);
  const [routeResult, setRouteResult] = useState<RouteTicketResponse | null>(null);
  const [routingHistory, setRoutingHistory] = useState<RoutingHistoryItem[]>([]);
  const [isLoadingRoutingHistory, setIsLoadingRoutingHistory] = useState<boolean>(false);
  const [teamWorkloads, setTeamWorkloads] = useState<TeamWorkloadItem[]>([]);
  const [isLoadingWorkload, setIsLoadingWorkload] = useState<boolean>(false);
  const [manualAgentId, setManualAgentId] = useState<string>('');
  const [manualReason, setManualReason] = useState<string>('');
  const [manualForceOverride, setManualForceOverride] = useState<boolean>(false);
  const [routingFeedback, setRoutingFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const [isRoutingPending, setIsRoutingPending] = useState<boolean>(false);

  // Resolution State
  const [resolutionData, setResolutionData] = useState<EvaluateResolutionResponse | null>(null);
  const [isLoadingResolution, setIsLoadingResolution] = useState<boolean>(false);
  const [resolutionHistory, setResolutionHistory] = useState<RoutingHistoryItem[]>([]);
  const [isLoadingResHistory, setIsLoadingResHistory] = useState<boolean>(false);
  const [approvalReason, setApprovalReason] = useState<string>('');
  const [rejectionReason, setRejectionReason] = useState<string>('');
  const [activeActionId, setActiveActionId] = useState<string | null>(null);
  const [actionPromptType, setActionPromptType] = useState<'approve' | 'reject' | 'edit' | null>(null);
  const [editParamsJson, setEditParamsJson] = useState<string>('{}');
  const [verifyResult, setVerifyResult] = useState<VerifyResolutionResponse | null>(null);
  const [resolutionFeedback, setResolutionFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const [isResolutionPending, setIsResolutionPending] = useState<boolean>(false);

  // Load Tickets Queue
  const fetchTickets = useCallback(async () => {
    setIsLoading(true);
    setErrorMsg(null);
    try {
      const params: any = {
        limit: pageSize,
        offset: page * pageSize,
      };
      if (statusFilter !== 'ALL') {
        params.status = statusFilter;
      } else {
        params.exclude_closed = true;
      }
      if (priorityFilter !== 'ALL') params.priority = priorityFilter;
      if (categoryFilter !== 'ALL') params.category = categoryFilter;

      const res = await ticketsApi.listTickets(params);
      setTickets(res.items);
      setTotalCount(res.total_count);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to load tickets from queue.';
      setErrorMsg(msg);
      setTickets([]);
      setTotalCount(0);
    } finally {
      setIsLoading(false);
    }
  }, [page, statusFilter, priorityFilter, categoryFilter]);

  useEffect(() => {
    fetchTickets();
  }, [fetchTickets, activeSession?.token]);

  // Load Ticket Detail
  const fetchTicketDetail = useCallback(async (ticketId: string) => {
    setIsLoadingDetail(true);
    setDetailErrorMsg(null);
    setRoutingFeedback(null);
    setResolutionFeedback(null);
    try {
      const detail = await ticketsApi.getTicket(ticketId);
      setTicketDetail(detail);

      // Load associated routing history
      try {
        setIsLoadingRoutingHistory(true);
        const rh = await ticketsApi.getRoutingHistory(ticketId);
        setRoutingHistory(rh.events);
      } catch {
        setRoutingHistory([]);
      } finally {
        setIsLoadingRoutingHistory(false);
      }

      // Load associated resolution plan
      try {
        setIsLoadingResolution(true);
        const res = await ticketsApi.getResolution(ticketId);
        setResolutionData(res);
      } catch {
        setResolutionData(null);
      } finally {
        setIsLoadingResolution(false);
      }

      // Load resolution history
      try {
        setIsLoadingResHistory(true);
        const resh = await ticketsApi.getResolutionHistory(ticketId);
        setResolutionHistory(resh.events);
      } catch {
        setResolutionHistory([]);
      } finally {
        setIsLoadingResHistory(false);
      }
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to load ticket details.';
      setDetailErrorMsg(msg);
    } finally {
      setIsLoadingDetail(false);
    }
  }, []);

  const handleSelectTicket = (id: string) => {
    setSelectedTicketId(id);
    fetchTicketDetail(id);
  };

  const handleCloseDetail = () => {
    setSelectedTicketId(null);
    setTicketDetail(null);
    setPriorityResult(null);
    setRouteResult(null);
    setResolutionData(null);
    setVerifyResult(null);
    setRoutingFeedback(null);
    setResolutionFeedback(null);
    setActionPromptType(null);
    setActiveActionId(null);
  };

  // Load Team Workload (Supervisors/Admins only)
  const fetchTeamWorkloads = useCallback(async () => {
    if (!isSupervisorOrAdmin) return;
    setIsLoadingWorkload(true);
    try {
      const res = await ticketsApi.getTeamsWorkload();
      setTeamWorkloads(res.teams);
    } catch {
      setTeamWorkloads([]);
    } finally {
      setIsLoadingWorkload(false);
    }
  }, [isSupervisorOrAdmin]);

  useEffect(() => {
    if (activeTab === 'routing' && isSupervisorOrAdmin) {
      fetchTeamWorkloads();
    }
  }, [activeTab, isSupervisorOrAdmin, fetchTeamWorkloads]);

  // Priority Evaluation
  const handleEvaluatePriority = async () => {
    if (!selectedTicketId || isRoutingPending) return;
    setIsRoutingPending(true);
    setRoutingFeedback(null);
    try {
      const res = await ticketsApi.evaluatePriority(selectedTicketId);
      setPriorityResult(res);
      setRoutingFeedback({
        type: 'success',
        message: `Priority evaluated: ${res.priority} (Score: ${res.priority_score.toFixed(2)}). ${res.reason}`,
      });
      await fetchTicketDetail(selectedTicketId);
      fetchTickets();
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to evaluate priority.';
      setRoutingFeedback({ type: 'error', message: msg });
    } finally {
      setIsRoutingPending(false);
    }
  };

  // Intelligent Routing
  const handleAutoRoute = async () => {
    if (!selectedTicketId || isRoutingPending) return;
    setIsRoutingPending(true);
    setRoutingFeedback(null);
    try {
      const res = await ticketsApi.routeTicket(selectedTicketId);
      setRouteResult(res);
      setRoutingFeedback({
        type: 'success',
        message: `Routed to ${res.target_department}${res.assigned_agent_name ? ` (${res.assigned_agent_name})` : ' (Team Queue)'}. ${res.reason}`,
      });
      await fetchTicketDetail(selectedTicketId);
      fetchTickets();
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to route ticket.';
      setRoutingFeedback({ type: 'error', message: msg });
    } finally {
      setIsRoutingPending(false);
    }
  };

  // Manual Assignment
  const handleManualAssign = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedTicketId || isRoutingPending) return;
    if (!manualAgentId.trim() || manualReason.trim().length < 5) {
      setRoutingFeedback({
        type: 'error',
        message: 'Agent ID and valid justification (min 5 characters) are required.',
      });
      return;
    }

    setIsRoutingPending(true);
    setRoutingFeedback(null);
    try {
      const res = await ticketsApi.assignTicket(selectedTicketId, {
        agent_id: manualAgentId.trim(),
        reason: manualReason.trim(),
        force_override: manualForceOverride,
      });
      setRoutingFeedback({
        type: 'success',
        message: `Assigned to ${res.assigned_agent_name} (${res.assigned_team}). Active load: ${res.active_tickets}/${res.max_active_tickets}.`,
      });
      setManualAgentId('');
      setManualReason('');
      setManualForceOverride(false);
      await fetchTicketDetail(selectedTicketId);
      fetchTickets();
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to assign agent.';
      setRoutingFeedback({ type: 'error', message: msg });
    } finally {
      setIsRoutingPending(false);
    }
  };

  // Resolution Evaluation
  const handleEvaluateResolution = async () => {
    if (!selectedTicketId || isResolutionPending) return;
    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      const res = await ticketsApi.evaluateResolution(selectedTicketId);
      setResolutionData(res);
      setResolutionFeedback({
        type: 'success',
        message: `Playbook evaluated: ${res.playbook_category}. Resolution: ${res.recommended_resolution} (Confidence: ${Math.round(res.confidence_score * 100)}%).`,
      });
      await fetchTicketDetail(selectedTicketId);
      fetchTickets();
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to evaluate resolution.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Approve Action
  const handleApproveAction = async (actionId: string) => {
    if (!selectedTicketId || isResolutionPending || !approvalReason.trim()) return;
    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      await ticketsApi.approveAction(selectedTicketId, actionId, approvalReason.trim());
      setResolutionFeedback({
        type: 'success',
        message: 'Action approved successfully.',
      });
      setApprovalReason('');
      setActionPromptType(null);
      setActiveActionId(null);
      await fetchTicketDetail(selectedTicketId);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to approve action.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Reject Action
  const handleRejectAction = async (actionId: string) => {
    if (!selectedTicketId || isResolutionPending || !rejectionReason.trim()) return;
    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      await ticketsApi.rejectAction(selectedTicketId, actionId, rejectionReason.trim());
      setResolutionFeedback({
        type: 'success',
        message: 'Action rejected.',
      });
      setRejectionReason('');
      setActionPromptType(null);
      setActiveActionId(null);
      await fetchTicketDetail(selectedTicketId);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to reject action.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Update Action Params
  const handleUpdateParams = async (actionId: string) => {
    if (!selectedTicketId || isResolutionPending) return;
    let parsed: Record<string, any>;
    try {
      parsed = JSON.parse(editParamsJson);
    } catch {
      setResolutionFeedback({ type: 'error', message: 'Action parameters must be valid JSON.' });
      return;
    }

    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      await ticketsApi.updateActionParams(selectedTicketId, actionId, parsed);
      setResolutionFeedback({
        type: 'success',
        message: 'Parameters updated. Note: Any prior approval was invalidated as required by security policy.',
      });
      setActionPromptType(null);
      setActiveActionId(null);
      await fetchTicketDetail(selectedTicketId);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Failed to update action parameters.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Execute Action
  const handleExecuteAction = async (actionId: string) => {
    if (!selectedTicketId || isResolutionPending) return;
    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      const res = await ticketsApi.executeAction(selectedTicketId, actionId);
      setResolutionFeedback({
        type: 'success',
        message: `Action executed (${res.status}). Attempt #${res.execution_attempts}.`,
      });
      await fetchTicketDetail(selectedTicketId);
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Execution rejected by security or capacity checks.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Verify Resolution
  const handleVerifyResolution = async () => {
    if (!selectedTicketId || isResolutionPending) return;
    setIsResolutionPending(true);
    setResolutionFeedback(null);
    try {
      const res = await ticketsApi.verifyResolution(selectedTicketId);
      setVerifyResult(res);
      setResolutionFeedback({
        type: res.verified ? 'success' : 'error',
        message: res.verified
          ? `Resolution verified! Ticket status: ${res.ticket_status}.`
          : `Resolution verification failed: ${res.failures?.join(', ') || 'Criteria not met'}. Ticket escalated.`,
      });
      await fetchTicketDetail(selectedTicketId);
      fetchTickets();
    } catch (err: any) {
      const msg =
        err?.response?.data?.error?.message ||
        err?.response?.data?.detail ||
        'Verification encountered an error.';
      setResolutionFeedback({ type: 'error', message: msg });
    } finally {
      setIsResolutionPending(false);
    }
  };

  // Priority Badge Variant Helper
  const getPriorityVariant = (priority: string): BadgeVariant => {
    switch (priority.toUpperCase()) {
      case 'CRITICAL':
      case 'URGENT':
        return 'danger';
      case 'HIGH':
        return 'warning';
      case 'MEDIUM':
        return 'cyan';
      default:
        return 'neutral';
    }
  };

  // Status Badge Variant Helper
  const getStatusVariant = (status: string): BadgeVariant => {
    switch (status.toUpperCase()) {
      case 'OPEN':
        return 'primary';
      case 'IN_PROGRESS':
        return 'cyan';
      case 'PENDING_CUSTOMER':
        return 'warning';
      case 'ESCALATED':
        return 'danger';
      case 'RESOLVED':
      case 'CLOSED':
        return 'success';
      default:
        return 'neutral';
    }
  };

  // SLA Calculation Helper
  const calculateSlaDisplay = (t: TicketListItem | TicketDetailResponse) => {
    if (t.status === 'RESOLVED' || t.status === 'CLOSED') {
      return { text: 'Resolved', isPaused: false, isBreached: false, variant: 'success' as BadgeVariant };
    }
    if ((t as any).sla_paused_at || t.status === 'PENDING_CUSTOMER') {
      return { text: 'SLA Paused', isPaused: true, isBreached: false, variant: 'cyan' as BadgeVariant };
    }
    if (t.sla_breached) {
      return { text: 'SLA Breached', isPaused: false, isBreached: true, variant: 'danger' as BadgeVariant };
    }

    try {
      const breachTime = new Date(t.sla_breach_at).getTime();
      const diffMins = Math.floor((breachTime - Date.now()) / 60000);
      if (diffMins <= 0) {
        return { text: 'Breached', isPaused: false, isBreached: true, variant: 'danger' as BadgeVariant };
      }
      if (diffMins < 60) {
        return { text: `${diffMins}m left`, isPaused: false, isBreached: false, variant: 'danger' as BadgeVariant };
      }
      return {
        text: `${Math.floor(diffMins / 60)}h ${diffMins % 60}m`,
        isPaused: false,
        isBreached: false,
        variant: diffMins < 240 ? ('warning' as BadgeVariant) : ('neutral' as BadgeVariant),
      };
    } catch {
      return { text: 'SLA Active', isPaused: false, isBreached: false, variant: 'neutral' as BadgeVariant };
    }
  };

  const totalPages = Math.ceil(totalCount / pageSize);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      {/* =========================================================================
          HEADER & FILTERS
      ========================================================================= */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          flexWrap: 'wrap',
          gap: '16px',
        }}
      >
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <h1
              style={{
                fontSize: '1.625rem',
                fontWeight: 700,
                letterSpacing: '-0.02em',
                color: 'var(--text-primary)',
              }}
            >
              Ticket Command Queue
            </h1>
            <Badge variant="cyan" size="sm">
              Live API
            </Badge>
            <Badge variant="purple" size="sm">
              {totalCount} Total
            </Badge>
          </div>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Authoritative ticket synthesis, explainable priority, capacity-locked routing & audited playbooks.
          </p>
        </div>

        {/* Filter Controls Cluster */}
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center' }}>
          {/* Status Filter Pills */}
          <div
            style={{
              display: 'flex',
              gap: '4px',
              background: 'var(--surface-glass-base)',
              padding: '4px',
              borderRadius: 'var(--radius-md)',
              border: 'var(--border-subtle)',
            }}
          >
            {(['ALL', 'OPEN', 'IN_PROGRESS', 'PENDING_CUSTOMER', 'ESCALATED', 'RESOLVED', 'CLOSED'] as const).map((s) => (
              <button
                key={s}
                onClick={() => {
                  setStatusFilter(s);
                  setPage(0);
                }}
                style={{
                  padding: '6px 12px',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  border: 'none',
                  cursor: 'pointer',
                  background: statusFilter === s ? 'var(--accent-primary)' : 'transparent',
                  color: statusFilter === s ? '#ffffff' : 'var(--text-secondary)',
                  transition: 'all var(--transition-fast)',
                }}
              >
                {s.replace('_', ' ')}
              </button>
            ))}
          </div>

          {/* Priority Select */}
          <select
            value={priorityFilter}
            onChange={(e) => {
              setPriorityFilter(e.target.value as PriorityFilter);
              setPage(0);
            }}
            style={{
              padding: '6px 10px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface-glass-input)',
              border: 'var(--border-card)',
              color: 'var(--text-primary)',
              fontSize: '0.75rem',
              outline: 'none',
            }}
            title="Filter by Priority"
          >
            <option value="ALL">All Priorities</option>
            <option value="LOW">Low</option>
            <option value="MEDIUM">Medium</option>
            <option value="HIGH">High</option>
            <option value="CRITICAL">Critical</option>
            <option value="URGENT">Urgent</option>
          </select>

          {/* Refresh Button */}
          <Button
            size="sm"
            variant="outline"
            onClick={() => fetchTickets()}
            loading={isLoading}
            style={{ fontSize: '0.75rem', padding: '6px 12px' }}
          >
            ↻ Refresh
          </Button>
        </div>
      </div>

      {/* Error Alert Banner */}
      {errorMsg && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--accent-rose-light)',
            border: '1px solid rgba(244, 63, 94, 0.3)',
            color: 'var(--accent-rose)',
            fontSize: '0.8125rem',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div>
            <strong>Error Loading Queue:</strong> {errorMsg}
          </div>
          <Button size="sm" variant="outline" onClick={() => fetchTickets()}>
            Retry
          </Button>
        </div>
      )}

      {/* =========================================================================
          TICKETS QUEUE GRID
      ========================================================================= */}
      {isLoading && tickets.length === 0 ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
            gap: '20px',
          }}
        >
          {[1, 2, 3, 4, 5, 6].map((n) => (
            <Card key={n} variant="base" style={{ height: '180px', opacity: 0.6 }}>
              <div style={{ height: '100%', background: 'var(--surface-glass-card)', borderRadius: 'var(--radius-md)' }} />
            </Card>
          ))}
        </div>
      ) : tickets.length === 0 ? (
        <Card variant="base" style={{ padding: '48px', textAlign: 'center', color: 'var(--text-muted)' }}>
          <div style={{ fontSize: '1.125rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '6px' }}>
            No Tickets Found
          </div>
          <div style={{ fontSize: '0.875rem' }}>
            No tickets match the current filter selection ({statusFilter} / {priorityFilter}).
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setStatusFilter('ALL');
              setPriorityFilter('ALL');
              setCategoryFilter('ALL');
              setPage(0);
            }}
            style={{ marginTop: '16px' }}
          >
            Clear Filters
          </Button>
        </Card>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
            gap: '20px',
          }}
        >
          {tickets.map((t) => {
            const sla = calculateSlaDisplay(t);
            const isSelected = t.id === selectedTicketId;

            return (
              <Card
                key={t.id}
                variant="base"
                onClick={() => handleSelectTicket(t.id)}
                style={{
                  cursor: 'pointer',
                  border: isSelected ? '1px solid var(--accent-primary)' : 'var(--border-card)',
                  background: isSelected ? 'var(--accent-primary-light)' : undefined,
                  transition: 'all var(--transition-fast)',
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'space-between',
                }}
              >
                <div>
                  {/* Top Meta Row */}
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'flex-start',
                      marginBottom: '10px',
                    }}
                  >
                    <div>
                      <span
                        style={{
                          fontSize: '0.75rem',
                          fontFamily: 'var(--font-mono)',
                          fontWeight: 700,
                          color: 'var(--accent-primary)',
                        }}
                      >
                        #{t.ticket_number}
                      </span>
                      <span style={{ margin: '0 6px', color: 'var(--text-muted)' }}>•</span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'capitalize' }}>
                        {t.category}
                      </span>
                    </div>

                    <Badge variant={getPriorityVariant(t.priority)} size="sm">
                      {t.priority} ({t.priority_score.toFixed(2)})
                    </Badge>
                  </div>

                  {/* Title */}
                  <h3
                    style={{
                      fontSize: '0.9375rem',
                      fontWeight: 600,
                      color: 'var(--text-primary)',
                      marginBottom: '6px',
                      lineHeight: 1.4,
                    }}
                  >
                    {t.title}
                  </h3>

                  {/* Summary Snippet */}
                  <div
                    style={{
                      fontSize: '0.75rem',
                      color: 'var(--text-secondary)',
                      lineHeight: 1.4,
                      marginBottom: '12px',
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden',
                    }}
                  >
                    {t.masked_summary || t.description}
                  </div>
                </div>

                <div>
                  {/* Mid Status / SLA Box */}
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '8px 10px',
                      borderRadius: 'var(--radius-sm)',
                      background: 'var(--surface-glass-base)',
                      marginBottom: '12px',
                    }}
                  >
                    <div>
                      <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Assigned Team</div>
                      <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                        {t.assigned_team || 'Unassigned'}
                      </div>
                    </div>

                    <div style={{ textAlign: 'right' }}>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '4px',
                          fontSize: '0.75rem',
                          fontWeight: 700,
                          color: sla.isBreached
                            ? 'var(--accent-rose)'
                            : sla.isPaused
                            ? 'var(--accent-cyan)'
                            : 'var(--text-primary)',
                        }}
                      >
                        <ClockIcon size={12} />
                        <span>{sla.text}</span>
                      </div>
                      <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                        Target: {t.sla_target_minutes}m
                      </div>
                    </div>
                  </div>

                  {/* Card Footer */}
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      borderTop: 'var(--border-subtle)',
                      paddingTop: '10px',
                    }}
                  >
                    <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                      <Badge variant={getStatusVariant(t.status)} size="sm">
                        {t.status}
                      </Badge>
                      <Badge variant="neutral" size="sm">
                        {t.routing_status}
                      </Badge>
                    </div>

                    <span style={{ fontSize: '0.75rem', color: 'var(--accent-primary)', fontWeight: 600 }}>
                      Inspect →
                    </span>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {/* =========================================================================
          PAGINATION CONTROLS
      ========================================================================= */}
      {totalCount > pageSize && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '12px 16px',
            background: 'var(--surface-glass-base)',
            borderRadius: 'var(--radius-md)',
            border: 'var(--border-subtle)',
          }}
        >
          <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
            Showing <strong>{page * pageSize + 1}</strong> –{' '}
            <strong>{Math.min((page + 1) * pageSize, totalCount)}</strong> of <strong>{totalCount}</strong> tickets
          </div>

          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <Button
              size="sm"
              variant="outline"
              disabled={page === 0 || isLoading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </Button>
            <span style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>
              Page {page + 1} of {Math.max(1, totalPages)}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={(page + 1) * pageSize >= totalCount || isLoading}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}

      {/* =========================================================================
          DETAIL & WORKFLOW INSPECTOR DRAWER (SLIDE-OVER)
      ========================================================================= */}
      {selectedTicketId && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 1000,
            display: 'flex',
            justifyContent: 'flex-end',
            background: 'rgba(0, 0, 0, 0.55)',
            backdropFilter: 'blur(4px)',
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) handleCloseDetail();
          }}
        >
          <div
            style={{
              width: '680px',
              maxWidth: '92vw',
              height: '100%',
              background: 'var(--surface-glass-card)',
              backdropFilter: 'blur(28px)',
              borderLeft: '1px solid rgba(255, 255, 255, 0.1)',
              boxShadow: 'var(--shadow-xl)',
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            }}
          >
            {/* Drawer Header */}
            <div
              style={{
                padding: '20px 24px',
                borderBottom: 'var(--border-subtle)',
                background: 'var(--surface-glass-base)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'flex-start',
                gap: '12px',
              }}
            >
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: '0.875rem',
                      fontWeight: 700,
                      color: 'var(--accent-primary)',
                    }}
                  >
                    #{ticketDetail?.ticket_number || 'TICKET'}
                  </span>
                  {ticketDetail && (
                    <>
                      <Badge variant={getStatusVariant(ticketDetail.status)} size="sm">
                        {ticketDetail.status}
                      </Badge>
                      <Badge variant={getPriorityVariant(ticketDetail.priority)} size="sm">
                        {ticketDetail.priority}
                      </Badge>
                    </>
                  )}
                </div>
                <h2 style={{ fontSize: '1.125rem', fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.3 }}>
                  {ticketDetail?.title || 'Loading Ticket Details...'}
                </h2>
              </div>

              <Button size="sm" variant="ghost" onClick={handleCloseDetail} style={{ padding: '4px 8px' }}>
                ✕
              </Button>
            </div>

            {/* Drawer Tabs */}
            <div
              style={{
                display: 'flex',
                borderBottom: 'var(--border-subtle)',
                background: 'var(--surface-glass-base)',
                padding: '0 24px',
                gap: '8px',
              }}
            >
              {[
                { id: 'overview', label: 'Overview & SLA' },
                { id: 'routing', label: 'Routing & Workload' },
                { id: 'resolution', label: 'Resolution Playbook' },
              ].map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id as DetailTab)}
                  style={{
                    padding: '12px 14px',
                    fontSize: '0.8125rem',
                    fontWeight: 600,
                    border: 'none',
                    borderBottom: activeTab === tab.id ? '2px solid var(--accent-primary)' : '2px solid transparent',
                    background: 'transparent',
                    color: activeTab === tab.id ? 'var(--accent-primary)' : 'var(--text-secondary)',
                    cursor: 'pointer',
                    transition: 'all var(--transition-fast)',
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Drawer Body */}
            <div style={{ flex: 1, overflowY: 'auto', padding: '24px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
              {isLoadingDetail ? (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
                  Loading authoritative ticket data...
                </div>
              ) : detailErrorMsg ? (
                <div
                  style={{
                    padding: '12px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--accent-rose-light)',
                    color: 'var(--accent-rose)',
                    fontSize: '0.8125rem',
                  }}
                >
                  {detailErrorMsg}
                </div>
              ) : !ticketDetail ? (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
                  Ticket not found.
                </div>
              ) : (
                <>
                  {/* -------------------------------------------------------------
                      TAB 1: OVERVIEW & SLA
                  ------------------------------------------------------------- */}
                  {activeTab === 'overview' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
                      {/* Masked Summary Card */}
                      <div
                        style={{
                          padding: '14px 16px',
                          borderRadius: 'var(--radius-md)',
                          background: 'var(--surface-glass-base)',
                          border: 'var(--border-subtle)',
                        }}
                      >
                        <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '6px' }}>
                          MASKED SUMMARY (ZERO-LOGGING SANITIZED)
                        </div>
                        <div style={{ fontSize: '0.8125rem', color: 'var(--text-primary)', lineHeight: 1.5 }}>
                          {ticketDetail.masked_summary || 'No summary available.'}
                        </div>
                      </div>

                      {/* Description Card */}
                      <div>
                        <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '6px' }}>
                          ORIGINAL INTAKE DESCRIPTION
                        </div>
                        <div
                          style={{
                            fontSize: '0.8125rem',
                            color: 'var(--text-secondary)',
                            lineHeight: 1.5,
                            background: 'var(--surface-glass-base)',
                            padding: '12px',
                            borderRadius: 'var(--radius-sm)',
                          }}
                        >
                          {ticketDetail.description}
                        </div>
                      </div>

                      {/* Metadata Grid */}
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div style={{ padding: '10px', background: 'var(--surface-glass-base)', borderRadius: 'var(--radius-sm)' }}>
                          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Customer ID:</span>
                          <div style={{ fontSize: '0.75rem', fontFamily: 'var(--font-mono)', color: 'var(--accent-cyan)' }}>
                            {ticketDetail.customer_id}
                          </div>
                        </div>
                        <div style={{ padding: '10px', background: 'var(--surface-glass-base)', borderRadius: 'var(--radius-sm)' }}>
                          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Category:</span>
                          <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                            {ticketDetail.category}
                          </div>
                        </div>
                        <div style={{ padding: '10px', background: 'var(--surface-glass-base)', borderRadius: 'var(--radius-sm)' }}>
                          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Synthesis Status:</span>
                          <div>
                            <Badge variant="neutral" size="sm">
                              {ticketDetail.synthesis_status}
                            </Badge>
                          </div>
                        </div>
                        <div style={{ padding: '10px', background: 'var(--surface-glass-base)', borderRadius: 'var(--radius-sm)' }}>
                          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Linked Conversation:</span>
                          <div style={{ fontSize: '0.75rem', fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
                            {ticketDetail.conversation_id ? ticketDetail.conversation_id.substring(0, 12) + '...' : 'None'}
                          </div>
                        </div>
                      </div>

                      {/* SLA Compliance Box */}
                      <div
                        style={{
                          padding: '14px 16px',
                          borderRadius: 'var(--radius-md)',
                          background: 'var(--surface-glass-base)',
                          border: 'var(--border-subtle)',
                        }}
                      >
                        <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                          CALENDAR-AWARE SLA STATUS
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '0.75rem' }}>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Target Window: </span>
                            <span style={{ fontWeight: 600 }}>{ticketDetail.sla_target_minutes} minutes</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>SLA Breached: </span>
                            <Badge variant={ticketDetail.sla_breached ? 'danger' : 'success'} size="sm">
                              {ticketDetail.sla_breached ? 'YES (Breached)' : 'NO (Compliant)'}
                            </Badge>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Breach At: </span>
                            <span style={{ fontFamily: 'var(--font-mono)' }}>
                              {new Date(ticketDetail.sla_breach_at).toLocaleString()}
                            </span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Total Paused Duration: </span>
                            <span>{Math.round(ticketDetail.total_paused_duration_seconds / 60)} minutes</span>
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* -------------------------------------------------------------
                      TAB 2: ROUTING & WORKLOAD
                  ------------------------------------------------------------- */}
                  {activeTab === 'routing' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
                      {routingFeedback && (
                        <div
                          style={{
                            padding: '10px 14px',
                            borderRadius: 'var(--radius-sm)',
                            background: routingFeedback.type === 'success' ? 'var(--accent-emerald-light)' : 'var(--accent-rose-light)',
                            color: routingFeedback.type === 'success' ? 'var(--accent-emerald)' : 'var(--accent-rose)',
                            fontSize: '0.75rem',
                          }}
                        >
                          {routingFeedback.message}
                        </div>
                      )}

                      {/* Priority Evaluation Section */}
                      <div
                        style={{
                          padding: '14px 16px',
                          borderRadius: 'var(--radius-md)',
                          background: 'var(--surface-glass-base)',
                          border: 'var(--border-subtle)',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)' }}>
                            PRIORITY SCORE & EXPLANATION
                          </div>
                          {isStaff && (
                            <Button
                              size="sm"
                              variant="outline"
                              loading={isRoutingPending}
                              onClick={handleEvaluatePriority}
                              style={{ fontSize: '0.6875rem', padding: '4px 8px' }}
                            >
                              ⚡ Re-Evaluate Priority
                            </Button>
                          )}
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                          <Badge variant={getPriorityVariant(ticketDetail.priority)} size="md">
                            {ticketDetail.priority} ({ticketDetail.priority_score.toFixed(2)})
                          </Badge>
                        </div>

                        <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                          {ticketDetail.priority_explanation || priorityResult?.reason || 'Priority not yet explicitly calculated.'}
                        </div>

                        {priorityResult?.supporting_facts && priorityResult.supporting_facts.length > 0 && (
                          <div style={{ marginTop: '8px', paddingTop: '8px', borderTop: 'var(--border-subtle)' }}>
                            <div style={{ fontSize: '0.6875rem', fontWeight: 600, color: 'var(--text-muted)' }}>
                              Supporting Facts:
                            </div>
                            <ul style={{ margin: '4px 0 0 16px', padding: 0, fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>
                              {priorityResult.supporting_facts.map((fact, idx) => (
                                <li key={idx}>{fact}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>

                      {/* Intelligent Auto-Routing Section */}
                      <div
                        style={{
                          padding: '14px 16px',
                          borderRadius: 'var(--radius-md)',
                          background: 'var(--surface-glass-base)',
                          border: 'var(--border-subtle)',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)' }}>
                            INTELLIGENT ROUTING ENGINE
                          </div>
                          {isStaff && (
                            <Button
                              size="sm"
                              variant="primary"
                              loading={isRoutingPending}
                              onClick={handleAutoRoute}
                              style={{ fontSize: '0.6875rem', padding: '4px 10px' }}
                            >
                              Route Ticket
                            </Button>
                          )}
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '0.75rem', marginBottom: '8px' }}>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Routing Status: </span>
                            <Badge variant="cyan" size="sm">
                              {ticketDetail.routing_status}
                            </Badge>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Assigned Team: </span>
                            <span style={{ fontWeight: 600 }}>{ticketDetail.assigned_team || 'None'}</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Assigned Agent: </span>
                            <span style={{ fontFamily: 'var(--font-mono)' }}>
                              {ticketDetail.assigned_agent_id ? ticketDetail.assigned_agent_id.substring(0, 8) + '...' : 'None'}
                            </span>
                          </div>
                        </div>

                        {routeResult && (
                          <div style={{ marginTop: '8px', padding: '8px', background: 'var(--surface-glass-card)', borderRadius: 'var(--radius-sm)', fontSize: '0.6875rem' }}>
                            <div style={{ fontWeight: 600, color: 'var(--accent-cyan)' }}>Routing Decision:</div>
                            <div>{routeResult.reason}</div>
                            <div style={{ color: 'var(--text-muted)', marginTop: '2px' }}>
                              Evaluated {routeResult.candidates_evaluated} candidates.
                            </div>
                          </div>
                        )}
                      </div>

                      {/* Manual Assignment Override (Staff Only) */}
                      {isStaff && (
                        <form
                          onSubmit={handleManualAssign}
                          style={{
                            padding: '14px 16px',
                            borderRadius: 'var(--radius-md)',
                            background: 'var(--surface-glass-base)',
                            border: 'var(--border-subtle)',
                          }}
                        >
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                            MANUAL ASSIGNMENT (CAPACITY-CHECKED)
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                            <input
                              type="text"
                              placeholder="Agent User UUID..."
                              value={manualAgentId}
                              onChange={(e) => setManualAgentId(e.target.value)}
                              style={{
                                padding: '6px 8px',
                                fontSize: '0.75rem',
                                borderRadius: 'var(--radius-sm)',
                                background: 'var(--surface-glass-input)',
                                border: 'var(--border-card)',
                                color: 'var(--text-primary)',
                                outline: 'none',
                              }}
                            />
                            <input
                              type="text"
                              placeholder="Auditable Reason (min 5 characters)..."
                              value={manualReason}
                              onChange={(e) => setManualReason(e.target.value)}
                              style={{
                                padding: '6px 8px',
                                fontSize: '0.75rem',
                                borderRadius: 'var(--radius-sm)',
                                background: 'var(--surface-glass-input)',
                                border: 'var(--border-card)',
                                color: 'var(--text-primary)',
                                outline: 'none',
                              }}
                            />
                            {isSupervisorOrAdmin && (
                              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                                <input
                                  type="checkbox"
                                  checked={manualForceOverride}
                                  onChange={(e) => setManualForceOverride(e.target.checked)}
                                />
                                <span>Force Capacity Override (Supervisor/Admin Only)</span>
                              </label>
                            )}
                            <Button
                              type="submit"
                              size="sm"
                              variant="secondary"
                              loading={isRoutingPending}
                              style={{ fontSize: '0.75rem', alignSelf: 'flex-start' }}
                            >
                              Assign Agent
                            </Button>
                          </div>
                        </form>
                      )}

                      {/* Team Workload Breakdown (Supervisors/Admins) */}
                      {isSupervisorOrAdmin && (
                        <div>
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                            DEPARTMENT WORKLOAD & CAPACITY METRICS
                          </div>
                          {isLoadingWorkload ? (
                            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Loading department workloads...</div>
                          ) : teamWorkloads.length > 0 ? (
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '8px' }}>
                              {teamWorkloads.map((team, idx) => (
                                <div
                                  key={idx}
                                  style={{
                                    padding: '8px 10px',
                                    borderRadius: 'var(--radius-sm)',
                                    background: 'var(--surface-glass-base)',
                                    border: 'var(--border-subtle)',
                                  }}
                                >
                                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                                    {team.department}
                                  </div>
                                  <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                                    Active Tickets: {team.active_tickets} / {team.total_capacity}
                                  </div>
                                  <div style={{ fontSize: '0.6875rem', fontWeight: 600, color: team.utilization_pct > 80 ? 'var(--accent-rose)' : 'var(--accent-cyan)' }}>
                                    Utilization: {team.utilization_pct.toFixed(1)}%
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      )}

                      {/* Routing Audit Trail */}
                      <div>
                        <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                          IMMUTABLE ROUTING AUDIT TRAIL
                        </div>
                        {isLoadingRoutingHistory ? (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Loading history...</div>
                        ) : routingHistory.length === 0 ? (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                            No routing events recorded yet.
                          </div>
                        ) : (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            {routingHistory.map((evt, idx) => (
                              <div
                                key={idx}
                                style={{
                                  padding: '6px 10px',
                                  borderRadius: 'var(--radius-sm)',
                                  background: 'var(--surface-glass-base)',
                                  border: 'var(--border-subtle)',
                                  fontSize: '0.6875rem',
                                }}
                              >
                                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                                  <span style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>{evt.event_type}</span>
                                  <span style={{ color: 'var(--text-muted)' }}>
                                    {evt.created_at ? new Date(evt.created_at).toLocaleTimeString() : ''}
                                  </span>
                                </div>
                                <div style={{ color: 'var(--text-secondary)', marginTop: '2px' }}>
                                  {JSON.stringify(evt.event_payload)}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {/* -------------------------------------------------------------
                      TAB 3: RESOLUTION PLAYBOOK
                  ------------------------------------------------------------- */}
                  {activeTab === 'resolution' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
                      {resolutionFeedback && (
                        <div
                          style={{
                            padding: '10px 14px',
                            borderRadius: 'var(--radius-sm)',
                            background: resolutionFeedback.type === 'success' ? 'var(--accent-emerald-light)' : 'var(--accent-rose-light)',
                            color: resolutionFeedback.type === 'success' ? 'var(--accent-emerald)' : 'var(--accent-rose)',
                            fontSize: '0.75rem',
                          }}
                        >
                          {resolutionFeedback.message}
                        </div>
                      )}

                      {/* Evaluate Plan Button / Status */}
                      <div
                        style={{
                          padding: '14px 16px',
                          borderRadius: 'var(--radius-md)',
                          background: 'var(--surface-glass-base)',
                          border: 'var(--border-subtle)',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)' }}>
                            RESOLUTION PLAYBOOK ORCHESTRATION
                          </div>
                          {isStaff && (
                            <Button
                              size="sm"
                              variant="primary"
                              loading={isResolutionPending}
                              onClick={handleEvaluateResolution}
                              style={{ fontSize: '0.6875rem', padding: '4px 10px' }}
                            >
                              ⚡ Evaluate Playbook
                            </Button>
                          )}
                        </div>

                        {isLoadingResolution ? (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Loading resolution playbook...</div>
                        ) : resolutionData ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '0.75rem' }}>
                            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                              <Badge variant="cyan" size="sm">
                                {resolutionData.playbook_category}
                              </Badge>
                              <Badge variant={resolutionData.eligibility_decision === 'ELIGIBLE' ? 'success' : 'warning'} size="sm">
                                {resolutionData.eligibility_decision}
                              </Badge>
                              <span style={{ color: 'var(--text-muted)' }}>
                                Confidence: {Math.round(resolutionData.confidence_score * 100)}%
                              </span>
                            </div>

                            <div>
                              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Recommendation: </span>
                              <span style={{ color: 'var(--text-secondary)' }}>{resolutionData.recommended_resolution}</span>
                            </div>

                            {resolutionData.requires_human_approval && (
                              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--accent-amber)', fontWeight: 600 }}>
                                <AlertTriangleIcon size={14} />
                                <span>Requires Supervisor / Senior Approval for Sensitive Actions</span>
                              </div>
                            )}

                            {resolutionData.supporting_evidence && resolutionData.supporting_evidence.length > 0 && (
                              <div>
                                <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Supporting Evidence: </span>
                                <span style={{ color: 'var(--text-secondary)' }}>{resolutionData.supporting_evidence.join(', ')}</span>
                              </div>
                            )}
                          </div>
                        ) : (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                            Resolution has not been evaluated yet. Click "Evaluate Playbook" to generate actions.
                          </div>
                        )}
                      </div>

                      {/* Proposed Actions List */}
                      {resolutionData && resolutionData.actions && resolutionData.actions.length > 0 && (
                        <div>
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                            RESOLUTION ACTIONS ({resolutionData.actions.length})
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                            {resolutionData.actions.map((act) => {
                              const isApproved = act.status === 'APPROVED';
                              const isExecuted = act.status === 'EXECUTED';
                              const canExecute = !act.is_sensitive || isApproved;

                              return (
                                <div
                                  key={act.action_id}
                                  style={{
                                    padding: '12px',
                                    borderRadius: 'var(--radius-md)',
                                    background: 'var(--surface-glass-base)',
                                    border: 'var(--border-subtle)',
                                    display: 'flex',
                                    flexDirection: 'column',
                                    gap: '8px',
                                  }}
                                >
                                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                      <span style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                                        {act.action_type}
                                      </span>
                                      <span style={{ fontSize: '0.6875rem', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                                        ({act.action_key})
                                      </span>
                                    </div>
                                    <div style={{ display: 'flex', gap: '4px' }}>
                                      <Badge variant={act.status === 'EXECUTED' ? 'success' : act.status === 'APPROVED' ? 'cyan' : act.status === 'REJECTED' ? 'danger' : 'neutral'} size="sm">
                                        {act.status}
                                      </Badge>
                                      {act.is_sensitive && (
                                        <Badge variant="warning" size="sm">
                                          Sensitive
                                        </Badge>
                                      )}
                                    </div>
                                  </div>

                                  {/* Action Params */}
                                  <div style={{ fontSize: '0.75rem', background: 'rgba(0,0,0,0.2)', padding: '6px 8px', borderRadius: 'var(--radius-sm)', fontFamily: 'var(--font-mono)' }}>
                                    {JSON.stringify(act.action_params)}
                                  </div>

                                  {/* Action Controls */}
                                  {isStaff && (
                                    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '4px' }}>
                                      {isSupervisorOrAdmin && act.status === 'PROPOSED' && (
                                        <>
                                          <Button
                                            size="sm"
                                            variant="secondary"
                                            onClick={() => {
                                              setActiveActionId(act.action_id);
                                              setActionPromptType('approve');
                                            }}
                                            style={{ fontSize: '0.6875rem', padding: '3px 8px' }}
                                          >
                                            ✓ Approve
                                          </Button>
                                          <Button
                                            size="sm"
                                            variant="outline"
                                            onClick={() => {
                                              setActiveActionId(act.action_id);
                                              setActionPromptType('reject');
                                            }}
                                            style={{ fontSize: '0.6875rem', padding: '3px 8px' }}
                                          >
                                            ✕ Reject
                                          </Button>
                                        </>
                                      )}

                                      {act.status !== 'EXECUTED' && (
                                        <Button
                                          size="sm"
                                          variant="ghost"
                                          onClick={() => {
                                            setActiveActionId(act.action_id);
                                            setActionPromptType('edit');
                                            setEditParamsJson(JSON.stringify(act.action_params, null, 2));
                                          }}
                                          style={{ fontSize: '0.6875rem', padding: '3px 8px' }}
                                        >
                                          ✎ Edit Params
                                        </Button>
                                      )}

                                      {!isExecuted && (
                                        <Button
                                          size="sm"
                                          variant={canExecute ? 'primary' : 'outline'}
                                          disabled={!canExecute || isResolutionPending}
                                          onClick={() => handleExecuteAction(act.action_id)}
                                          style={{ fontSize: '0.6875rem', padding: '3px 8px' }}
                                        >
                                          ▶ Execute
                                        </Button>
                                      )}
                                    </div>
                                  )}

                                  {/* Inline Prompt for Approve / Reject / Edit */}
                                  {activeActionId === act.action_id && actionPromptType && (
                                    <div style={{ marginTop: '6px', padding: '8px', background: 'var(--surface-glass-card)', borderRadius: 'var(--radius-sm)' }}>
                                      {actionPromptType === 'approve' && (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                          <input
                                            type="text"
                                            placeholder="Approval justification reason..."
                                            value={approvalReason}
                                            onChange={(e) => setApprovalReason(e.target.value)}
                                            style={{ padding: '4px 6px', fontSize: '0.75rem', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-input)', border: 'var(--border-card)', color: 'var(--text-primary)' }}
                                          />
                                          <div style={{ display: 'flex', gap: '6px' }}>
                                            <Button size="sm" variant="primary" onClick={() => handleApproveAction(act.action_id)}>Confirm Approval</Button>
                                            <Button size="sm" variant="ghost" onClick={() => setActionPromptType(null)}>Cancel</Button>
                                          </div>
                                        </div>
                                      )}

                                      {actionPromptType === 'reject' && (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                          <input
                                            type="text"
                                            placeholder="Rejection justification reason..."
                                            value={rejectionReason}
                                            onChange={(e) => setRejectionReason(e.target.value)}
                                            style={{ padding: '4px 6px', fontSize: '0.75rem', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-input)', border: 'var(--border-card)', color: 'var(--text-primary)' }}
                                          />
                                          <div style={{ display: 'flex', gap: '6px' }}>
                                            <Button size="sm" variant="danger" onClick={() => handleRejectAction(act.action_id)}>Confirm Rejection</Button>
                                            <Button size="sm" variant="ghost" onClick={() => setActionPromptType(null)}>Cancel</Button>
                                          </div>
                                        </div>
                                      )}

                                      {actionPromptType === 'edit' && (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                          <div style={{ fontSize: '0.6875rem', color: 'var(--accent-amber)' }}>
                                            Warning: Modifying parameters will invalidate any prior approvals.
                                          </div>
                                          <textarea
                                            rows={3}
                                            value={editParamsJson}
                                            onChange={(e) => setEditParamsJson(e.target.value)}
                                            style={{ padding: '4px 6px', fontSize: '0.6875rem', fontFamily: 'var(--font-mono)', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-input)', border: 'var(--border-card)', color: 'var(--text-primary)' }}
                                          />
                                          <div style={{ display: 'flex', gap: '6px' }}>
                                            <Button size="sm" variant="primary" onClick={() => handleUpdateParams(act.action_id)}>Save Parameters</Button>
                                            <Button size="sm" variant="ghost" onClick={() => setActionPromptType(null)}>Cancel</Button>
                                          </div>
                                        </div>
                                      )}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                      {/* Verify Resolution Completion */}
                      {isStaff && resolutionData && (
                        <div
                          style={{
                            padding: '14px 16px',
                            borderRadius: 'var(--radius-md)',
                            background: 'var(--surface-glass-base)',
                            border: 'var(--border-subtle)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '8px',
                          }}
                        >
                          <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)' }}>
                            FINALIZE & VERIFY RESOLUTION
                          </div>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                            Verifies that all playbook conditions and required actions have been safely satisfied.
                            Successful verification transitions ticket to <strong>RESOLVED</strong>.
                          </div>
                          <Button
                            size="sm"
                            variant="primary"
                            loading={isResolutionPending}
                            onClick={handleVerifyResolution}
                            style={{ alignSelf: 'flex-start', fontSize: '0.75rem' }}
                          >
                            ✓ Verify & Finalize Resolution
                          </Button>

                          {verifyResult && (
                            <div style={{ marginTop: '8px', padding: '8px 10px', background: 'var(--surface-glass-card)', borderRadius: 'var(--radius-sm)', fontSize: '0.75rem' }}>
                              <div>Verified: <strong>{verifyResult.verified ? 'YES (Playbook Satisfied)' : 'NO (Unverified)'}</strong></div>
                              {verifyResult.failures && verifyResult.failures.length > 0 && (
                                <div style={{ color: 'var(--accent-rose)', marginTop: '4px' }}>
                                  Failures: {verifyResult.failures.join(', ')}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}

                      {/* Resolution Audit Trail */}
                      <div>
                        <div style={{ fontSize: '0.6875rem', fontWeight: 700, color: 'var(--text-muted)', marginBottom: '8px' }}>
                          IMMUTABLE RESOLUTION AUDIT TRAIL
                        </div>
                        {isLoadingResHistory ? (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Loading history...</div>
                        ) : resolutionHistory.length === 0 ? (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                            No resolution events recorded yet.
                          </div>
                        ) : (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            {resolutionHistory.map((evt, idx) => (
                              <div
                                key={idx}
                                style={{
                                  padding: '6px 10px',
                                  borderRadius: 'var(--radius-sm)',
                                  background: 'var(--surface-glass-base)',
                                  border: 'var(--border-subtle)',
                                  fontSize: '0.6875rem',
                                }}
                              >
                                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                                  <span style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>{evt.event_type}</span>
                                  <span style={{ color: 'var(--text-muted)' }}>
                                    {evt.created_at ? new Date(evt.created_at).toLocaleTimeString() : ''}
                                  </span>
                                </div>
                                <div style={{ color: 'var(--text-secondary)', marginTop: '2px' }}>
                                  {JSON.stringify(evt.event_payload)}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
