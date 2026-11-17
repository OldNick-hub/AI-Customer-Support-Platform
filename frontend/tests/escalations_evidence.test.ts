/**
 * Escalation Center, Evidence Intake & Claim Reconciliation Test Suite.
 *
 * Verifies:
 * 1. escalationApi.getAfterHoursStatus retrieves operational hours and next opening.
 * 2. escalationApi.listAudits retrieves chronological immutable escalation audit trail.
 * 3. escalationApi.evaluateEscalation submits conversation turns and returns routing decisions.
 * 4. escalationApi.scanUnresolved triggers Rule B background evaluations.
 * 5. evidenceApi.uploadEvidence uploads multipart documents with magic-byte validation.
 * 6. evidenceApi.getEvidence and getEvidenceExtraction retrieve OCR metadata and structured entities.
 * 7. evidenceApi.purgeExpired executes administrative retention ceiling enforcement.
 * 8. reconciliationApi.reconcile evaluates claim assertions under strict financial trust hierarchy.
 *    - Invariant: DOCUMENT_CORROBORATED never auto-authorizes refunds (is_refund_authorized: false).
 */

import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  api,
  escalationApi,
  evidenceApi,
  reconciliationApi,
  type AfterHoursStatusResponse,
  type EscalationAuditItem,
  type EscalationEvaluateResponse,
  type ScanUnresolvedResponse,
  type EvidenceUploadResponse,
  type EvidenceDetailResponse,
  type EvidenceExtractionResponse,
  type ReconciliationResponse,
  type PurgeResult,
} from '../src/services/api.ts';

