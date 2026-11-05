import React from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';

export const AnalyticsView: React.FC = () => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
            Platform Analytics & SLA Performance
          </h1>
          <Badge variant="warning" size="sm">Contract Simulated — Backend Task 6 Milestone Day 15</Badge>
        </div>
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
          Real-time metrics, resolution efficiency, language breakdown, and agent workload distribution.
        </p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '24px' }}>
        {/* SLA Compliance by Category */}
        <Card variant="base">
          <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
            SLA Compliance by Category
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8125rem', marginBottom: '4px' }}>
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>Hardware Support</span>
                <span style={{ color: 'var(--accent-emerald)', fontWeight: 700 }}>98.2%</span>
              </div>
              <div style={{ height: '8px', borderRadius: '4px', background: 'var(--surface-glass-hover)', overflow: 'hidden' }}>
                <div style={{ width: '98.2%', height: '100%', background: 'var(--accent-emerald)' }} />
              </div>
            </div>

            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8125rem', marginBottom: '4px' }}>
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>Returns & Refunds</span>
                <span style={{ color: 'var(--accent-amber)', fontWeight: 700 }}>92.5%</span>
              </div>
              <div style={{ height: '8px', borderRadius: '4px', background: 'var(--surface-glass-hover)', overflow: 'hidden' }}>
                <div style={{ width: '92.5%', height: '100%', background: 'var(--accent-amber)' }} />
              </div>
            </div>

            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8125rem', marginBottom: '4px' }}>
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>Billing Inquiries</span>
                <span style={{ color: 'var(--accent-cyan)', fontWeight: 700 }}>99.1%</span>
              </div>
              <div style={{ height: '8px', borderRadius: '4px', background: 'var(--surface-glass-hover)', overflow: 'hidden' }}>
                <div style={{ width: '99.1%', height: '100%', background: 'var(--accent-cyan)' }} />
              </div>
            </div>
          </div>
        </Card>

        {/* Multilingual Traffic Breakdown */}
        <Card variant="base">
          <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '16px' }}>
            Multilingual Language Distribution
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', fontSize: '0.8125rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>English (en)</span>
              <span style={{ fontWeight: 700, color: 'var(--accent-primary)' }}>64% (1,240 queries)</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Spanish (es)</span>
              <span style={{ fontWeight: 700, color: 'var(--accent-cyan)' }}>18% (350 queries)</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Hindi (hi - Devanagari & Hinglish)</span>
              <span style={{ fontWeight: 700, color: 'var(--accent-purple)' }}>10% (195 queries)</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>French & German (fr/de)</span>
              <span style={{ fontWeight: 700, color: 'var(--accent-emerald)' }}>8% (155 queries)</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
};
