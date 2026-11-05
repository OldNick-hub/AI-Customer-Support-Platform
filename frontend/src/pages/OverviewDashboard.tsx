import React from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import {
  TicketIcon,
  AlertTriangleIcon,
  ClockIcon,
  MessageSquareIcon,
} from '../components/common/Icons';

export const OverviewDashboard: React.FC = () => {
  const kpiMetrics = [
    {
      title: 'Open Tickets',
      value: '24',
      sub: '4 awaiting customer response',
      badge: '+3 today',
      badgeVariant: 'primary' as const,
      icon: <TicketIcon size={20} />,
      glow: 'none' as const,
    },
    {
      title: 'Urgent & Escalated',
      value: '3',
      sub: 'Requires immediate supervisor review',
      badge: 'Critical',
      badgeVariant: 'danger' as const,
      icon: <AlertTriangleIcon size={20} />,
      glow: 'rose' as const,
    },
    {
      title: 'SLA Breach Warnings',
      value: '2',
      sub: 'Passed 75% operational window',
      badge: 'Warning',
      badgeVariant: 'warning' as const,
      icon: <ClockIcon size={20} />,
      glow: 'none' as const,
    },
    {
      title: 'Active Dialogues',
      value: '18',
      sub: 'Multi-turn RAG streaming',
      badge: 'Live',
      badgeVariant: 'cyan' as const,
      icon: <MessageSquareIcon size={20} />,
      glow: 'blue' as const,
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '28px' }}>
      {/* Header Banner */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
              Operations Command Center
            </h1>
            <Badge variant="cyan" size="sm">Backend API: Live Health Probe Connected</Badge>
          </div>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Real-time telemetry, conversational RAG chat, SLA watchdogs & risk escalation. (Aggregated ticket metrics simulated pending Day 12).
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <Button variant="secondary" size="sm">Export Report</Button>
          <Button variant="primary" size="sm">New Ticket</Button>
        </div>
      </div>

      {/* KPI Cards Grid */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
          gap: '20px',
        }}
      >
        {kpiMetrics.map((kpi, idx) => (
          <Card key={idx} glow={kpi.glow} style={{ position: 'relative', overflow: 'hidden' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-muted)' }}>
                {kpi.title}
              </span>
              <div
                style={{
                  width: '36px',
                  height: '36px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-glass-elevated)',
                  border: 'var(--border-subtle)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: 'var(--text-primary)',
                }}
              >
                {kpi.icon}
              </div>
            </div>

            <div style={{ fontSize: '2rem', fontWeight: 700, margin: '14px 0 6px', color: 'var(--text-primary)', letterSpacing: '-0.03em' }}>
              {kpi.value}
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '10px' }}>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{kpi.sub}</span>
              <Badge variant={kpi.badgeVariant} size="sm">{kpi.badge}</Badge>
            </div>
          </Card>
        ))}
      </div>

      {/* Main Split Analytics Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '24px' }}>
        {/* SVG Resolution & Volume Trend */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                Resolution Trend & Conversation Volume
              </h3>
              <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Hourly breakdown of automated RAG resolutions vs. escalated cases
              </p>
            </div>
            <Badge variant="success" dot>94.2% AI Resolved</Badge>
          </div>

          {/* Clean Vector SVG Chart */}
          <div style={{ width: '100%', height: '200px', position: 'relative' }}>
            <svg width="100%" height="100%" viewBox="0 0 500 180" preserveAspectRatio="none" style={{ overflow: 'visible' }}>
              {/* Horizontal grid lines */}
              <line x1="0" y1="30" x2="500" y2="30" stroke="var(--chart-grid)" strokeDasharray="4 4" />
              <line x1="0" y1="80" x2="500" y2="80" stroke="var(--chart-grid)" strokeDasharray="4 4" />
              <line x1="0" y1="130" x2="500" y2="130" stroke="var(--chart-grid)" strokeDasharray="4 4" />

              {/* Area gradient */}
              <defs>
                <linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--chart-line-blue)" stopOpacity="0.25" />
                  <stop offset="100%" stopColor="var(--chart-line-blue)" stopOpacity="0.0" />
                </linearGradient>
              </defs>

              {/* Area path */}
              <path
                d="M 0 140 Q 60 110, 120 70 T 240 90 T 360 40 T 500 60 L 500 180 L 0 180 Z"
                fill="url(#areaGradient)"
              />

              {/* Trend line */}
              <path
                d="M 0 140 Q 60 110, 120 70 T 240 90 T 360 40 T 500 60"
                fill="none"
                stroke="var(--chart-line-blue)"
                strokeWidth="3"
                strokeLinecap="round"
              />

              {/* Escalated line */}
              <path
                d="M 0 160 Q 60 150, 120 140 T 240 145 T 360 150 T 500 140"
                fill="none"
                stroke="var(--chart-line-rose)"
                strokeWidth="2"
                strokeDasharray="4 4"
              />
            </svg>
          </div>

          <div style={{ display: 'flex', gap: '20px', marginTop: '16px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: '10px', height: '3px', backgroundColor: 'var(--chart-line-blue)', borderRadius: '2px' }} />
              Automated RAG Resolution
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: '10px', height: '3px', backgroundColor: 'var(--chart-line-rose)', borderRadius: '2px' }} />
              Escalated to Staff
            </span>
          </div>
        </Card>

        {/* Real-time AI Sentiment Insights */}
        <Card variant="base">
          <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
            AI Sentiment Trajectory
          </h3>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ padding: '12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>Positive / Satisfied</span>
                <span style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--accent-emerald)' }}>68%</span>
              </div>
              <div style={{ height: '6px', borderRadius: '3px', background: 'var(--border-subtle)', overflow: 'hidden' }}>
                <div style={{ width: '68%', height: '100%', background: 'var(--accent-emerald)' }} />
              </div>
            </div>

            <div style={{ padding: '12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>Neutral Inquiry</span>
                <span style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--accent-cyan)' }}>22%</span>
              </div>
              <div style={{ height: '6px', borderRadius: '3px', background: 'var(--border-subtle)', overflow: 'hidden' }}>
                <div style={{ width: '22%', height: '100%', background: 'var(--accent-cyan)' }} />
              </div>
            </div>

            <div style={{ padding: '12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                <span style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>Frustrated / Escalated</span>
                <span style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--accent-rose)' }}>10%</span>
              </div>
              <div style={{ height: '6px', borderRadius: '3px', background: 'var(--border-subtle)', overflow: 'hidden' }}>
                <div style={{ width: '10%', height: '100%', background: 'var(--accent-rose)' }} />
              </div>
            </div>
          </div>

          <div style={{ marginTop: '16px', padding: '10px', borderRadius: 'var(--radius-md)', background: 'var(--accent-amber-light)', border: '1px solid rgba(245, 158, 11, 0.25)', fontSize: '0.75rem', color: 'var(--accent-amber)' }}>
            <strong>Watchdog Alert:</strong> 1 conversation exhibiting negative trajectory slope ($m &gt; 0.6$).
          </div>
        </Card>
      </div>

      {/* Critical Active Cases Table */}
      <Card variant="base">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              Urgent & Critical Escalations
            </h3>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Cases prioritized by dynamic priority scoring and calendar SLA deadlines
            </p>
          </div>
          <Badge variant="danger">Requires Immediate Action</Badge>
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.8125rem' }}>
            <thead>
              <tr style={{ borderBottom: 'var(--border-subtle)', color: 'var(--text-muted)' }}>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>TICKET ID</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>CUSTOMER</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>CATEGORY</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>PRIORITY</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>SLA TIME REMAINING</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>TRIGGER CONDITION</th>
                <th style={{ padding: '10px 12px', fontWeight: 600 }}>ACTION</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: 'var(--border-subtle)' }}>
                <td style={{ padding: '12px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>
                  TCK-202611-00042
                </td>
                <td style={{ padding: '12px' }}>
                  <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>John Doe</div>
                  <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Tier: VIP</div>
                </td>
                <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>HARDWARE</td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="danger" size="sm">CRITICAL (4.8)</Badge>
                </td>
                <td style={{ padding: '12px', color: 'var(--accent-rose)', fontWeight: 600 }}>
                  28m remaining
                </td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="warning" size="sm">SENTIMENT_FRUSTRATION</Badge>
                </td>
                <td style={{ padding: '12px' }}>
                  <Button variant="secondary" size="sm">Inspect</Button>
                </td>
              </tr>
              <tr style={{ borderBottom: 'var(--border-subtle)' }}>
                <td style={{ padding: '12px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>
                  TCK-202611-00039
                </td>
                <td style={{ padding: '12px' }}>
                  <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Sarah Connor</div>
                  <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Tier: ENTERPRISE</div>
                </td>
                <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>RETURNS</td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="warning" size="sm">HIGH (3.9)</Badge>
                </td>
                <td style={{ padding: '12px', color: 'var(--accent-amber)', fontWeight: 600 }}>
                  1h 14m remaining
                </td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="danger" size="sm">HIGH_RISK_SAFETY</Badge>
                </td>
                <td style={{ padding: '12px' }}>
                  <Button variant="secondary" size="sm">Inspect</Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};
