import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import {
  ShieldCheckIcon,
  AlertTriangleIcon,
  ClockIcon,
  CheckCircleIcon,
} from '../components/common/Icons';
import {
  analyticsApi,
  type OverviewDashboardResponse,
  type DashboardIncidentItem,
} from '../services/api';
import { useAuth } from '../context/AuthContext';

export interface IncidentQueueItem {
  id: string;
  ticketId: string;
  title: string;
  customer: string;
  tier: string;
  category: string;
  priority: 'CRITICAL' | 'HIGH' | 'MEDIUM';
  severity: string;
  status: string;
  assignedTeam: string;
  priorityScore: number;
  slaElapsed: string;
  slaTotal: string;
  slaRemaining: string;
  progress: number; // 0 to 100% of SLA consumed
  sentimentScore: number;
  sentimentLabel: string;
  latestActivity: string;
  evidenceStatus: string;
  evidenceSnippet: string;
  evidenceDoc: string;
  suggestedAction: string;
  isBreached?: boolean;
  isOverdue?: boolean;
  overdueDuration?: string;
  slaTargetMinutes?: number;
  slaDeadline?: string;
}

const mapIncidentItem = (item: DashboardIncidentItem): IncidentQueueItem => ({
  id: item.id || `inc-${item.ticket_id?.slice(0, 8)}`,
  ticketId: item.ticket_number || item.ticket_id || 'TCK-UNKNOWN',
  title: item.title,
  customer: item.customer || 'Unknown Customer',
  tier: item.tier || 'STANDARD',
  category: item.category || 'General',
  priority: (item.priority as any) || 'MEDIUM',
  severity: item.severity || `SEV-2 ${item.priority || 'MEDIUM'}`,
  status: item.status || 'OPEN',
  assignedTeam: item.assigned_team || 'General Support',
  priorityScore: item.priority_score ?? 2.5,
  slaElapsed: item.sla_elapsed || '0m elapsed',
  slaTotal: item.sla_total || '60m window',
  slaRemaining: item.sla_remaining || '60m remaining',
  progress: item.progress ?? 0,
  sentimentScore: item.sentiment_score ?? 0,
  sentimentLabel: item.sentiment_label || 'Neutral Ingestion',
  latestActivity: item.latest_activity || 'Automated ticket telemetry processed.',
  evidenceStatus: item.evidence_status || 'Standard Ingestion',
  evidenceSnippet: item.evidence_snippet || item.title,
  evidenceDoc: item.evidence_doc || 'ticket-data.json',
  suggestedAction: item.suggested_action || 'Review Queue',
  isBreached: item.is_breached ?? (item.sla_remaining?.includes('BREACHED') || (item.progress ?? 0) >= 100),
  isOverdue: item.is_overdue ?? (item.sla_remaining?.includes('BREACHED') || (item.progress ?? 0) >= 100),
  overdueDuration: item.overdue_duration,
  slaTargetMinutes: item.sla_target_minutes,
  slaDeadline: item.sla_deadline,
});

