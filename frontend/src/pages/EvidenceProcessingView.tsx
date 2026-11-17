import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import {
  FileTextIcon,
  AlertTriangleIcon,
  ShieldCheckIcon,
  ClockIcon,
  SearchIcon,
} from '../components/common/Icons';
import { useAuth } from '../context/AuthContext';
import {
  evidenceApi,
  reconciliationApi,
  conversationsApi,
  type EvidenceUploadResponse,
  type EvidenceExtractionResponse,
  type ReconciliationResponse,
  type PurgeResult,
  type StructuredInvoiceData,
  type ConversationListItem,
} from '../services/api';

const MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024; // 15 MB
const ALLOWED_MIME_TYPES = ['application/pdf', 'image/png', 'image/jpeg', 'image/webp'];

export const EvidenceProcessingView: React.FC = () => {
  const { currentUser, activeSession } = useAuth();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Upload State
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [conversationId, setConversationId] = useState<string>('');
  const [ticketId, setTicketId] = useState<string>('');
  const [recentConversations, setRecentConversations] = useState<ConversationListItem[]>([]);
  const [isLoadingConversations, setIsLoadingConversations] = useState(false);
  const [isUploading, setIsUploading] = useState(false);

  // Evidence Inspection State
  const [currentEvidence, setCurrentEvidence] = useState<EvidenceUploadResponse | null>(null);
  const [extraction, setExtraction] = useState<EvidenceExtractionResponse | null>(null);
  const [isLoadingExtraction, setIsLoadingExtraction] = useState(false);
  const [lookupEvidenceId, setLookupEvidenceId] = useState<string>('');

  // Claim Reconciliation State
  const [claimText, setClaimText] = useState<string>(
    'Customer claims refund of ₹4,500 for damaged electronics package delivered on 2026-10-15.'
  );
  const [reconciliationResult, setReconciliationResult] = useState<ReconciliationResponse | null>(null);
  const [isReconciling, setIsReconciling] = useState(false);

  // Admin Retention Purge State
  const [purgeBatchSize, setPurgeBatchSize] = useState<number>(50);
  const [includeFailedPurges, setIncludeFailedPurges] = useState<boolean>(false);
  const [isPurging, setIsPurging] = useState(false);
  const [purgeResult, setPurgeResult] = useState<PurgeResult | null>(null);
  const [showPurgeModal, setShowPurgeModal] = useState(false);

  // Global Notification
  const [notification, setNotification] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);

  const isAdmin =
    currentUser?.roles?.some((r: string) => ['ADMIN', 'SUPER_ADMIN'].includes(r.toUpperCase())) ||
    activeSession?.roles?.some((r: string) => ['ADMIN', 'SUPER_ADMIN'].includes(r.toUpperCase())) ||
    false;

  // Load recent conversations for easy selection
  const loadRecentConversations = useCallback(async () => {
    try {
      setIsLoadingConversations(true);
      const res = await conversationsApi.listConversations({ limit: 5, offset: 0 });
      setRecentConversations(res.items || []);
      if (!conversationId && res.items && res.items.length > 0) {
        setConversationId(res.items[0].id);
      }
    } catch {
      // In local dev without conversations yet, keep manual entry open
    } finally {
      setIsLoadingConversations(false);
    }
  }, [conversationId]);

  useEffect(() => {
    loadRecentConversations();
  }, [loadRecentConversations]);

  // File Validation Helper
  const validateAndSetFile = (file: File) => {
    if (file.size > MAX_FILE_SIZE_BYTES) {
      setNotification({
        type: 'error',
        message: `File exceeds 15 MB limit (${(file.size / (1024 * 1024)).toFixed(2)} MB). Please upload a smaller file.`,
      });
      return;
    }
    const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
    const isAllowedExt = ['.pdf', '.png', '.jpg', '.jpeg', '.webp'].includes(ext);
    const isAllowedMime = ALLOWED_MIME_TYPES.includes(file.type) || isAllowedExt;

    if (!isAllowedMime) {
      setNotification({
        type: 'error',
        message: `Unsupported file type "${file.type || ext}". Only PDF, PNG, JPG, and WEBP files are allowed.`,
      });
      return;
    }

    setSelectedFile(file);
    setNotification({
      type: 'info',
      message: `Selected "${file.name}" (${(file.size / 1024).toFixed(1)} KB). Ready for upload.`,
    });
  };

  // Upload handler
  const handleUpload = async () => {
    if (!selectedFile) {
      setNotification({ type: 'error', message: 'Please select an invoice, receipt, or photo to upload.' });
      return;
    }
    if (!conversationId.trim()) {
      setNotification({ type: 'error', message: 'Target Conversation UUID is required for evidence intake.' });
      return;
    }

    try {
      setIsUploading(true);
      setNotification(null);

      const formData = new FormData();
      formData.append('file', selectedFile);
      formData.append('conversation_id', conversationId.trim());
      if (ticketId.trim()) {
        formData.append('ticket_id', ticketId.trim());
      }
      formData.append('sync_processing', 'true');

      const uploadRes = await evidenceApi.uploadEvidence(formData);
      setCurrentEvidence(uploadRes);
      setLookupEvidenceId(uploadRes.evidence_id);

      setNotification({
        type: 'success',
        message: `Evidence "${uploadRes.file_name}" uploaded successfully! Status: ${uploadRes.status}. SHA-256: ${uploadRes.sha256_hash.substring(0, 12)}...`,
      });

      // If extraction returned inline, set it
      if (uploadRes.structured_entities) {
        setExtraction({
          evidence_id: uploadRes.evidence_id,
          status: uploadRes.status,
          extracted_at: uploadRes.created_at,
          ocr_confidence: uploadRes.structured_entities.overall_confidence || 0.95,
          pii_redacted_text: uploadRes.structured_entities.pii_redacted_text || '',
          entities: uploadRes.structured_entities,
        });
      } else {
        // Fetch extraction endpoint
        await loadExtraction(uploadRes.evidence_id);
      }
    } catch (err: any) {
      const msg = err.response?.data?.detail?.message || err.response?.data?.message || err.message || 'Evidence upload failed.';
      setNotification({ type: 'error', message: `Upload error: ${msg}` });
    } finally {
      setIsUploading(false);
    }
  };

  // Load extraction details
  const loadExtraction = async (id: string) => {
    if (!id.trim()) return;
    try {
      setIsLoadingExtraction(true);
      const data = await evidenceApi.getEvidenceExtraction(id.trim());
      setExtraction(data);
    } catch (err: any) {
      const msg = err.response?.data?.detail?.message || err.response?.data?.message || err.message;
      setNotification({ type: 'error', message: `Could not retrieve OCR extraction: ${msg}` });
    } finally {
      setIsLoadingExtraction(false);
    }
  };

  // Direct lookup of existing evidence
  const handleLookup = async () => {
    if (!lookupEvidenceId.trim()) {
      setNotification({ type: 'error', message: 'Enter a valid Evidence UUID to inspect.' });
      return;
    }
    try {
      setIsLoadingExtraction(true);
      setNotification(null);
      const details = await evidenceApi.getEvidence(lookupEvidenceId.trim());
      setCurrentEvidence({
        evidence_id: details.evidence_id,
        conversation_id: details.conversation_id || '',
        customer_id: details.customer_id,
        file_name: details.file_name,
        mime_type: details.mime_type,
        file_size_bytes: details.file_size_bytes,
        sha256_hash: details.sha256_hash,
        status: details.status,
        is_malicious: details.is_malicious,
        quarantine_reason: details.quarantine_reason,
        retention_expires_at: details.retention_expires_at,
        created_at: details.created_at,
      });
      await loadExtraction(details.evidence_id);
      setNotification({ type: 'success', message: `Loaded evidence record ${details.evidence_id.substring(0, 8)}...` });
    } catch (err: any) {
      const msg = err.response?.data?.detail?.message || err.response?.data?.message || err.message || 'Lookup failed.';
      setNotification({ type: 'error', message: `Evidence lookup failed: ${msg}` });
    } finally {
      setIsLoadingExtraction(false);
    }
  };

  // Run claim reconciliation
  const handleReconcile = async () => {
    const targetEvidenceId = currentEvidence?.evidence_id || lookupEvidenceId.trim();
    if (!targetEvidenceId) {
      setNotification({ type: 'error', message: 'Upload or select an evidence record first before reconciling claims.' });
      return;
    }
    if (!conversationId.trim()) {
      setNotification({ type: 'error', message: 'Target Conversation UUID is required for reconciliation.' });
      return;
    }
    if (!claimText.trim()) {
      setNotification({ type: 'error', message: 'Please enter the customer assertion/claim text to verify.' });
      return;
    }

    try {
      setIsReconciling(true);
      setNotification(null);
      const res = await reconciliationApi.reconcile({
        conversation_id: conversationId.trim(),
        evidence_id: targetEvidenceId,
        claim_text: claimText.trim(),
        async_job: false,
      });
      setReconciliationResult(res);
      setNotification({
        type: 'success',
        message: `Claim reconciliation evaluated: ${res.overall_match_status}. Corroboration level: ${res.verification_level}.`,
      });
    } catch (err: any) {
      const msg = err.response?.data?.detail?.message || err.response?.data?.detail || err.message || 'Reconciliation failed.';
      setNotification({ type: 'error', message: `Reconciliation error: ${msg}` });
    } finally {
      setIsReconciling(false);
    }
  };

  // Admin retention purge execution
  const handleExecutePurge = async () => {
    try {
      setIsPurging(true);
      setShowPurgeModal(false);
      const result = await evidenceApi.purgeExpired(purgeBatchSize, includeFailedPurges);
      setPurgeResult(result);
      setNotification({
        type: 'success',
        message: `Retention purge batch complete. Purged: ${result.purged_count ?? 0} file(s). Status: ${result.status}.`,
      });
    } catch (err: any) {
      const msg = err.response?.data?.detail?.message || err.response?.data?.message || err.message;
      setNotification({ type: 'error', message: `Purge execution failed: ${msg}` });
    } finally {
      setIsPurging(false);
    }
  };

  const entities: StructuredInvoiceData | undefined = extraction?.entities;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      {/* Header & System Status */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '16px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <h1 style={{ fontSize: '1.625rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
              Multimodal Evidence Vault & Reconciliation
            </h1>
            <Badge variant="cyan" size="sm">Backend Integrated — Phase 5</Badge>
          </div>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            MIME-validated persistent intake (≤15MB), synchronous OCR, claim corroboration, and 30-day retention ceiling.
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Badge variant={activeSession?.auth_type === 'SESSION_TOKEN' ? 'warning' : 'primary'}>
            Account: {activeSession?.display_name || (currentUser?.roles && currentUser.roles[0]) || 'Active User'}
          </Badge>
          {isAdmin && <Badge variant="purple">Admin Controls Active</Badge>}
        </div>
      </div>

      {/* Global Notification Banner */}
      {notification && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            fontSize: '0.875rem',
            background:
              notification.type === 'error'
                ? 'var(--accent-rose-light)'
                : notification.type === 'success'
                ? 'var(--accent-emerald-light)'
                : 'var(--accent-primary-light)',
            border: `1px solid ${
              notification.type === 'error'
                ? 'rgba(244, 63, 94, 0.3)'
                : notification.type === 'success'
                ? 'rgba(16, 185, 129, 0.3)'
                : 'rgba(59, 130, 246, 0.3)'
            }`,
            color:
              notification.type === 'error'
                ? 'var(--accent-rose)'
                : notification.type === 'success'
                ? 'var(--accent-emerald)'
                : 'var(--accent-primary)',
          }}
        >
          <span>{notification.message}</span>
          <button
            onClick={() => setNotification(null)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              fontWeight: 700,
              marginLeft: '12px',
            }}
          >
            ✕
          </button>
        </div>
      )}

      {/* Top Architecture Invariant Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px' }}>
        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <FileTextIcon size={24} style={{ color: 'var(--accent-primary)' }} />
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Active Evidence Record</div>
              <div style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                {currentEvidence ? currentEvidence.file_name : 'No Evidence Loaded'}
              </div>
            </div>
          </div>
          {currentEvidence && (
            <div style={{ marginTop: '10px', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              UUID: {currentEvidence.evidence_id.substring(0, 18)}...
            </div>
          )}
        </Card>

        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <ShieldCheckIcon size={24} style={{ color: 'var(--accent-emerald)' }} />
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Financial Trust Hierarchy</div>
              <div style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                DOCUMENT_CORROBORATED
              </div>
            </div>
          </div>
          <div style={{ marginTop: '10px', fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>
            Uploaded documents corroborate customer claims, but refunds require external payment gateway verification.
          </div>
        </Card>

        <Card variant="base">
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <ClockIcon size={24} style={{ color: 'var(--accent-amber)' }} />
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Storage Lifecycle Policy</div>
              <div style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                30-Day Retention Ceiling
              </div>
            </div>
          </div>
          <div style={{ marginTop: '10px', fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>
            AES-256-GCM encrypted payloads auto-expire after 30 days. Purge enforces PostgreSQL SKIP LOCKED.
          </div>
        </Card>
      </div>

      {/* Evidence Intake & Context Configuration */}
      <Card variant="base">
        <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '14px' }}>
          Evidence Intake & Conversation Context
        </h3>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px', marginBottom: '16px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '6px' }}>
              Target Conversation UUID <span style={{ color: 'var(--accent-rose)' }}>*</span>
            </label>
            <input
              type="text"
              value={conversationId}
              onChange={(e) => setConversationId(e.target.value)}
              placeholder="e.g. 550e8400-e29b-41d4-a716-446655440000"
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--surface-glass-base)',
                border: 'var(--border-subtle)',
                color: 'var(--text-primary)',
                fontSize: '0.8125rem',
                outline: 'none',
              }}
            />
            {isLoadingConversations ? (
              <div style={{ marginTop: '6px', fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                Loading recent threads...
              </div>
            ) : recentConversations.length > 0 ? (
              <div style={{ marginTop: '6px', display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Recent:</span>
                {recentConversations.slice(0, 3).map((conv) => (
                  <button
                    key={conv.id}
                    onClick={() => setConversationId(conv.id)}
                    style={{
                      background: conversationId === conv.id ? 'var(--accent-primary-light)' : 'var(--surface-glass-hover)',
                      border: '1px solid var(--border-subtle)',
                      borderRadius: 'var(--radius-sm)',
                      padding: '2px 8px',
                      fontSize: '0.6875rem',
                      color: conversationId === conv.id ? 'var(--accent-primary)' : 'var(--text-secondary)',
                      cursor: 'pointer',
                    }}
                  >
                    {conv.id.substring(0, 8)}... ({conv.status})
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          <div>
            <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '6px' }}>
              Associated Ticket UUID (Optional)
            </label>
            <input
              type="text"
              value={ticketId}
              onChange={(e) => setTicketId(e.target.value)}
              placeholder="e.g. 123e4567-e89b-12d3-a456-426614174000"
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--surface-glass-base)',
                border: 'var(--border-subtle)',
                color: 'var(--text-primary)',
                fontSize: '0.8125rem',
                outline: 'none',
              }}
            />
          </div>
        </div>

        {/* Drag & Drop Upload Zone */}
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.png,.jpg,.jpeg,.webp"
          style={{ display: 'none' }}
          onChange={(e) => {
            if (e.target.files && e.target.files[0]) {
              validateAndSetFile(e.target.files[0]);
            }
          }}
        />

        <div
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            if (e.dataTransfer.files && e.dataTransfer.files[0]) {
              validateAndSetFile(e.dataTransfer.files[0]);
            }
          }}
          style={{
            border: dragActive ? '2px dashed var(--accent-primary)' : '2px dashed var(--border-card)',
            borderRadius: 'var(--radius-xl)',
            padding: '32px 20px',
            textAlign: 'center',
            background: dragActive ? 'var(--accent-primary-light)' : 'var(--surface-glass-base)',
            cursor: 'pointer',
            transition: 'all var(--transition-fast)',
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                width: '44px',
                height: '44px',
                borderRadius: '50%',
                background: 'var(--surface-glass-elevated)',
                border: 'var(--border-subtle)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--accent-primary)',
              }}
            >
              <FileTextIcon size={22} />
            </div>
            <div style={{ fontSize: '0.9375rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              {selectedFile ? `Selected: ${selectedFile.name}` : 'Drag & Drop Invoices, Receipts, or Photos here'}
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Supported: PDF (≤10 pages), PNG, JPG, WEBP (Max 15MB) • Signature magic-byte validated
            </div>
            {selectedFile && (
              <Badge variant="success" size="sm">
                Ready to Upload ({(selectedFile.size / 1024).toFixed(1)} KB)
              </Badge>
            )}
          </div>
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '16px', flexWrap: 'wrap', gap: '12px' }}>
          <div style={{ display: 'flex', gap: '8px' }}>
            <Button
              variant="primary"
              size="sm"
              loading={isUploading}
              disabled={!selectedFile || !conversationId}
              onClick={handleUpload}
            >
              {isUploading ? 'Uploading & Analyzing...' : 'Upload & Run Fast-Path OCR'}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={isUploading}
              onClick={() => fileInputRef.current?.click()}
            >
              Browse Local Files
            </Button>
          </div>

          {/* Existing Evidence Lookup Bar */}
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <input
              type="text"
              placeholder="Or inspect Evidence UUID..."
              value={lookupEvidenceId}
              onChange={(e) => setLookupEvidenceId(e.target.value)}
              style={{
                padding: '6px 10px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--surface-glass-base)',
                border: 'var(--border-subtle)',
                color: 'var(--text-primary)',
                fontSize: '0.75rem',
                width: '210px',
              }}
            />
            <Button
              variant="secondary"
              size="sm"
              loading={isLoadingExtraction}
              onClick={handleLookup}
            >
              <SearchIcon size={14} /> Fetch
            </Button>
          </div>
        </div>
      </Card>

      {/* Split Inspector: Processed Document & Structured OCR Extraction */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: '24px' }}>
        {/* Left: Document Metadata & Storage Details */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              Document Metadata & Vault Status
            </h3>
            {currentEvidence && (
              <Badge
                variant={
                  currentEvidence.status === 'VERIFIED'
                    ? 'success'
                    : currentEvidence.status === 'FLAGGED_UNSAFE'
                    ? 'danger'
                    : 'warning'
                }
                size="sm"
              >
                {currentEvidence.status}
              </Badge>
            )}
          </div>

          {currentEvidence ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', fontSize: '0.8125rem' }}>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>File Name:</span>
                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{currentEvidence.file_name}</span>
              </div>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>MIME Type:</span>
                <span style={{ color: 'var(--text-primary)' }}>{currentEvidence.mime_type}</span>
              </div>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Size:</span>
                <span style={{ color: 'var(--text-primary)' }}>
                  {(currentEvidence.file_size_bytes / 1024).toFixed(1)} KB
                </span>
              </div>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>SHA-256 Digest:</span>
                <span style={{ fontFamily: 'monospace', fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>
                  {currentEvidence.sha256_hash.substring(0, 16)}...
                </span>
              </div>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Retention Expires:</span>
                <span style={{ color: 'var(--accent-amber)', fontWeight: 600 }}>
                  {new Date(currentEvidence.retention_expires_at).toLocaleString()}
                </span>
              </div>

              {currentEvidence.is_malicious && (
                <div style={{ padding: '10px', borderRadius: 'var(--radius-md)', background: 'var(--accent-rose-light)', border: '1px solid rgba(244, 63, 94, 0.3)', color: 'var(--accent-rose)' }}>
                  <strong>Security Warning:</strong> {currentEvidence.quarantine_reason || 'File flagged unsafe by security analysis.'}
                </div>
              )}

              {/* Redacted OCR Text Preview */}
              {extraction?.pii_redacted_text && (
                <div style={{ marginTop: '8px' }}>
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '4px' }}>
                    PII-Redacted Extracted Text:
                  </div>
                  <pre
                    style={{
                      maxHeight: '140px',
                      overflowY: 'auto',
                      padding: '10px',
                      borderRadius: 'var(--radius-md)',
                      background: 'var(--surface-glass-base)',
                      border: 'var(--border-subtle)',
                      fontSize: '0.6875rem',
                      color: 'var(--text-muted)',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {extraction.pii_redacted_text}
                  </pre>
                </div>
              )}
            </div>
          ) : (
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
              <div style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                No Document Selected
              </div>
              <div style={{ fontSize: '0.75rem' }}>Upload a file or fetch an evidence ID above to view details</div>
            </div>
          )}
        </Card>

        {/* Right: Structured OCR Extraction & Entities */}
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              Structured OCR Extraction
            </h3>
            {extraction && (
              <Badge variant={extraction.ocr_confidence >= 0.8 ? 'success' : 'warning'} size="sm">
                OCR Confidence: {(extraction.ocr_confidence * 100).toFixed(0)}%
              </Badge>
            )}
          </div>

          {isLoadingExtraction ? (
            <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
              Extracting financial invoice entities...
            </div>
          ) : entities ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', fontSize: '0.8125rem' }}>
              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Merchant / Vendor:</span>
                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                  {entities.merchant_name || 'Not detected'}
                </span>
              </div>

              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Invoice / Date:</span>
                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                  {entities.invoice_date || entities.transaction_date || 'Undated'}
                </span>
              </div>

              <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: 'var(--surface-glass-hover)', display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Invoice Total:</span>
                <span style={{ fontWeight: 700, color: 'var(--accent-primary)', fontSize: '0.9375rem' }}>
                  {entities.total_amount != null
                    ? `${entities.currency || '₹'} ${entities.total_amount.toLocaleString()}`
                    : 'Unknown'}
                </span>
              </div>

              {/* Line Items Table */}
              {entities.line_items && entities.line_items.length > 0 && (
                <div style={{ marginTop: '8px' }}>
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '4px' }}>
                    Parsed Line Items ({entities.line_items.length}):
                  </div>
                  <div style={{ overflowX: 'auto', border: 'var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.6875rem' }}>
                      <thead>
                        <tr style={{ background: 'var(--surface-glass-hover)', borderBottom: 'var(--border-subtle)' }}>
                          <th style={{ padding: '6px 8px', textAlign: 'left', color: 'var(--text-muted)' }}>Item</th>
                          <th style={{ padding: '6px 8px', textAlign: 'center', color: 'var(--text-muted)' }}>Qty</th>
                          <th style={{ padding: '6px 8px', textAlign: 'right', color: 'var(--text-muted)' }}>Total</th>
                        </tr>
                      </thead>
                      <tbody>
                        {entities.line_items.map((item, idx) => (
                          <tr key={idx} style={{ borderBottom: 'var(--border-subtle)' }}>
                            <td style={{ padding: '6px 8px', color: 'var(--text-primary)' }}>{item.product_name}</td>
                            <td style={{ padding: '6px 8px', textAlign: 'center', color: 'var(--text-secondary)' }}>
                              {item.quantity ?? 1}
                            </td>
                            <td style={{ padding: '6px 8px', textAlign: 'right', fontWeight: 600, color: 'var(--text-primary)' }}>
                              ₹{item.total_price ?? item.unit_price ?? 0}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* Uncertain / Missing Fields Warnings */}
              {entities.missing_fields && entities.missing_fields.length > 0 && (
                <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap', alignItems: 'center', marginTop: '4px' }}>
                  <span style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>Missing:</span>
                  {entities.missing_fields.map((f, i) => (
                    <Badge key={i} variant="warning" size="sm">{f}</Badge>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
              No structured OCR extraction available. Upload a document or fetch an evidence record.
            </div>
          )}
        </Card>
      </div>

      {/* Claim Reconciliation Engine */}
      <Card variant="base">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              Intelligent Claim Reconciliation & Mismatch Detection
            </h3>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
              Compares conversational assertions against OCR entities under the financial trust hierarchy.
            </p>
          </div>
          <Badge variant="cyan" size="sm">Rule-Enforced Corroboration</Badge>
        </div>

        <div style={{ marginBottom: '16px' }}>
          <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '6px' }}>
            Customer Statement / Claim Text to Corroborate:
          </label>
          <textarea
            rows={2}
            value={claimText}
            onChange={(e) => setClaimText(e.target.value)}
            style={{
              width: '100%',
              padding: '10px 12px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--surface-glass-base)',
              border: 'var(--border-subtle)',
              color: 'var(--text-primary)',
              fontSize: '0.8125rem',
              outline: 'none',
              resize: 'vertical',
            }}
          />
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: '16px' }}>
          <Button
            variant="primary"
            size="sm"
            loading={isReconciling}
            disabled={!currentEvidence && !lookupEvidenceId}
            onClick={handleReconcile}
          >
            {isReconciling ? 'Reconciling Claims...' : 'Run Claim Reconciliation'}
          </Button>
        </div>

        {/* Reconciliation Results Section */}
        {reconciliationResult && (
          <div
            style={{
              padding: '16px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--surface-glass-hover)',
              border: 'var(--border-subtle)',
              display: 'flex',
              flexDirection: 'column',
              gap: '12px',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Badge
                  variant={
                    reconciliationResult.overall_match_status === 'MATCH'
                      ? 'success'
                      : reconciliationResult.overall_match_status === 'CONFLICT'
                      ? 'danger'
                      : 'warning'
                  }
                  size="md"
                >
                  Verdict: {reconciliationResult.overall_match_status}
                </Badge>
                <Badge variant="purple" size="md">
                  Hierarchy: {reconciliationResult.verification_level}
                </Badge>
              </div>

              {/* Strict Financial Hierarchy Indicator */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem' }}>
                <span style={{ color: 'var(--text-muted)' }}>Refund Authorization:</span>
                <Badge variant={reconciliationResult.is_refund_authorized ? 'success' : 'danger'} size="sm">
                  {reconciliationResult.is_refund_authorized ? 'Authorized' : 'Requires Payment Gateway Check'}
                </Badge>
              </div>
            </div>

            <div style={{ fontSize: '0.8125rem', color: 'var(--text-primary)', fontWeight: 500 }}>
              {reconciliationResult.summary}
            </div>

            {/* Field Comparisons Table */}
            {reconciliationResult.field_comparisons && reconciliationResult.field_comparisons.length > 0 && (
              <div style={{ overflowX: 'auto', marginTop: '6px' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.75rem' }}>
                  <thead>
                    <tr style={{ background: 'var(--surface-glass-elevated)', borderBottom: 'var(--border-subtle)' }}>
                      <th style={{ padding: '8px 10px', textAlign: 'left', color: 'var(--text-muted)' }}>Field</th>
                      <th style={{ padding: '8px 10px', textAlign: 'left', color: 'var(--text-muted)' }}>Extracted (Doc)</th>
                      <th style={{ padding: '8px 10px', textAlign: 'left', color: 'var(--text-muted)' }}>Claimed</th>
                      <th style={{ padding: '8px 10px', textAlign: 'center', color: 'var(--text-muted)' }}>Status</th>
                      <th style={{ padding: '8px 10px', textAlign: 'left', color: 'var(--text-muted)' }}>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reconciliationResult.field_comparisons.map((fc, i) => (
                      <tr key={i} style={{ borderBottom: 'var(--border-subtle)' }}>
                        <td style={{ padding: '8px 10px', fontWeight: 600, color: 'var(--text-primary)' }}>
                          {fc.field_name || fc.field || 'Field'}
                        </td>
                        <td style={{ padding: '8px 10px', color: 'var(--text-secondary)' }}>
                          {String(fc.extracted_value ?? '—')}
                        </td>
                        <td style={{ padding: '8px 10px', color: 'var(--text-secondary)' }}>
                          {String(fc.claimed_value ?? '—')}
                        </td>
                        <td style={{ padding: '8px 10px', textAlign: 'center' }}>
                          <Badge
                            variant={
                              fc.status === 'MATCH'
                                ? 'success'
                                : fc.status === 'MISMATCH' || fc.status === 'CONFLICT'
                                ? 'danger'
                                : 'warning'
                            }
                            size="sm"
                          >
                            {fc.status || 'CHECK'}
                          </Badge>
                        </td>
                        <td style={{ padding: '8px 10px', color: 'var(--text-muted)' }}>
                          {fc.notes || fc.discrepancy || '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Clarification prompt if any */}
            {reconciliationResult.clarification_prompt && (
              <div
                style={{
                  padding: '10px 14px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--accent-amber-light)',
                  border: '1px solid rgba(245, 158, 11, 0.3)',
                  color: 'var(--accent-amber)',
                  fontSize: '0.8125rem',
                }}
              >
                <strong>Clarification Required:</strong> {reconciliationResult.clarification_prompt}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Admin-Only Retention Purge Panel */}
      {isAdmin && (
        <Card variant="base">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <AlertTriangleIcon size={20} style={{ color: 'var(--accent-rose)' }} />
              <div>
                <h3 style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                  Administrative Evidence Retention & Purge Management
                </h3>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                  Executes batch unlinking and status updates for evidence records past the 30-day retention ceiling.
                </p>
              </div>
            </div>
            <Badge variant="danger" size="sm">Admin Role Enforced</Badge>
          </div>

          <div style={{ display: 'flex', gap: '16px', alignItems: 'center', flexWrap: 'wrap', marginBottom: '14px' }}>
            <div>
              <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginRight: '8px' }}>
                Batch Size:
              </label>
              <input
                type="number"
                min={1}
                max={500}
                value={purgeBatchSize}
                onChange={(e) => setPurgeBatchSize(Math.max(1, Math.min(500, parseInt(e.target.value) || 50)))}
                style={{
                  width: '80px',
                  padding: '6px 10px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-glass-base)',
                  border: 'var(--border-subtle)',
                  color: 'var(--text-primary)',
                  fontSize: '0.75rem',
                }}
              />
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={includeFailedPurges}
                onChange={(e) => setIncludeFailedPurges(e.target.checked)}
              />
              Include previously failed purges
            </label>

            <Button
              variant="danger"
              size="sm"
              loading={isPurging}
              onClick={() => setShowPurgeModal(true)}
            >
              Purge Expired Records
            </Button>
          </div>

          {purgeResult && (
            <div
              style={{
                padding: '10px 14px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--surface-glass-hover)',
                border: 'var(--border-subtle)',
                fontSize: '0.8125rem',
                display: 'flex',
                gap: '16px',
              }}
            >
              <span style={{ color: 'var(--text-muted)' }}>Status: <strong>{purgeResult.status}</strong></span>
              <span style={{ color: 'var(--text-muted)' }}>Purged Count: <strong>{purgeResult.purged_count ?? 0}</strong></span>
              {purgeResult.message && <span style={{ color: 'var(--text-muted)' }}>{purgeResult.message}</span>}
            </div>
          )}
        </Card>
      )}

      {/* Purge Confirmation Modal */}
      {showPurgeModal && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.7)',
            backdropFilter: 'blur(6px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
            padding: '16px',
          }}
          onClick={() => setShowPurgeModal(false)}
        >
          <div
            style={{
              background: 'var(--surface-card)',
              borderRadius: 'var(--radius-xl)',
              border: '1px solid rgba(244, 63, 94, 0.3)',
              padding: '24px',
              maxWidth: '480px',
              width: '100%',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <AlertTriangleIcon size={24} style={{ color: 'var(--accent-rose)' }} />
              <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                Confirm Irreversible Retention Purge
              </h3>
            </div>

            <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              This will irreversibly unlink and update up to <strong>{purgeBatchSize}</strong> expired evidence files
              whose 30-day retention ceiling has expired. Active and non-expired records will not be affected.
            </p>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <Button variant="ghost" size="sm" onClick={() => setShowPurgeModal(false)}>
                Cancel
              </Button>
              <Button variant="danger" size="sm" loading={isPurging} onClick={handleExecutePurge}>
                Confirm & Purge Expired
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
