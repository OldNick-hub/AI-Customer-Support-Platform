import React from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { ShieldCheckIcon, ClockIcon } from '../components/common/Icons';

export const SettingsView: React.FC = () => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
          Platform Settings & Governance
        </h1>
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
          Role-based access control (RBAC), SLA operational calendar, business hours & on-call routing.
        </p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '24px' }}>
        {/* SLA Calendar Configuration */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <ClockIcon size={18} style={{ color: 'var(--accent-primary)' }} />
              <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>Operational SLA Calendar</h3>
            </div>
            <Badge variant="primary" size="sm">Active</Badge>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', fontSize: '0.8125rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ color: 'var(--text-muted)' }}>Calendar Name:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>US_COMMERCIAL_HOURS</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ color: 'var(--text-muted)' }}>Operational Timezone:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>America/New_York (EST/EDT)</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ color: 'var(--text-muted)' }}>Business Hours:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>09:00:00 – 17:00:00 (Mon – Fri)</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ color: 'var(--text-muted)' }}>Observed Holidays:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>6 federal holidays registered</span>
            </div>
          </div>
        </Card>

        {/* Enterprise RBAC Overview */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <ShieldCheckIcon size={18} style={{ color: 'var(--accent-emerald)' }} />
              <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>RBAC Role Separation</h3>
            </div>
            <Badge variant="success" size="sm">Enforced</Badge>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', fontSize: '0.8125rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>ADMIN</span>
              <span style={{ color: 'var(--text-muted)' }}>system:*, knowledge:*, tickets:*, users:*</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>SUPERVISOR</span>
              <span style={{ color: 'var(--text-muted)' }}>tickets:manage, knowledge:rollback, audit:read</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>AGENT</span>
              <span style={{ color: 'var(--text-muted)' }}>tickets:view, tickets:update, chat:respond</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>AUDITOR</span>
              <span style={{ color: 'var(--text-muted)' }}>audit:read, compliance:audit</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
};