export const OverviewDashboard: React.FC = () => {
  const { activeSession } = useAuth();
  const [activeIndex, setActiveIndex] = useState(0);
  const [acknowledgedCases, setAcknowledgedCases] = useState<Record<string, boolean>>({});
  const [escalatedCases, setEscalatedCases] = useState<Record<string, boolean>>({});

  // Live Telemetry & Filter State
  const [timeWindow, setTimeWindow] = useState<'today' | '7d' | '30d' | '90d'>('30d');
  const [overviewData, setOverviewData] = useState<OverviewDashboardResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const fetchOverview = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await analyticsApi.getOverview({ time_window: timeWindow });
      setOverviewData(data);
      setLastRefreshed(new Date());
    } catch (err: any) {
      const msg =
        err.response?.data?.detail?.message ||
        err.response?.data?.message ||
        err.message ||
        'Failed to load dashboard metrics.';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [timeWindow]);

  useEffect(() => {
    fetchOverview();
    const interval = setInterval(() => {
      fetchOverview();
    }, 20000);
    return () => clearInterval(interval);
  }, [fetchOverview, activeSession?.token]);

  const displayQueue: IncidentQueueItem[] = useMemo(() => {
    if (overviewData && overviewData.priority_queue && overviewData.priority_queue.length > 0) {
      return overviewData.priority_queue.map(mapIncidentItem);
    }
    return [];
  }, [overviewData]);

  const safeActiveIndex = Math.min(activeIndex, Math.max(0, displayQueue.length - 1));
  const activeIncident: IncidentQueueItem | null = displayQueue.length > 0 ? displayQueue[safeActiveIndex] : null;
  const isAcknowledged = activeIncident ? !!acknowledgedCases[activeIncident.id] : false;
  const isEscalated = activeIncident ? !!escalatedCases[activeIncident.id] : false;

  const handleAcknowledge = (id: string) => {
    setAcknowledgedCases((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleEscalate = (id: string) => {
    setEscalatedCases((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const nextIncident = () => {
    if (displayQueue.length > 0) {
      setActiveIndex((prev) => (prev + 1) % displayQueue.length);
    }
  };

  const prevIncident = () => {
    if (displayQueue.length > 0) {
      setActiveIndex((prev) => (prev - 1 + displayQueue.length) % displayQueue.length);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '36px', position: 'relative' }}>
      {/* =========================================================================
          LIVE TELEMETRY FILTER BAR
          ========================================================================= */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: '12px',
          padding: '12px 20px',
          borderRadius: 'var(--radius-xl)',
          background: 'var(--surface-glass-card)',
          border: '1px solid var(--border-card)',
          backdropFilter: 'var(--blur-md)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.75rem', fontWeight: 700, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Reporting Window:
          </span>
          {(['today', '7d', '30d', '90d'] as const).map((tw) => (
            <button
              key={tw}
              type="button"
              onClick={() => setTimeWindow(tw)}
              style={{
                padding: '4px 12px',
                borderRadius: 'var(--radius-full)',
                border: '1px solid var(--border-subtle)',
                background: timeWindow === tw ? 'var(--accent-primary)' : 'var(--surface-glass-base)',
                color: timeWindow === tw ? '#ffffff' : 'var(--text-secondary)',
                fontSize: '0.75rem',
                fontWeight: 600,
                cursor: 'pointer',
                transition: 'all var(--transition-fast)',
              }}
            >
              {tw === 'today' ? 'Today' : tw === '7d' ? 'Last 7 Days' : tw === '30d' ? 'Last 30 Days' : 'Last 90 Days'}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          {lastRefreshed && (
            <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              Live Telemetry Updated {lastRefreshed.toLocaleTimeString()}
            </span>
          )}
          <Button
            size="sm"
            variant="outline"
            loading={isLoading}
            onClick={fetchOverview}
          >
            Refresh Telemetry
          </Button>
        </div>
      </div>

      {error && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--accent-rose-light)',
            color: 'var(--accent-rose)',
            fontSize: '0.8125rem',
            border: '1px solid rgba(244, 63, 94, 0.3)',
          }}
        >
          {error}
        </div>
      )}
      {/* =========================================================================
          OPERATIONAL INCIDENT COMMAND & LIVE OPERATIONAL TRIAGE
          - Single Highest-Priority Active Incident Spotlight
          - Accurate SLA Countdown & Evidence Verification
          - Compact Queue Preview of Next 3-5 Incidents
          - Direct Navigation to Dedicated Escalations Hub (/app/escalations)
          ========================================================================= */}
      <section style={{ position: 'relative' }}>
        <div className="glass-hero-container">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '16px', marginBottom: '24px' }}>
            <div>
              <div
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '8px',
                  padding: '4px 12px',
                  borderRadius: 'var(--radius-full)',
                  background: 'rgba(239, 68, 68, 0.12)',
                  border: '1px solid rgba(239, 68, 68, 0.25)',
                  fontSize: '0.6875rem',
                  fontWeight: 700,
                  letterSpacing: '0.15em',
                  textTransform: 'uppercase',
                  color: 'var(--accent-rose)',
                  marginBottom: '10px',
                }}
              >
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-rose)' }} />
                <span>Active Incident Command · Operational Summary</span>
              </div>
              <h2 style={{ fontSize: '1.5rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
                Active Incident Command & Live Operational Triage
              </h2>
              <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
                Highest-severity incident spotlight, verified SLA deadline countdown, and queue triage.
              </p>
            </div>

            <Link
              to="/app/escalations"
              className="glass-outline-pill"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '8px 18px',
                fontSize: '0.8125rem',
                fontWeight: 600,
                color: 'var(--accent-cyan)',
                borderColor: 'rgba(6, 182, 212, 0.4)',
                background: 'rgba(6, 182, 212, 0.08)',
                borderRadius: 'var(--radius-full)',
                textDecoration: 'none',
                transition: 'all var(--transition-fast)',
              }}
            >
              <span>View All Escalations ({overviewData ? overviewData.escalated_tickets : 0})</span>
              <span style={{ fontSize: '1rem', lineHeight: 1 }}>→</span>
            </Link>
          </div>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 420px), 1fr))',
              gap: '24px',
              alignItems: 'stretch',
            }}
          >
            {/* Active Incident Triage Card (Primary Spotlight) */}
            <div
              className="glass-card-nested"
              style={{
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                padding: '24px',
                borderRadius: 'var(--radius-2xl)',
              }}
            >
              {!activeIncident ? (
                isLoading ? (
                  <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--text-muted)' }}>
                    <ClockIcon size={32} style={{ margin: '0 auto 12px', color: 'var(--accent-primary)' }} />
                    <p style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>Loading Live Telemetry...</p>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>Querying active queue and SLA tracking engines.</p>
                  </div>
                ) : error ? (
                  <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--text-muted)' }}>
                    <AlertTriangleIcon size={32} style={{ margin: '0 auto 12px', color: 'var(--accent-amber)' }} />
                    <p style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>Backend Telemetry Unavailable</p>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '6px', maxWidth: '380px', margin: '6px auto 0' }}>
                      {error}
                    </p>
                    <div style={{ marginTop: '16px' }}>
                      <Button size="sm" variant="outline" onClick={fetchOverview}>Retry Connection</Button>
                    </div>
                  </div>
                ) : (
                  <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--text-muted)' }}>
                    <CheckCircleIcon size={32} style={{ margin: '0 auto 12px', color: 'var(--accent-emerald)' }} />
                    <p style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>Operational Queue Clear</p>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '6px', maxWidth: '380px', margin: '6px auto 0' }}>
                      Zero critical or urgent incidents requiring supervisor triage in the selected window ({timeWindow === 'today' ? 'Today' : timeWindow === '7d' ? 'Last 7 Days' : timeWindow === '30d' ? 'Last 30 Days' : 'Last 90 Days'}).
                    </p>
                  </div>
                )
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', height: '100%', justifyContent: 'space-between' }}>
                  {/* Top Details Block */}
                  <div>
                    {/* Header Strip: Severity, Status, Priority & Queue Stepper */}
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '10px', marginBottom: '14px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                        <Badge variant={activeIncident.priority === 'CRITICAL' ? 'danger' : 'warning'} dot size="sm">
                          {activeIncident.severity}
                        </Badge>
                        <Badge variant={isAcknowledged ? 'success' : 'cyan'} size="sm">
                          {isAcknowledged ? 'STATUS: ACKNOWLEDGED' : `STATUS: ${activeIncident.status}`}
                        </Badge>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                          Team: <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{activeIncident.assignedTeam}</strong>
                        </span>
                      </div>

                      {/* Case Stepper */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <button
                          type="button"
                          onClick={prevIncident}
                          className="glass-outline-pill"
                          style={{ padding: '4px 10px', fontSize: '0.6875rem', cursor: 'pointer' }}
                          title="Previous incident"
                          aria-label="Previous incident"
                        >
                          ←
                        </button>
                        <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>
                          {safeActiveIndex + 1} / {displayQueue.length}
                        </span>
                        <button
                          type="button"
                          onClick={nextIncident}
                          className="glass-outline-pill"
                          style={{ padding: '4px 10px', fontSize: '0.6875rem', cursor: 'pointer' }}
                          title="Next incident"
                          aria-label="Next incident"
                        >
                          →
                        </button>
                      </div>
                    </div>

                    {/* Incident Title (Properly wrapped, no truncation) */}
                    <h3
                      style={{
                        fontSize: '1.25rem',
                        fontWeight: 700,
                        letterSpacing: '-0.02em',
                        lineHeight: 1.35,
                        color: 'var(--text-primary)',
                        margin: '0 0 10px',
                        wordBreak: 'break-word',
                        whiteSpace: 'normal',
                      }}
                    >
                      {activeIncident.title}
                    </h3>

                    {/* Ticket ID & Customer Identity */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '18px' }}>
                      <span
                        style={{
                          fontFamily: 'var(--font-mono)',
                          fontSize: '0.8125rem',
                          fontWeight: 600,
                          color: 'var(--accent-cyan)',
                          background: 'rgba(6, 182, 212, 0.1)',
                          padding: '2px 8px',
                          borderRadius: 'var(--radius-sm)',
                          border: '1px solid rgba(6, 182, 212, 0.25)',
                        }}
                      >
                        {activeIncident.ticketId}
                      </span>
                      <span style={{ fontSize: '0.8125rem', color: 'var(--text-primary)', fontWeight: 600 }}>
                        {activeIncident.customer}
                      </span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>•</span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--accent-cyan)', background: 'rgba(6, 182, 212, 0.08)', padding: '1px 6px', borderRadius: '4px' }}>
                        Tier: {activeIncident.tier}
                      </span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>•</span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                        Category: {activeIncident.category}
                      </span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>•</span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--accent-cyan)', fontWeight: 600 }}>
                        Score: {activeIncident.priorityScore.toFixed(1)} / 5.0
                      </span>
                    </div>

                    {/* Cohesive Operational Context & Evidence Verification Block */}
                    <div
                      style={{
                        padding: '14px 16px',
                        borderRadius: 'var(--radius-lg)',
                        background: 'rgba(0, 0, 0, 0.25)',
                        border: '1px solid var(--border-subtle)',
                        marginBottom: '16px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '8px',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <AlertTriangleIcon size={14} style={{ color: activeIncident.priority === 'CRITICAL' ? 'var(--accent-rose)' : 'var(--accent-amber)' }} />
                          <span style={{ fontSize: '0.6875rem', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
                            Operational Trigger & Incident Context
                          </span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <ShieldCheckIcon size={14} style={{ color: 'var(--accent-emerald)' }} />
                          <span style={{ fontSize: '0.6875rem', fontWeight: 600, color: 'var(--accent-emerald)' }}>
                            Evidence: {activeIncident.evidenceStatus}
                          </span>
                        </div>
                      </div>
                      <p style={{ fontSize: '0.8125rem', lineHeight: 1.5, color: 'var(--text-secondary)', margin: 0 }}>
                        {activeIncident.evidenceSnippet || activeIncident.latestActivity}
                      </p>
                    </div>

                    {/* SLA Countdown with Clear Multi-Gradient Progress Bar */}
                    <div
                      style={{
                        padding: '14px 16px',
                        borderRadius: 'var(--radius-lg)',
                        background: 'rgba(0, 0, 0, 0.25)',
                        border: '1px solid var(--border-subtle)',
                        marginBottom: '20px',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px', flexWrap: 'wrap', gap: '8px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <ClockIcon size={15} style={{ color: activeIncident.isOverdue || activeIncident.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-amber)' }} />
                          <span style={{ fontSize: '0.75rem', fontWeight: 700, letterSpacing: '0.05em', color: 'var(--text-primary)' }}>
                            SLA DEADLINE COUNTDOWN
                          </span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px' }}>
                          {activeIncident.isOverdue ? (
                            <span
                              style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.8125rem',
                                fontWeight: 700,
                                color: 'var(--accent-rose)',
                                background: 'rgba(244, 63, 94, 0.15)',
                                border: '1px solid rgba(244, 63, 94, 0.3)',
                                padding: '2px 8px',
                                borderRadius: '4px',
                                letterSpacing: '0.03em',
                              }}
                            >
                              {activeIncident.overdueDuration ? `OVERDUE: ${activeIncident.overdueDuration}` : activeIncident.slaRemaining}
                            </span>
                          ) : (
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.9375rem', fontWeight: 700, color: activeIncident.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-amber)' }}>
                              {activeIncident.slaRemaining}
                            </span>
                          )}
                          <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                            ({activeIncident.progress}% consumed)
                          </span>
                        </div>
                      </div>

                      {/* 8px SLA Progress Bar */}
                      <div style={{ height: '8px', width: '100%', borderRadius: 'var(--radius-full)', background: 'rgba(255, 255, 255, 0.08)', overflow: 'hidden' }}>
                        <div
                          style={{
                            height: '100%',
                            borderRadius: 'var(--radius-full)',
                            background: activeIncident.isOverdue || activeIncident.progress > 60
                              ? 'linear-gradient(90deg, var(--accent-primary) 0%, var(--accent-amber) 70%, var(--accent-rose) 100%)'
                              : 'linear-gradient(90deg, var(--accent-primary) 0%, var(--accent-cyan) 100%)',
                            width: `${activeIncident.progress}%`,
                            transition: 'width 0.4s ease',
                          }}
                        />
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '8px', flexWrap: 'wrap', gap: '4px' }}>
                        <span>Elapsed: {activeIncident.slaElapsed}</span>
                        <span>Target: {activeIncident.slaTotal}</span>
                        {activeIncident.isOverdue ? (
                          <span style={{ color: 'var(--accent-rose)', fontWeight: 700 }}>
                            SLA Breached ({activeIncident.overdueDuration || 'Overdue'})
                          </span>
                        ) : (
                          <span style={{ color: activeIncident.progress > 50 ? 'var(--accent-amber)' : 'var(--accent-emerald)', fontWeight: 600 }}>
                            Breach Margin: {Math.max(0, 100 - activeIncident.progress)}%
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Operational Action Buttons Bar (Visually Distinct) */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px', paddingTop: '12px', borderTop: '1px solid rgba(255, 255, 255, 0.08)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                      {/* Primary Action Button: Acknowledge & Triage */}
                      <button
                        type="button"
                        onClick={() => handleAcknowledge(activeIncident.id)}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '8px',
                          padding: '9px 18px',
                          borderRadius: 'var(--radius-lg)',
                          fontSize: '0.8125rem',
                          fontWeight: 600,
                          cursor: 'pointer',
                          transition: 'all 0.2s ease',
                          border: isAcknowledged ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid rgba(6, 182, 212, 0.5)',
                          background: isAcknowledged ? 'rgba(16, 185, 129, 0.18)' : 'linear-gradient(135deg, rgba(6, 182, 212, 0.25) 0%, rgba(99, 102, 241, 0.25) 100%)',
                          color: isAcknowledged ? 'var(--accent-emerald)' : '#ffffff',
                          boxShadow: isAcknowledged ? '0 0 12px rgba(16, 185, 129, 0.2)' : '0 0 16px rgba(6, 182, 212, 0.25)',
                        }}
                      >
                        <CheckCircleIcon size={16} style={{ color: isAcknowledged ? 'var(--accent-emerald)' : 'var(--accent-cyan)' }} />
                        <span>{isAcknowledged ? 'Triage Acknowledged' : 'Acknowledge & Triage'}</span>
                      </button>

                      {/* Warning Action Button: Escalate On-Call */}
                      <button
                        type="button"
                        onClick={() => handleEscalate(activeIncident.id)}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '8px',
                          padding: '9px 18px',
                          borderRadius: 'var(--radius-lg)',
                          fontSize: '0.8125rem',
                          fontWeight: 600,
                          cursor: 'pointer',
                          transition: 'all 0.2s ease',
                          border: isEscalated ? '1px solid rgba(244, 63, 94, 0.6)' : '1px solid rgba(245, 158, 11, 0.4)',
                          background: isEscalated ? 'rgba(244, 63, 94, 0.2)' : 'rgba(245, 158, 11, 0.1)',
                          color: isEscalated ? 'var(--accent-rose)' : 'var(--accent-amber)',
                        }}
                      >
                        <AlertTriangleIcon size={16} style={{ color: isEscalated ? 'var(--accent-rose)' : 'var(--accent-amber)' }} />
                        <span>{isEscalated ? 'Escalated to On-Call' : 'Escalate On-Call'}</span>
                      </button>
                    </div>

                    {/* Inspect Full Dossier Action */}
                    <Link
                      to={`/app/escalations?inspect=${encodeURIComponent(activeIncident.ticketId)}`}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '6px',
                        padding: '8px 16px',
                        borderRadius: 'var(--radius-lg)',
                        fontSize: '0.75rem',
                        fontWeight: 600,
                        color: 'var(--accent-cyan)',
                        background: 'rgba(6, 182, 212, 0.08)',
                        border: '1px solid rgba(6, 182, 212, 0.3)',
                        textDecoration: 'none',
                        transition: 'all 0.2s ease',
                      }}
                    >
                      <span>Inspect Full Dossier</span>
                      <span style={{ fontSize: '0.875rem' }}>↗</span>
                    </Link>
                  </div>
                </div>
              )}
            </div>

            {/* Right Column: Priority Queue (Next 3-5 Incidents with Clear Selection) */}
            <div
              className="glass-card-nested"
              style={{
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                gap: '16px',
                padding: '24px',
                borderRadius: 'var(--radius-2xl)',
              }}
            >
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid rgba(255, 255, 255, 0.08)', paddingBottom: '12px', marginBottom: '14px' }}>
                  <div>
                    <h3 style={{ fontSize: '1.0625rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                      Priority Queue
                    </h3>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                      Next 3–5 critical incidents awaiting triage
                    </p>
                  </div>
                  <Badge variant="cyan" size="sm">
                    {displayQueue.length} Queued
                  </Badge>
                </div>

                {/* Queue Incident List */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {displayQueue.length === 0 ? (
                    <div
                      style={{
                        padding: '36px 16px',
                        textAlign: 'center',
                        color: 'var(--text-muted)',
                        fontSize: '0.8125rem',
                        background: 'rgba(0, 0, 0, 0.15)',
                        borderRadius: 'var(--radius-lg)',
                        border: '1px solid var(--border-subtle)',
                      }}
                    >
                      <CheckCircleIcon size={24} style={{ color: 'var(--accent-emerald)', margin: '0 auto 8px' }} />
                      <p style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Secondary Queue Clear</p>
                      <p style={{ fontSize: '0.75rem', marginTop: '4px' }}>All other cases are currently within healthy SLA margins.</p>
                    </div>
                  ) : (
                    displayQueue.slice(0, 5).map((item, idx) => {
                      const isSelected = idx === safeActiveIndex;

                      return (
                        <div
                          key={item.id}
                          role="button"
                          tabIndex={0}
                          onClick={() => setActiveIndex(idx)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              setActiveIndex(idx);
                            }
                          }}
                          aria-pressed={isSelected}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '12px',
                            padding: '10px 14px',
                            borderRadius: 'var(--radius-lg)',
                            background: isSelected ? 'rgba(6, 182, 212, 0.14)' : 'rgba(255, 255, 255, 0.03)',
                            border: isSelected ? '1px solid rgba(6, 182, 212, 0.5)' : '1px solid var(--border-subtle)',
                            borderLeft: isSelected ? '4px solid var(--accent-cyan)' : '4px solid transparent',
                            cursor: 'pointer',
                            transition: 'all var(--transition-fast)',
                            boxShadow: isSelected ? '0 4px 20px rgba(6, 182, 212, 0.15)' : 'none',
                          }}
                        >
                          {/* Rank indicator */}
                          <div
                            style={{
                              width: '30px',
                              height: '30px',
                              borderRadius: 'var(--radius-md)',
                              background: isSelected ? 'var(--accent-cyan)' : 'rgba(255, 255, 255, 0.08)',
                              color: isSelected ? '#0b1329' : 'var(--text-secondary)',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              fontSize: '0.75rem',
                              fontWeight: 700,
                              flexShrink: 0,
                            }}
                          >
                            P{idx + 1}
                          </div>

                          {/* Title & Customer */}
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <p
                              style={{
                                fontSize: '0.8125rem',
                                fontWeight: 600,
                                color: isSelected ? 'var(--text-primary)' : 'var(--text-secondary)',
                                margin: 0,
                                lineHeight: 1.35,
                                wordBreak: 'break-word',
                                display: '-webkit-box',
                                WebkitLineClamp: 2,
                                WebkitBoxOrient: 'vertical',
                                overflow: 'hidden',
                              }}
                            >
                              {item.title}
                            </p>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: '3px' }}>
                              <span style={{ fontSize: '0.6875rem', color: 'var(--accent-cyan)', fontFamily: 'var(--font-mono)' }}>
                                {item.ticketId}
                              </span>
                              <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>•</span>
                              <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                                {item.customer}
                              </span>
                            </div>
                          </div>

                          {/* Priority & SLA */}
                          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '3px', flexShrink: 0 }}>
                            <Badge variant={item.priority === 'CRITICAL' ? 'danger' : 'warning'} size="sm">
                              {item.priority}
                            </Badge>
                            <span
                              style={{
                                fontSize: '0.6875rem',
                                fontWeight: item.isOverdue ? 700 : 600,
                                color: item.isOverdue || item.progress > 60 ? 'var(--accent-rose)' : 'var(--accent-amber)',
                              }}
                            >
                              {item.isOverdue && item.overdueDuration ? `+${item.overdueDuration}` : item.slaRemaining.replace(' remaining', '')}
                            </span>
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>

              {/* Direct Link to Dedicated Escalations Hub */}
              <div style={{ paddingTop: '12px', borderTop: '1px solid rgba(255, 255, 255, 0.08)' }}>
                <Link
                  to="/app/escalations"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '8px',
                    width: '100%',
                    padding: '11px 16px',
                    borderRadius: 'var(--radius-xl)',
                    background: 'linear-gradient(135deg, rgba(6, 182, 212, 0.15) 0%, rgba(99, 102, 241, 0.15) 100%)',
                    border: '1px solid rgba(6, 182, 212, 0.35)',
                    color: 'var(--text-primary)',
                    fontSize: '0.8125rem',
                    fontWeight: 600,
                    textDecoration: 'none',
                    transition: 'all var(--transition-fast)',
                    boxShadow: '0 4px 16px rgba(0, 0, 0, 0.2)',
                  }}
                >
                  <AlertTriangleIcon size={15} style={{ color: 'var(--accent-rose)' }} />
                  <span>View All Escalations Hub ({overviewData ? overviewData.escalated_tickets : 0} Cases)</span>
                  <span style={{ fontSize: '1rem', lineHeight: 1, color: 'var(--accent-cyan)' }}>→</span>
                </Link>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
};
