import React, { useState } from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { FileTextIcon } from '../components/common/Icons';

export const EvidenceProcessingView: React.FC = () => {
  const [dragActive, setDragActive] = useState(false);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
            Multimodal Evidence Vault
          </h1>
          <Badge variant="warning" size="sm">Contract Simulated — Backend Task 4 Milestone Day 13</Badge>
        </div>
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
          MIME-validated persistent staging, synchronous 3s fast-path OCR, and claim verification.
        </p>
      </div>

      {/* Drag & Drop Upload Zone */}
      <Card
        variant="base"
        style={{
          border: dragActive ? '2px dashed var(--accent-primary)' : '2px dashed var(--border-card)',
          borderRadius: 'var(--radius-xl)',
          padding: '40px',
          textAlign: 'center',
          background: dragActive ? 'var(--accent-primary-light)' : 'var(--surface-glass-card)',
          cursor: 'pointer',
          transition: 'all var(--transition-fast)',
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragActive(false);
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px' }}>
          <div
            style={{
              width: '48px',
              height: '48px',
              borderRadius: '50%',
              background: 'var(--surface-glass-elevated)',
              border: 'var(--border-subtle)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--accent-primary)',
            }}
          >
            <FileTextIcon size={24} />
          </div>
          <div style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
            Drag & Drop Invoices, Receipts, or Photos here
          </div>
          <div style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>
            Supported formats: PDF, PNG, JPG, WEBP (Max 20MB) • Magic-byte validated
          </div>
          <Button variant="secondary" size="sm" style={{ marginTop: '8px' }}>
            Browse Local Files
          </Button>
        </div>
      </Card>

      {/* Split Inspector of Processed Evidence */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>Document Preview</h3>
            <Badge variant="success" size="sm">Fast-Path 200 OK (1.8s)</Badge>
          </div>

          <div
            style={{
              height: '240px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--surface-glass-hover)',
              border: 'var(--border-subtle)',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              color: 'var(--text-muted)',
            }}
          >
            <FileTextIcon size={40} style={{ color: 'var(--accent-cyan)' }} />
            <div style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-primary)' }}>bestbuy_receipt_1026.pdf</div>
            <div style={{ fontSize: '0.75rem' }}>1.4 MB • SHA256: d8f1a9...c4b2</div>
          </div>
        </Card>

        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>OCR Extraction & Claim Check</h3>
            <Badge variant="warning" size="sm">Conflict Detected</Badge>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', fontSize: '0.8125rem' }}>
            <div style={{ padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: 'var(--text-muted)' }}>Detected Vendor:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Best Buy US Retail Store #482</span>
            </div>

            <div style={{ padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: 'var(--text-muted)' }}>Purchase Date:</span>
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>2026-10-15 (21 days ago)</span>
            </div>

            <div style={{ padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: 'var(--text-muted)' }}>Invoice Total:</span>
              <span style={{ fontWeight: 700, color: 'var(--accent-primary)' }}>$899.99 USD</span>
            </div>

            <div style={{ padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--accent-rose-light)', border: '1px solid rgba(244, 63, 94, 0.25)', color: 'var(--accent-rose)' }}>
              <strong>Claim Discrepancy:</strong> Customer claimed refund of $1,200.00 does not match receipt total ($899.99).
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
};
