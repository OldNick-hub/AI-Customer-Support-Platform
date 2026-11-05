import React from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { AlertTriangleIcon, ShieldCheckIcon } from '../components/common/Icons';

export const EscalationCenterView: React.FC = () => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
            Risk Escalation & On-Call Center
          </h1>
          <Badge variant="warning" size="sm">Contract Simulated — Backend Task 5 Milestone Day 08/09</Badge>
        </div>
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
          Automated sentiment slope detection, legal threats, safety triggers and after-hours paging.
        </p>
      </div>

      {/* On-Call & Operational Status Banner */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '20px' }}>
        <Card variant="highlight" style={{ borderLeft: '4px solid var(--accent-rose)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <AlertTriangleIcon size={22} style={{ color: 'var(--accent-rose)' }} />
              <div>
                <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>High-Risk Safety Alarms</h3>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>1 active case under immediate supervisory hold</p>
              </div>
            </div>
            <Badge variant="danger" dot>Active</Badge>
          </div>
        </Card>

        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <ShieldCheckIcon size={22} style={{ color: 'var(--accent-emerald)' }} />
              <div>
                <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>On-Call Rotation Roster</h3>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>2 agents flagged is_on_call=True for after-hours</p>
              </div>
            </div>
            <Badge variant="success">Operational</Badge>
          </div>
        </Card>
      </div>

      {/* Escalation Audit Table */}
      <Card variant="base">
        <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
          Deterministic Escalation Audits (Immutable Trail)
        </h3>

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.8125rem' }}>
            <thead>
              <tr style={{ borderBottom: 'var(--border-subtle)', color: 'var(--text-muted)' }}>
                <th style={{ padding: '10px 12px' }}>TICKET ID</th>
                <th style={{ padding: '10px 12px' }}>TRIGGER CONDITION</th>
                <th style={{ padding: '10px 12px' }}>PREV STATE &gt; NEW STATE</th>
                <th style={{ padding: '10px 12px' }}>ESCALATION REASON</th>
                <th style={{ padding: '10px 12px' }}>TIMESTAMP</th>
                <th style={{ padding: '10px 12px' }}>REVIEWER</th>
                <th style={{ padding: '10px 12px' }}>ACTION</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: 'var(--border-subtle)' }}>
                <td style={{ padding: '12px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>
                  TCK-202611-00042
                </td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="danger" size="sm">HIGH_RISK_SAFETY</Badge>
                </td>
                <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>
                  OPEN &gt; ESCALATED
                </td>
                <td style={{ padding: '12px', color: 'var(--text-primary)' }}>
                  Customer mentioned battery thermal expansion and sparking charger.
                </td>
                <td style={{ padding: '12px', color: 'var(--text-muted)' }}>
                  2026-11-05 22:40 UTC
                </td>
                <td style={{ padding: '12px', color: 'var(--accent-primary)', fontWeight: 600 }}>
                  Automated Engine
                </td>
                <td style={{ padding: '12px' }}>
                  <Button variant="danger" size="sm">Acknowledge</Button>
                </td>
              </tr>
              <tr style={{ borderBottom: 'var(--border-subtle)' }}>
                <td style={{ padding: '12px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>
                  TCK-202611-00038
                </td>
                <td style={{ padding: '12px' }}>
                  <Badge variant="warning" size="sm">SENTIMENT_FRUSTRATION</Badge>
                </td>
                <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>
                  OPEN &gt; ESCALATED
                </td>
                <td style={{ padding: '12px', color: 'var(--text-primary)' }}>
                  Frustration index exceeded 0.85 across two consecutive turns.
                </td>
                <td style={{ padding: '12px', color: 'var(--text-muted)' }}>
                  2026-11-05 21:15 UTC
                </td>
                <td style={{ padding: '12px', color: 'var(--text-secondary)' }}>
                  Supervisor On-Duty
                </td>
                <td style={{ padding: '12px' }}>
                  <Button variant="secondary" size="sm">View Trail</Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};
