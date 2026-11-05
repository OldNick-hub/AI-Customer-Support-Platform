import React, { useState } from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { ClockIcon } from '../components/common/Icons';

export const TicketManagementView: React.FC = () => {
  const [filter, setFilter] = useState<'ALL' | 'OPEN' | 'PENDING' | 'ESCALATED'>('ALL');

  const tickets = [
    {
      id: 'TCK-202611-00042',
      title: 'Cracked screen on delivery (Dell XPS 15)',
      customer: 'John Doe',
      tier: 'VIP',
      priority: 'CRITICAL',
      score: 4.85,
      status: 'OPEN',
      remainingMin: 28,
      paused: false,
      category: 'HARDWARE',
    },
    {
      id: 'TCK-202611-00043',
      title: 'Awaiting customer serial number for warranty claim',
      customer: 'Sarah Connor',
      tier: 'ENTERPRISE',
      priority: 'HIGH',
      score: 3.75,
      status: 'PENDING_CUSTOMER',
      remainingMin: 180,
      paused: true,
      category: 'WARRANTY',
    },
    {
      id: 'TCK-202611-00044',
      title: 'Billing discrepancy on invoice #BB-992014',
      customer: 'Michael Scott',
      tier: 'STANDARD',
      priority: 'MEDIUM',
      score: 2.50,
      status: 'OPEN',
      remainingMin: 140,
      paused: false,
      category: 'BILLING',
    },
    {
      id: 'TCK-202611-00045',
      title: 'Extreme anger over delayed refund on return',
      customer: 'Alex Rivera',
      tier: 'STANDARD',
      priority: 'CRITICAL',
      score: 4.90,
      status: 'ESCALATED',
      remainingMin: 15,
      paused: false,
      category: 'RETURNS',
    },
  ];

  const filtered = tickets.filter((t) => {
    if (filter === 'OPEN') return t.status === 'OPEN';
    if (filter === 'PENDING') return t.status === 'PENDING_CUSTOMER';
    if (filter === 'ESCALATED') return t.status === 'ESCALATED';
    return true;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
              Ticket Command Queue
            </h1>
            <Badge variant="warning" size="sm">Contract Simulated — Backend Task 3 Milestone Day 12</Badge>
          </div>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Calendar-aware SLA tracking, dynamic priority scores & PENDING_CUSTOMER pause handling.
          </p>
        </div>

        {/* Filter Pills */}
        <div style={{ display: 'flex', gap: '8px', background: 'var(--surface-glass-base)', padding: '4px', borderRadius: 'var(--radius-md)', border: 'var(--border-subtle)' }}>
          {(['ALL', 'OPEN', 'PENDING', 'ESCALATED'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              style={{
                padding: '6px 14px',
                borderRadius: 'var(--radius-sm)',
                fontSize: '0.75rem',
                fontWeight: 600,
                border: 'none',
                cursor: 'pointer',
                background: filter === f ? 'var(--accent-primary)' : 'transparent',
                color: filter === f ? '#ffffff' : 'var(--text-secondary)',
                transition: 'all var(--transition-fast)',
              }}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* Ticket Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: '20px' }}>
        {filtered.map((t) => (
          <Card key={t.id} variant="base">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
              <div>
                <span style={{ fontSize: '0.75rem', fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--accent-primary)' }}>
                  {t.id}
                </span>
                <span style={{ margin: '0 6px', color: 'var(--text-muted)' }}>•</span>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{t.category}</span>
              </div>
              <Badge variant={t.priority === 'CRITICAL' ? 'danger' : t.priority === 'HIGH' ? 'warning' : 'neutral'} size="sm">
                {t.priority} ({t.score.toFixed(2)})
              </Badge>
            </div>

            <h3 style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '8px', lineHeight: 1.4 }}>
              {t.title}
            </h3>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '14px 0', padding: '10px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <div>
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>{t.customer}</div>
                <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Tier: {t.tier}</div>
              </div>

              {/* SLA Countdown pill */}
              <div style={{ textAlign: 'right' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '0.8125rem', fontWeight: 700, color: t.paused ? 'var(--accent-cyan)' : t.remainingMin < 30 ? 'var(--accent-rose)' : 'var(--accent-amber)' }}>
                  <ClockIcon size={14} />
                  <span>{t.remainingMin}m</span>
                </div>
                <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                  {t.paused ? 'SLA Paused (Customer)' : 'Until SLA Breach'}
                </div>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: 'var(--border-subtle)', paddingTop: '12px' }}>
              <Badge variant={t.status === 'ESCALATED' ? 'danger' : t.status === 'PENDING_CUSTOMER' ? 'warning' : 'primary'} size="sm">
                {t.status}
              </Badge>
              <div style={{ display: 'flex', gap: '8px' }}>
                <Button variant="secondary" size="sm">Handoff Brief</Button>
                <Button variant="primary" size="sm">Takeover</Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
};