describe('Escalation Center, Evidence Processing & Reconciliation Workflows', () => {
  let capturedRequests: Array<{ url: string; method: string; data?: any; params?: any }> = [];

  beforeEach(() => {
    capturedRequests = [];
  });

  // ===========================================================================
  // 1. Escalation Center Operations & Business Hours
  // ===========================================================================

  test('escalationApi.getAfterHoursStatus queries after-hours schedule and timezone', async () => {
    const originalGet = api.get;
    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      return {
        data: {
          is_after_hours: true,
          reason: 'Outside standard operating hours (09:00 - 18:00 IST)',
          calendar_timezone: 'Asia/Kolkata',
          local_time: '2026-11-07T21:30:00+05:30',
          next_eligible_business_time: '2026-11-08T09:00:00+05:30',
        } as AfterHoursStatusResponse,
      } as any;
    }) as any;

    try {
      const res = await escalationApi.getAfterHoursStatus();
      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/escalation/after-hours/status');
      assert.equal(res.is_after_hours, true);
      assert.equal(res.calendar_timezone, 'Asia/Kolkata');
      assert.ok(res.next_eligible_business_time);
    } finally {
      api.get = originalGet;
    }
  });

  test('escalationApi.listAudits retrieves immutable audit events with pagination', async () => {
    const originalGet = api.get;
    api.get = (async (url: string, config?: any) => {
      capturedRequests.push({ url, method: 'GET', params: config?.params });
      return {
        data: [
          {
            id: 'audit-uuid-001',
            conversation_id: 'conv-uuid-1',
            ticket_id: 'ticket-uuid-1',
            trigger_condition: 'HIGH_RISK_SAFETY',
            previous_state: 'ACTIVE',
            new_state: 'ESCALATED',
            escalation_reason: 'Self-harm / critical safety keyword detected in customer turn.',
            escalation_payload: { detected_pattern: 'critical_harm', confidence: 0.98 },
            triggered_at: '2026-11-07T08:30:00Z',
          },
          {
            id: 'audit-uuid-002',
            conversation_id: 'conv-uuid-2',
            ticket_id: null,
            trigger_condition: 'SENTIMENT_FRUSTRATION',
            previous_state: 'ACTIVE',
            new_state: 'ESCALATED',
            escalation_reason: 'Negative sentiment slope exceeded threshold over 3 turns.',
            escalation_payload: { slope_delta: -0.62 },
            triggered_at: '2026-11-07T08:45:00Z',
          },
        ] as EscalationAuditItem[],
      } as any;
    }) as any;

    try {
      const audits = await escalationApi.listAudits({ limit: 20 });
      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/escalation/audits');
      assert.deepEqual(capturedRequests[0].params, { limit: 20 });
      assert.equal(audits.length, 2);
      assert.equal(audits[0].trigger_condition, 'HIGH_RISK_SAFETY');
      assert.equal(audits[1].trigger_condition, 'SENTIMENT_FRUSTRATION');
    } finally {
      api.get = originalGet;
    }
  });

  test('escalationApi.evaluateEscalation posts message turn and returns deterministic decision', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          should_escalate: true,
          trigger_condition: 'HIGH_RISK_SAFETY',
          reason: 'Severe threat keyword flagged by safety classifier.',
          is_after_hours: false,
          routing_destination: 'SAFETY_RAPID_RESPONSE',
          next_eligible_business_time: null,
          audit_record_id: 'audit-uuid-999',
          already_escalated: false,
        } as EscalationEvaluateResponse,
      } as any;
    }) as any;

    try {
      const res = await escalationApi.evaluateEscalation({
        conversation_id: 'conv-uuid-test',
        message_content: 'I will escalate this immediately if not fixed.',
      });

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/escalation/evaluate');
      assert.equal(capturedRequests[0].data.conversation_id, 'conv-uuid-test');
      assert.equal(res.should_escalate, true);
      assert.equal(res.routing_destination, 'SAFETY_RAPID_RESPONSE');
    } finally {
      api.post = originalPost;
    }
  });

  test('escalationApi.scanUnresolved triggers batch Rule B scan and returns escalated count', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          scanned_at: '2026-11-07T09:00:00Z',
          escalated_count: 3,
          audit_ids: ['audit-1', 'audit-2', 'audit-3'],
        } as ScanUnresolvedResponse,
      } as any;
    }) as any;

    try {
      const res = await escalationApi.scanUnresolved();
      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/escalation/scan-unresolved');
      assert.equal(res.escalated_count, 3);
      assert.equal(res.audit_ids.length, 3);
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 2. Multimodal Evidence Intake & OCR Extraction
  // ===========================================================================

  test('evidenceApi.uploadEvidence uploads multipart payload and receives staged metadata', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          evidence_id: 'evi-uuid-001',
          conversation_id: 'conv-uuid-001',
          customer_id: 'cust-uuid-001',
          file_name: 'receipt_invoice_1026.pdf',
          mime_type: 'application/pdf',
          file_size_bytes: 142050,
          sha256_hash: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
          status: 'VERIFIED',
          is_malicious: false,
          quarantine_reason: null,
          retention_expires_at: '2026-12-07T08:00:00Z',
          created_at: '2026-11-07T08:00:00Z',
          structured_entities: {
            merchant_name: 'Electronics Mart India',
            total_amount: 4500,
            currency: 'INR',
            invoice_date: '2026-10-15',
            line_items: [
              {
                product_name: 'Smart Blender 750W',
                quantity: 1,
                unit_price: 4500,
                total_price: 4500,
                confidence: 0.96,
              },
            ],
            overall_confidence: 0.95,
            field_confidence: { total_amount: 0.98, merchant_name: 0.94 },
            uncertain_fields: [],
            missing_fields: [],
            warnings: [],
            page_count: 1,
            is_scanned: false,
            provider_used: 'tesseract_fastpath',
            pii_redacted_text: 'Electronics Mart India\nDate: 2026-10-15\nTotal: INR 4500.00',
          },
        } as EvidenceUploadResponse,
      } as any;
    }) as any;

    try {
      // Simulate multipart FormData object
      const dummyFormData = {} as FormData;
      const res = await evidenceApi.uploadEvidence(dummyFormData);

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/evidence/upload');
      assert.equal(res.evidence_id, 'evi-uuid-001');
      assert.equal(res.status, 'VERIFIED');
      assert.equal(res.structured_entities?.total_amount, 4500);
      assert.equal(res.structured_entities?.merchant_name, 'Electronics Mart India');
    } finally {
      api.post = originalPost;
    }
  });

  test('evidenceApi.getEvidence and getEvidenceExtraction return OCR data and retention fields', async () => {
    const originalGet = api.get;
    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      if (url.endsWith('/extraction')) {
        return {
          data: {
            evidence_id: 'evi-uuid-001',
            status: 'VERIFIED',
            extracted_at: '2026-11-07T08:01:00Z',
            ocr_confidence: 0.94,
            pii_redacted_text: 'Merchant: FreshMart Grocery\nTotal: 1299.00',
            entities: {
              merchant_name: 'FreshMart Grocery',
              total_amount: 1299,
              currency: 'INR',
              line_items: [],
              overall_confidence: 0.94,
              field_confidence: {},
              uncertain_fields: [],
              missing_fields: [],
              warnings: [],
              page_count: 1,
              is_scanned: false,
              provider_used: 'mock_fastpath',
              pii_redacted_text: 'Merchant: FreshMart Grocery\nTotal: 1299.00',
            },
          } as EvidenceExtractionResponse,
        } as any;
      }
      return {
        data: {
          evidence_id: 'evi-uuid-001',
          file_name: 'freshmart_receipt.jpg',
          mime_type: 'image/jpeg',
          file_size_bytes: 84000,
          sha256_hash: 'abc1234567890abcdef',
          status: 'VERIFIED',
          is_malicious: false,
          retention_expires_at: '2026-12-07T08:00:00Z',
          created_at: '2026-11-07T08:00:00Z',
        } as EvidenceDetailResponse,
      } as any;
    }) as any;

    try {
      const details = await evidenceApi.getEvidence('evi-uuid-001');
      const extraction = await evidenceApi.getEvidenceExtraction('evi-uuid-001');

      assert.equal(capturedRequests.length, 2);
      assert.equal(capturedRequests[0].url, '/evidence/evi-uuid-001');
      assert.equal(capturedRequests[1].url, '/evidence/evi-uuid-001/extraction');
      assert.equal(details.mime_type, 'image/jpeg');
      assert.equal(extraction.entities.merchant_name, 'FreshMart Grocery');
      assert.equal(extraction.ocr_confidence, 0.94);
    } finally {
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 3. Admin Retention & Purge Management
  // ===========================================================================

  test('evidenceApi.purgeExpired enforces batch size and administrative query params', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any, config?: any) => {
      capturedRequests.push({ url, method: 'POST', data, params: config?.params });
      return {
        data: {
          status: 'SUCCESS',
          purged_count: 12,
          message: 'Purged 12 expired evidence records past the 30-day retention ceiling.',
        } as PurgeResult,
      } as any;
    }) as any;

    try {
      const res = await evidenceApi.purgeExpired(100, true);
      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/evidence/purge-expired');
      assert.deepEqual(capturedRequests[0].params, {
        batch_size: 100,
        include_failed: true,
      });
      assert.equal(res.purged_count, 12);
      assert.equal(res.status, 'SUCCESS');
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 4. Financial Claim Reconciliation & Trust Invariants
  // ===========================================================================

  test('reconciliationApi.reconcile enforces DOCUMENT_CORROBORATED without auto-authorizing refunds', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          verification_id: 'verif-uuid-001',
          evidence_id: 'evi-uuid-001',
          conversation_id: 'conv-uuid-001',
          job_status: 'COMPLETED',
          reconciliation_status: 'EVALUATED',
          overall_match_status: 'MATCH',
          overall_confidence: 0.95,
          summary: 'Invoice items and total amount match customer statements perfectly.',
          clarification_status: 'NONE',
          field_comparisons: [
            {
              field_name: 'total_amount',
              extracted_value: 4500,
              claimed_value: 4500,
              status: 'MATCH',
            },
            {
              field_name: 'merchant',
              extracted_value: 'Electronics Mart India',
              claimed_value: 'Electronics Mart',
              status: 'MATCH',
            },
          ],
          extracted_claims: [],
          security_flags: [],
          is_quarantined: false,
          verification_level: 'DOCUMENT_CORROBORATED',
          is_refund_authorized: false, // Strict Rule: Customer docs alone NEVER auto-authorize refunds!
          requires_authoritative_verification: true,
        } as ReconciliationResponse,
      } as any;
    }) as any;

    try {
      const res = await reconciliationApi.reconcile({
        conversation_id: 'conv-uuid-001',
        evidence_id: 'evi-uuid-001',
        claim_text: 'Customer claims refund of ₹4,500 for order from Electronics Mart.',
      });

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/evidence/reconcile');
      assert.equal(capturedRequests[0].data.evidence_id, 'evi-uuid-001');

      // Platform Non-Negotiable Invariants:
      assert.equal(res.verification_level, 'DOCUMENT_CORROBORATED');
      assert.equal(res.is_refund_authorized, false, 'Refund must not be auto-authorized on customer evidence alone');
      assert.equal(res.requires_authoritative_verification, true);
      assert.equal(res.overall_match_status, 'MATCH');
      assert.equal(res.field_comparisons.length, 2);
    } finally {
      api.post = originalPost;
    }
  });

  test('reconciliationApi.reconcile identifies discrepancy and flags conflict', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          verification_id: 'verif-uuid-002',
          evidence_id: 'evi-uuid-002',
          conversation_id: 'conv-uuid-002',
          job_status: 'COMPLETED',
          reconciliation_status: 'EVALUATED',
          overall_match_status: 'CONFLICT',
          overall_confidence: 0.88,
          summary: 'Claimed refund amount (₹7,500) differs from document invoice total (₹4,500).',
          clarification_status: 'REQUIRED',
          clarification_prompt: 'Please confirm why the requested amount exceeds the receipt total.',
          field_comparisons: [
            {
              field_name: 'total_amount',
              extracted_value: 4500,
              claimed_value: 7500,
              status: 'CONFLICT',
              discrepancy: 'Amount mismatch: ₹3,000 difference',
            },
          ],
          extracted_claims: [],
          security_flags: ['AMOUNT_DISCREPANCY'],
          is_quarantined: false,
          verification_level: 'DOCUMENT_CORROBORATED',
          is_refund_authorized: false,
          requires_authoritative_verification: true,
        } as ReconciliationResponse,
      } as any;
    }) as any;

    try {
      const res = await reconciliationApi.reconcile({
        conversation_id: 'conv-uuid-002',
        evidence_id: 'evi-uuid-002',
        claim_text: 'Customer claims refund of ₹7,500 for blender.',
      });

      assert.equal(res.overall_match_status, 'CONFLICT');
      assert.equal(res.clarification_status, 'REQUIRED');
      assert.ok(res.clarification_prompt);
      assert.equal(res.field_comparisons[0].status, 'CONFLICT');
      assert.equal(res.is_refund_authorized, false);
    } finally {
      api.post = originalPost;
    }
  });
});
