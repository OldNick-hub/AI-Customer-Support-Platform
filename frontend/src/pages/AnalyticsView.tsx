import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Card } from '../components/common/Card';
import { Badge, type BadgeVariant } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import {
  BarChart3Icon,
  ClockIcon,
  ShieldCheckIcon,
  AlertTriangleIcon,
  CheckCircleIcon,
  MessageSquareIcon,
  FileTextIcon,
  DatabaseIcon,
} from '../components/common/Icons';
import { useAuth } from '../context/AuthContext';
import {
  analyticsApi,
  type PerformanceAnalyticsResponse,
  type OperationalIntelligenceResponse,
  type OperationalInsightItem,
  type DailyTrendItem,
  type DepartmentWorkloadItem,
} from '../services/api';

export const AnalyticsView: React.FC = () => {
  const { currentUser } = useAuth();
  const isStaff = currentUser ? !currentUser.roles.includes('CUSTOMER') : true;

  // Filters & State
  const [timeWindow, setTimeWindow] = useState<'today' | '7d' | '30d' | '90d'>('30d');
  const [selectedCategory, setSelectedCategory] = useState<string>('');
  const [selectedPriority, setSelectedPriority] = useState<string>('');
  const [performanceData, setPerformanceData] = useState<PerformanceAnalyticsResponse | null>(null);
  const [operationalData, setOperationalData] = useState<OperationalIntelligenceResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const fetchData = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const filterParams = {
        time_window: timeWindow,
        category: selectedCategory || undefined,
        priority: selectedPriority || undefined,
      };

      const [perfRes, intelRes] = await Promise.all([
        analyticsApi.getPerformance(filterParams),
        analyticsApi.getOperationalIntelligence(filterParams),
      ]);

      setPerformanceData(perfRes);
      setOperationalData(intelRes);
      setLastRefreshed(new Date());
    } catch (err: any) {
      const msg =
        err.response?.data?.detail?.message ||
        err.response?.data?.detail ||
        err.message ||
        'Failed to fetch analytics telemetry.';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [timeWindow, selectedCategory, selectedPriority]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Derived KPI calculations
  const ticketTrends: DailyTrendItem[] = performanceData?.ticket_trends || [];

  // SVG Chart computation
  const chartConfig = useMemo(() => {
    const width = 760;
    const height = 220;
    const padding = { top: 20, right: 30, bottom: 35, left: 45 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;

    if (!ticketTrends || ticketTrends.length === 0) {
      return {
        width,
        height,
        padding,
        chartW,
        chartH,
        maxVal: 10,
        pathCreated: '',
        pathResolved: '',
        areaCreated: '',
        areaResolved: '',
        coordsCreated: [] as Array<{ x: number; y: number; val: number; date: string }>,
        coordsResolved: [] as Array<{ x: number; y: number; val: number; date: string }>,
      };
    }

    const maxVal = Math.max(
      5,
      ...ticketTrends.map((d: DailyTrendItem) => Math.max(d.created, d.resolved, d.escalated))
    );

    const stepX = ticketTrends.length > 1 ? chartW / (ticketTrends.length - 1) : chartW / 2;

    const coordsCreated = ticketTrends.map((d: DailyTrendItem, i: number) => {
      const x = padding.left + i * stepX;
      const y = padding.top + chartH - (d.created / maxVal) * chartH;
      return { x, y, val: d.created, date: d.date };
    });

    const coordsResolved = ticketTrends.map((d: DailyTrendItem, i: number) => {
      const x = padding.left + i * stepX;
      const y = padding.top + chartH - (d.resolved / maxVal) * chartH;
      return { x, y, val: d.resolved, date: d.date };
    });

    const pathCreated = coordsCreated.reduce(
      (acc: string, pt: { x: number; y: number }, i: number) => (i === 0 ? `M ${pt.x} ${pt.y}` : `${acc} L ${pt.x} ${pt.y}`),
      ''
    );
    const pathResolved = coordsResolved.reduce(
      (acc: string, pt: { x: number; y: number }, i: number) => (i === 0 ? `M ${pt.x} ${pt.y}` : `${acc} L ${pt.x} ${pt.y}`),
      ''
    );

    const areaCreated =
      coordsCreated.length > 0
        ? `${pathCreated} L ${coordsCreated[coordsCreated.length - 1].x} ${padding.top + chartH} L ${coordsCreated[0].x} ${padding.top + chartH} Z`
        : '';

    const areaResolved =
      coordsResolved.length > 0
        ? `${pathResolved} L ${coordsResolved[coordsResolved.length - 1].x} ${padding.top + chartH} L ${coordsResolved[0].x} ${padding.top + chartH} Z`
        : '';

    return {
      width,
      height,
      padding,
      chartW,
      chartH,
      maxVal,
      pathCreated,
      pathResolved,
      areaCreated,
      areaResolved,
      coordsCreated,
      coordsResolved,
    };
  }, [ticketTrends]);

  const insights = operationalData?.insights || [];
  const workloads = performanceData?.operational_workload || [];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '32px', position: 'relative' }}>
      {/* =========================================================================
          PAGE HEADER & TELEMETRY CONTROLS
          ========================================================================= */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
                Platform Analytics & Operational Intelligence
              </h1>
              <Badge variant="cyan" size="sm">LIVE TELEMETRY</Badge>
            </div>
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
              Deterministic SLA metrics, multi-turn AI resolution rates, evidence integrity, and autonomous operations.
            </p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            {lastRefreshed && (
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Telemetry synced: {lastRefreshed.toLocaleTimeString()}
              </span>
            )}
            <Button
              size="sm"
              variant="outline"
              loading={isLoading}
              onClick={fetchData}
            >
              Refresh Telemetry
            </Button>
          </div>
        </div>

        {/* Global Filter Bar */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: '14px',
            padding: '12px 20px',
            borderRadius: 'var(--radius-xl)',
            background: 'var(--surface-glass-card)',
            border: '1px solid var(--border-card)',
            backdropFilter: 'var(--blur-md)',
          }}
        >
          {/* Time Window Selector */}
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

          {/* Category & Priority Selectors */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <label htmlFor="analytics-category-select" style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600 }}>Category:</label>
              <select
                id="analytics-category-select"
                aria-label="Category"
                value={selectedCategory}
                onChange={(e) => setSelectedCategory(e.target.value)}
                style={{
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-glass-base)',
                  border: '1px solid var(--border-subtle)',
                  color: 'var(--text-primary)',
                  fontSize: '0.75rem',
                  cursor: 'pointer',
                }}
              >
                <option value="">All Categories</option>
                <option value="Hardware Support">Hardware Support</option>
                <option value="Billing Inquiries">Billing Inquiries</option>
                <option value="Returns & Refunds">Returns & Refunds</option>
                <option value="General">General Inquiries</option>
              </select>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <label htmlFor="analytics-priority-select" style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600 }}>Priority:</label>
              <select
                id="analytics-priority-select"
                aria-label="Priority"
                value={selectedPriority}
                onChange={(e) => setSelectedPriority(e.target.value)}
                style={{
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-glass-base)',
                  border: '1px solid var(--border-subtle)',
                  color: 'var(--text-primary)',
                  fontSize: '0.75rem',
                  cursor: 'pointer',
                }}
              >
                <option value="">All Priorities</option>
                <option value="CRITICAL">Critical</option>
                <option value="HIGH">High</option>
                <option value="MEDIUM">Medium</option>
                <option value="LOW">Low</option>
              </select>
            </div>
          </div>
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
          OPERATIONAL INTELLIGENCE RULE ALERTS
          Surfaces deterministic watchdog alerts (Capacity saturation, SLA risks, Safety, Conflicts)
          ========================================================================= */}
      {insights.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <h2 style={{ fontSize: '1.125rem', fontWeight: 700, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <AlertTriangleIcon size={18} style={{ color: 'var(--accent-amber)' }} />
              Operational Intelligence & Rule-Based Watchdogs ({insights.length})
            </h2>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Deterministic cross-variable rules evaluated across current period
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: '16px' }}>
            {insights.map((alert: OperationalInsightItem, idx: number) => {
              const isCrit = alert.severity === 'CRITICAL';
              const isWarn = alert.severity === 'WARNING';
              const accentColor = isCrit ? 'var(--accent-rose)' : isWarn ? 'var(--accent-amber)' : 'var(--accent-cyan)';
              const badgeVariant: BadgeVariant = isCrit ? 'danger' : isWarn ? 'warning' : 'cyan';

              return (
                <div
                  key={`${alert.id || alert.rule_name}-${idx}`}
                  style={{
                    padding: '16px 20px',
                    borderRadius: 'var(--radius-xl)',
                    background: 'var(--surface-glass-card)',
                    border: `1px solid ${isCrit ? 'rgba(244, 63, 94, 0.4)' : isWarn ? 'rgba(245, 158, 11, 0.35)' : 'var(--border-card)'}`,
                    backdropFilter: 'var(--blur-md)',
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'space-between',
                    gap: '12px',
                    boxShadow: isCrit ? '0 8px 24px rgba(244, 63, 94, 0.15)' : 'none',
                  }}
                >
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                      <Badge variant={badgeVariant} dot size="sm">
                        {alert.severity}
                      </Badge>
                      <span style={{ fontSize: '0.6875rem', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                        {alert.rule_name}
                      </span>
                    </div>

                    <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '6px' }}>
                      {alert.title}
                    </h3>
                    <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5, margin: 0 }}>
                      {alert.description}
                    </p>
                  </div>

                  {/* Supporting metric pill & Action link */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', paddingTop: '8px', borderTop: '1px solid var(--border-subtle)' }}>
                    {alert.supporting_metric && (
                      <span
                        style={{
                          fontSize: '0.6875rem',
                          padding: '2px 8px',
                          borderRadius: 'var(--radius-sm)',
                          background: 'var(--surface-glass-elevated)',
                          color: 'var(--text-muted)',
                          fontFamily: 'var(--font-mono)',
                        }}
                      >
                        Metric: <strong style={{ color: accentColor }}>{alert.supporting_metric}</strong>
                      </span>
                    )}

                    {alert.target_route && (
                      <Link
                        to={alert.target_route}
                        style={{
                          fontSize: '0.75rem',
                          fontWeight: 600,
                          color: accentColor,
                          textDecoration: 'none',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '4px',
                        }}
                      >
                        {alert.suggested_action || 'Take Action'} →
                      </Link>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            padding: '14px 20px',
            borderRadius: 'var(--radius-xl)',
            background: 'var(--surface-glass-base)',
            border: '1px solid var(--border-card)',
            color: 'var(--text-secondary)',
            fontSize: '0.8125rem',
          }}
        >
          <CheckCircleIcon size={18} style={{ color: 'var(--accent-emerald)', flexShrink: 0 }} />
          <span>
            <strong>Operational Watchdog Status: Nominal</strong> — All SLA deadlines, department workload limits, and cryptographic evidence reconciliations are performing within safety thresholds.
          </span>
        </div>
      )}

      {/* =========================================================================
          KEY PERFORMANCE INDICATORS (KPIs) STRIP
          ========================================================================= */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '16px' }}>
        {/* SLA Compliance % */}
        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
              SLA Compliance
            </span>
            <ShieldCheckIcon
              size={18}
              style={{
                color: (performanceData?.overall_sla_compliance_pct ?? 100) >= 95 ? 'var(--accent-emerald)' : 'var(--accent-amber)',
              }}
            />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: '8px' }}>
            {performanceData ? `${performanceData.overall_sla_compliance_pct}%` : '—'}
          </div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
            Target threshold: 95.0%
          </div>
        </Card>

        {/* SLA Breaches */}
        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
              Breach Events
            </span>
            <AlertTriangleIcon
              size={18}
              style={{
                color: (performanceData?.total_breaches ?? 0) > 0 ? 'var(--accent-rose)' : 'var(--accent-emerald)',
              }}
            />
          </div>
          <div
            style={{
              fontSize: '1.75rem',
              fontWeight: 700,
              color: (performanceData?.total_breaches ?? 0) > 0 ? 'var(--accent-rose)' : 'var(--text-primary)',
              marginTop: '8px',
            }}
          >
            {performanceData ? performanceData.total_breaches : '—'}
          </div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
            {performanceData?.total_breaches === 0 ? 'Zero breaches detected' : 'Escalated to supervision'}
          </div>
        </Card>

        {/* Avg Resolution Time */}
        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
              Avg Resolution Time
            </span>
            <ClockIcon size={18} style={{ color: 'var(--accent-cyan)' }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: '8px' }}>
            {performanceData ? (
              performanceData.avg_resolution_time_minutes > 60
                ? `${(performanceData.avg_resolution_time_minutes / 60).toFixed(1)} hrs`
                : `${performanceData.avg_resolution_time_minutes} min`
            ) : '—'}
          </div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
            Ticket inception to resolution
          </div>
        </Card>

        {/* Total Ingested in Window */}
        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
              Ingested In Window
            </span>
            <CheckCircleIcon size={18} style={{ color: 'var(--accent-emerald)' }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--accent-emerald)', marginTop: '8px' }}>
            {performanceData ? ticketTrends.reduce((sum: number, d: DailyTrendItem) => sum + d.created, 0) : '—'}
          </div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
            Total volume logged
          </div>
        </Card>
      </div>

      {/* =========================================================================
          DATA VISUALIZATION: RESPONSIVE PURE SVG TICKET TRENDS CHART
          Zero external library dependencies — fluid liquid glass styling
          ========================================================================= */}
      <Card variant="base">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '12px', marginBottom: '20px' }}>
          <div>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <BarChart3Icon size={18} style={{ color: 'var(--accent-primary)' }} />
              Daily Ingestion & Resolution Trajectory
            </h3>
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-muted)', marginTop: '2px' }}>
              Comparison of incoming case volume vs completed resolutions over selected window
            </p>
          </div>

          {/* Chart Legend */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '16px', fontSize: '0.75rem', fontWeight: 600 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--accent-cyan)' }} />
              <span style={{ color: 'var(--text-secondary)' }}>Ingested</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--accent-emerald)' }} />
              <span style={{ color: 'var(--text-secondary)' }}>Resolved</span>
            </div>
          </div>
        </div>

        {/* SVG Canvas Container */}
        <div style={{ width: '100%', overflowX: 'auto' }}>
          {ticketTrends.length === 0 ? (
            <div style={{ height: '200px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: '0.875rem' }}>
              No ticket history recorded in this reporting period.
            </div>
          ) : (
            <svg
              viewBox={`0 0 ${chartConfig.width} ${chartConfig.height}`}
              style={{ width: '100%', height: 'auto', minWidth: '500px', overflow: 'visible' }}
            >
              <defs>
                <linearGradient id="gradientCreated" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--accent-cyan)" stopOpacity="0.3" />
                  <stop offset="100%" stopColor="var(--accent-cyan)" stopOpacity="0.0" />
                </linearGradient>
                <linearGradient id="gradientResolved" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--accent-emerald)" stopOpacity="0.3" />
                  <stop offset="100%" stopColor="var(--accent-emerald)" stopOpacity="0.0" />
                </linearGradient>
              </defs>

              {/* Horizontal Reference Grid Lines */}
              {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
                const y = chartConfig.padding.top + chartConfig.chartH * (1 - ratio);
                const val = Math.round(chartConfig.maxVal * ratio);
                return (
                  <g key={ratio}>
                    <line
                      x1={chartConfig.padding.left}
                      y1={y}
                      x2={chartConfig.width - chartConfig.padding.right}
                      y2={y}
                      stroke="var(--border-subtle)"
                      strokeDasharray="4 4"
                      strokeWidth="1"
                    />
                    <text
                      x={chartConfig.padding.left - 8}
                      y={y + 4}
                      textAnchor="end"
                      fontSize="10"
                      fill="var(--text-muted)"
                      fontFamily="var(--font-mono)"
                    >
                      {val}
                    </text>
                  </g>
                );
              })}

              {/* Area Fills */}
              {chartConfig.areaCreated && (
                <path d={chartConfig.areaCreated} fill="url(#gradientCreated)" />
              )}
              {chartConfig.areaResolved && (
                <path d={chartConfig.areaResolved} fill="url(#gradientResolved)" />
              )}

              {/* Trend Lines */}
              {chartConfig.pathCreated && (
                <path
                  d={chartConfig.pathCreated}
                  fill="none"
                  stroke="var(--accent-cyan)"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}
              {chartConfig.pathResolved && (
                <path
                  d={chartConfig.pathResolved}
                  fill="none"
                  stroke="var(--accent-emerald)"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}

              {/* Data Point Dots & Hover Tooltips */}
              {chartConfig.coordsCreated?.map((pt: { x: number; y: number; val: number; date: string }, idx: number) => (
                <g key={`cr-${idx}`}>
                  <circle
                    cx={pt.x}
                    cy={pt.y}
                    r={3.5}
                    fill="var(--accent-cyan)"
                    stroke="var(--bg-app)"
                    strokeWidth="1.5"
                  />
                  <title>{`${pt.date}: Ingested ${pt.val} tickets`}</title>
                </g>
              ))}

              {chartConfig.coordsResolved?.map((pt: { x: number; y: number; val: number; date: string }, idx: number) => (
                <g key={`res-${idx}`}>
                  <circle
                    cx={pt.x}
                    cy={pt.y}
                    r={3.5}
                    fill="var(--accent-emerald)"
                    stroke="var(--bg-app)"
                    strokeWidth="1.5"
                  />
                  <title>{`${pt.date}: Resolved ${pt.val} tickets`}</title>
                </g>
              ))}

              {/* X-Axis Date Labels */}
              {ticketTrends.map((d: DailyTrendItem, idx: number) => {
                const interval = ticketTrends.length > 15 ? 4 : ticketTrends.length > 7 ? 2 : 1;
                if (idx % interval !== 0 && idx !== ticketTrends.length - 1) return null;

                const stepX = ticketTrends.length > 1 ? chartConfig.chartW / (ticketTrends.length - 1) : chartConfig.chartW / 2;
                const x = chartConfig.padding.left + idx * stepX;
                const y = chartConfig.height - 10;
                const label = d.date.length >= 10 ? d.date.slice(5) : d.date;

                return (
                  <text
                    key={d.date}
                    x={x}
                    y={y}
                    textAnchor="middle"
                    fontSize="10"
                    fill="var(--text-muted)"
                    fontFamily="var(--font-mono)"
                  >
                    {label}
                  </text>
                );
              })}
            </svg>
          )}
        </div>
      </Card>

      {/* =========================================================================
          DETAILED SLA PERFORMANCE BREAKDOWNS (Category & Priority)
          ========================================================================= */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: '24px' }}>
        {/* SLA Breakdown by Category */}
        <Card variant="base">
          <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
            SLA Compliance by Category
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {(!performanceData?.sla_by_category || performanceData.sla_by_category.length === 0) ? (
              <div style={{ color: 'var(--text-muted)', fontSize: '0.8125rem' }}>No categorized tickets in window.</div>
            ) : (
              performanceData.sla_by_category.map((cat) => {
                const pct = cat.compliance_pct;
                const barColor = pct >= 95 ? 'var(--accent-emerald)' : pct >= 85 ? 'var(--accent-amber)' : 'var(--accent-rose)';

                return (
                  <div key={cat.label}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8125rem', marginBottom: '6px' }}>
                      <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{cat.label}</span>
                      <span style={{ color: barColor, fontWeight: 700 }}>
                        {pct}% ({cat.compliant}/{cat.total})
                      </span>
                    </div>
                    <div style={{ height: '8px', borderRadius: '4px', background: 'var(--surface-glass-hover)', overflow: 'hidden' }}>
                      <div style={{ width: `${pct}%`, height: '100%', background: barColor, transition: 'width 0.4s ease' }} />
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>

        {/* SLA Breakdown by Priority */}
        <Card variant="base">
          <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
            SLA Compliance by Severity Tier
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {(!performanceData?.sla_by_priority || performanceData.sla_by_priority.length === 0) ? (
              <div style={{ color: 'var(--text-muted)', fontSize: '0.8125rem' }}>No priority tickets in window.</div>
            ) : (
              performanceData.sla_by_priority.map((prio) => {
                const pct = prio.compliance_pct;
                const barColor =
                  prio.label === 'CRITICAL'
                    ? 'var(--accent-rose)'
                    : prio.label === 'HIGH'
                    ? 'var(--accent-amber)'
                    : prio.label === 'MEDIUM'
                    ? 'var(--accent-cyan)'
                    : 'var(--accent-emerald)';

                const badgeVariant: BadgeVariant =
                  prio.label === 'CRITICAL' ? 'danger' : prio.label === 'HIGH' ? 'warning' : 'cyan';

                return (
                  <div key={prio.label}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8125rem', marginBottom: '6px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <Badge variant={badgeVariant} size="sm">
                          {prio.label}
                        </Badge>
                        <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                          {prio.breached} breaches
                        </span>
                      </div>
                      <span style={{ color: barColor, fontWeight: 700 }}>
                        {pct}%
                      </span>
                    </div>
                    <div style={{ height: '8px', borderRadius: '4px', background: 'var(--surface-glass-hover)', overflow: 'hidden' }}>
                      <div style={{ width: `${pct}%`, height: '100%', background: barColor, transition: 'width 0.4s ease' }} />
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>
      </div>

      {/* =========================================================================
          CROSS-CHANNEL INTELLIGENCE: Multilingual + Evidence Telemetry
          ========================================================================= */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: '24px' }}>
        {/* Multilingual Traffic Breakdown */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <MessageSquareIcon size={16} style={{ color: 'var(--accent-primary)' }} />
              Multilingual Language Distribution
            </h3>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Auto-detected conversation language
            </span>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {(!performanceData?.language_distribution || performanceData.language_distribution.length === 0) ? (
              <div style={{ color: 'var(--text-muted)', fontSize: '0.8125rem' }}>No conversations recorded in window.</div>
            ) : (
              performanceData.language_distribution.map((lang) => (
                <div
                  key={lang.code}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '10px 14px',
                    borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-glass-hover)',
                    fontSize: '0.8125rem',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{lang.language}</span>
                    <span style={{ fontSize: '0.6875rem', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                      ({lang.code})
                    </span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                      {lang.query_count} queries
                    </span>
                    <Badge variant="cyan" size="sm">
                      {lang.percentage}%
                    </Badge>
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>

        {/* Cryptographic Evidence & Verification Telemetry */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <FileTextIcon size={16} style={{ color: 'var(--accent-cyan)' }} />
              Cryptographic Evidence & Reconciliation
            </h3>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Document Verification Engine
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '16px' }}>
            <div
              style={{
                padding: '12px 14px',
                borderRadius: 'var(--radius-lg)',
                background: 'var(--surface-glass-hover)',
              }}
            >
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
                Total Uploads
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: '2px' }}>
                {performanceData?.evidence_analytics?.total_uploaded ?? 0}
              </div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--accent-cyan)' }}>Dossier attachments</div>
            </div>

            <div
              style={{
                padding: '12px 14px',
                borderRadius: 'var(--radius-lg)',
                background: 'var(--surface-glass-hover)',
              }}
            >
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
                Verified Claims
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--accent-emerald)', marginTop: '2px' }}>
                {performanceData?.evidence_analytics?.verified_count ?? 0}
              </div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--accent-emerald)' }}>OCR & Metadata validated</div>
            </div>

            <div
              style={{
                padding: '12px 14px',
                borderRadius: 'var(--radius-lg)',
                background: 'var(--surface-glass-hover)',
              }}
            >
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
                Clean Matches
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--accent-primary)', marginTop: '2px' }}>
                {(performanceData?.evidence_analytics?.verified_count ?? 0) - (performanceData?.evidence_analytics?.conflict_count ?? 0)}
              </div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Ledger corroborated</div>
            </div>

            <div
              style={{
                padding: '12px 14px',
                borderRadius: 'var(--radius-lg)',
                background: 'var(--surface-glass-hover)',
              }}
            >
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
                Reconciliation Conflicts
              </div>
              <div
                style={{
                  fontSize: '1.25rem',
                  fontWeight: 700,
                  color: (performanceData?.evidence_analytics?.conflict_count ?? 0) > 0 ? 'var(--accent-rose)' : 'var(--accent-emerald)',
                  marginTop: '2px',
                }}
              >
                {performanceData?.evidence_analytics?.conflict_count ?? 0}
              </div>
              <div style={{ fontSize: '0.6875rem', color: (performanceData?.evidence_analytics?.conflict_count ?? 0) > 0 ? 'var(--accent-rose)' : 'var(--text-muted)' }}>
                {(performanceData?.evidence_analytics?.conflict_count ?? 0) > 0 ? 'Discrepancy flagged' : 'Clean audit'}
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Link
              to="/evidence"
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                color: 'var(--accent-cyan)',
                textDecoration: 'none',
              }}
            >
              Open Cryptographic Evidence Audit Center →
            </Link>
          </div>
        </Card>
      </div>

      {/* =========================================================================
          STAFF ONLY: DEPARTMENT WORKLOAD & CAPACITY UTILIZATION
          ========================================================================= */}
      {isStaff && workloads.length > 0 && (
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <DatabaseIcon size={16} style={{ color: 'var(--accent-primary)' }} />
                Department Capacity & Queue Saturation (Staff Operations)
              </h3>
              <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                Active ticket assignments measured against team concurrent handling capacity
              </p>
            </div>
            <Badge variant="cyan" size="sm">SUPERVISOR VIEW</Badge>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '16px' }}>
            {workloads.map((dept: DepartmentWorkloadItem) => {
              const util = dept.utilization_pct;
              const isOverloaded = util >= 85;
              const isWarning = util >= 70;
              const barColor = isOverloaded ? 'var(--accent-rose)' : isWarning ? 'var(--accent-amber)' : 'var(--accent-emerald)';
              const badgeVariant: BadgeVariant = isOverloaded ? 'danger' : isWarning ? 'warning' : 'success';

              return (
                <div
                  key={dept.department}
                  style={{
                    padding: '14px 16px',
                    borderRadius: 'var(--radius-lg)',
                    background: 'var(--surface-glass-hover)',
                    border: '1px solid var(--border-subtle)',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                    <span style={{ fontWeight: 600, color: 'var(--text-primary)', fontSize: '0.8125rem' }}>
                      {dept.department}
                    </span>
                    <Badge variant={badgeVariant} size="sm">
                      {util}% Load
                    </Badge>
                  </div>

                  <div style={{ height: '6px', borderRadius: '3px', background: 'var(--surface-glass-base)', overflow: 'hidden', marginBottom: '8px' }}>
                    <div style={{ width: `${Math.min(100, util)}%`, height: '100%', background: barColor }} />
                  </div>

                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                    <span>Active: <strong style={{ color: 'var(--text-primary)' }}>{dept.active_tickets}</strong></span>
                    <span>Capacity: {dept.total_capacity}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
};
