import React from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';


export const KnowledgeOpsView: React.FC = () => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
            Knowledge Base & FAISS Operations
          </h1>
          <Badge variant="warning" size="sm">Contract Simulated — Backend Task 5 Milestone Day 14</Badge>
        </div>
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
          Vector index generation, maintenance-window promotion, golden benchmark gates & rollback.
        </p>
      </div>

      {/* Index Status Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '20px' }}>
        <Card variant="highlight">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)' }}>ACTIVE GENERATION</span>
              <div style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: '4px' }}>
                faiss_index_v1
              </div>
            </div>
            <Badge variant="success" dot>Live Production</Badge>
          </div>
          <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', marginTop: '12px' }}>
            250 Chunks • 768-dim Vector Mapping • 100% Inverted File Loaded
          </div>
        </Card>

        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)' }}>MAINTENANCE WINDOW</span>
              <div style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--accent-primary)', marginTop: '4px' }}>
                02:00 – 04:00 UTC
              </div>
            </div>
            <Badge variant="cyan">Scheduled</Badge>
          </div>
          <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', marginTop: '12px' }}>
            Next Promotion Window in 2 hours 15 minutes
          </div>
        </Card>
      </div>

      {/* Benchmark Scorecard & Emergency Rollback */}
      <Card variant="base">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>Golden Benchmark Quality Gates</h3>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Automated test suite evaluated prior to candidate index promotion</p>
          </div>
          <Button variant="danger" size="sm">Emergency Rollback</Button>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
          <div style={{ padding: '16px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Semantic Accuracy</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--accent-emerald)', marginTop: '4px' }}>96.4%</div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '2px' }}>Target: $\ge 92.0\%$</div>
          </div>

          <div style={{ padding: '16px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Retrieval Latency</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--accent-cyan)', marginTop: '4px' }}>142 ms</div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '2px' }}>Target: $\le 200$ ms</div>
          </div>

          <div style={{ padding: '16px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)' }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Hallucination Refusal</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--accent-emerald)', marginTop: '4px' }}>100%</div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', marginTop: '2px' }}>Target: 100% on out-of-scope</div>
          </div>
        </div>
      </Card>
    </div>
  );
};
