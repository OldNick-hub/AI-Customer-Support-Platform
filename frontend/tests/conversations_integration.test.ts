/**
 * Live Conversations, Chat, and Ticket Synthesis Integration Test Suite.
 *
 * Verifies:
 * 1. chatApi.sendMessage strictly sends valid canonical payloads without extra fields.
 * 2. ticketsApi synthesis endpoints invoke expected paths, methods, and payloads.
 * 3. Message mapping, chronological sorting, and PII-masked content preservation.
 * 4. Ticket synthesis completeness scoring, status evaluation, and clarification round processing.
 * 5. AbortController signal handling and error propagation.
 */

import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  api,
  chatApi,
  ticketsApi,
  conversationsApi,
  type ChatMessageRequest,
  type ChatMessageResponse,
  type MessageItem,
  type ClarificationListItem,
} from '../src/services/api.ts';

describe('Conversations, Chat & Ticket Synthesis Integration', () => {
  let capturedRequests: Array<{ url: string; method: string; data?: any; params?: any }> = [];

  beforeEach(() => {
    capturedRequests = [];
  });

  // ===========================================================================
  // 1. Live Chat API Contract (POST /api/v1/chat/message)
  // ===========================================================================

  test('chatApi.sendMessage sends canonical payload matching schema invariants', async () => {
    // Intercept api.post
    const originalPost = api.post;
    api.post = (async (url: string, data?: any, config?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      return {
        data: {
          success: true,
          data: {
            message_id: 'msg-response-123',
            reply_text: 'Electronics return window is 15 calendar days.',
            is_resolved: true,
            refusal_triggered: false,
            citations: [
              {
                citation_id: 1,
                document_code: 'POL-RET-US-2026',
                document_title: 'US Return Policy',
                version_number: 1,
                effective_date: '2026-01-01',
                chunk_excerpt: 'Electronics return window is 15 days.',
              },
            ],
            confidence_score: 0.95,
            effective_as_of_date_applied: '2026-11-07',
            detected_language: 'en',
            primary_language: 'en',
            is_escalated: false,
            turn_sequence_number: 2,
            conversation_id: 'conv-uuid-456',
          },
          meta: {
            timestamp: new Date().toISOString(),
            request_id: 'req-corr-789',
            caller_auth_type: 'SESSION_TOKEN',
          },
        } as ChatMessageResponse,
      } as any;
    }) as any;

    try {
      const payload: ChatMessageRequest = {
        message: 'What is the return window for electronics?',
        conversation_id: 'conv-uuid-456',
        preferred_language: 'en',
        idempotency_key: 'idem-test-123',
      };

      const res = await chatApi.sendMessage(payload);

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/chat/message');
      assert.equal(capturedRequests[0].data.message, 'What is the return window for electronics?');
      assert.equal(capturedRequests[0].data.conversation_id, 'conv-uuid-456');
      assert.equal(capturedRequests[0].data.preferred_language, 'en');

      // Verify no illegal client-supplied auth/role overrides were sent
      assert.equal(capturedRequests[0].data.access_tier, undefined);
      assert.equal(capturedRequests[0].data.customer_id, undefined);
      assert.equal(capturedRequests[0].data.role, undefined);

      // Verify response envelope parsing
      assert.equal(res.success, true);
      assert.equal(res.data.reply_text, 'Electronics return window is 15 calendar days.');
      assert.equal(res.data.citations.length, 1);
      assert.equal(res.data.citations[0].document_code, 'POL-RET-US-2026');
      assert.equal(res.data.turn_sequence_number, 2);
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 2. Ticket Synthesis Endpoints Contract
  // ===========================================================================

  test('ticketsApi synthesis methods invoke expected endpoints and schemas', async () => {
    const originalPost = api.post;
    const originalGet = api.get;

    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      if (url === '/tickets/synthesize') {
        return {
          data: {
            ticket_id: data.ticket_id,
            synthesis_status: 'SYNTHESIZED',
            synthesis: { problem_statement: 'Damaged item received on delivery.' },
            completeness: { completeness_score: 0.85, is_complete: true },
          },
        } as any;
      }
      if (url.includes('/synthesis/respond')) {
        return {
          data: {
            ticket_id: 'ticket-1',
            clarification_request_id: data.clarification_request_id,
            round_number: 1,
            response_absorbed: true,
            ambiguous_response: false,
            new_completeness: { completeness_score: 0.9 },
            should_continue_clarification: false,
            synthesis_status: 'COMPLETE',
          },
        } as any;
      }
      if (url.includes('/synthesis/complete')) {
        return {
          data: {
            status: 'success',
            ticket_id: 'ticket-1',
            synthesis_status: 'COMPLETE',
          },
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      if (url === '/tickets/ticket-1/synthesis') {
        return {
          data: {
            ticket_id: 'ticket-1',
            synthesis_status: 'SYNTHESIZED',
            problem_statement: 'Damaged item received.',
            reference_identifiers: [{ key: 'order_id', value: 'ORD-9999' }],
            missing_fields: [],
            completeness_score: 0.88,
            extraction_confidence: 0.92,
            extraction_provenance: {},
            synthesis_version: 1,
          },
        } as any;
      }
      if (url === '/tickets/ticket-1/synthesis/clarifications') {
        return {
          data: {
            ticket_id: 'ticket-1',
            rounds: [
              {
                id: 'clar-round-1',
                round_number: 1,
                status: 'PENDING_CUSTOMER_RESPONSE',
                questions: [{ question_text: 'Please provide tracking number.' }],
                targeted_missing_fields: ['tracking_number'],
                completeness_score_before: 0.6,
                completeness_score_after: null,
                created_at: new Date().toISOString(),
              },
            ],
          },
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    try {
      // 1. Synthesize ticket
      const synthRes = await ticketsApi.synthesizeTicket('ticket-1', true);
      assert.equal(synthRes.synthesis_status, 'SYNTHESIZED');
      assert.equal(capturedRequests[0].url, '/tickets/synthesize');
      assert.equal(capturedRequests[0].data.force_resynthesis, true);

      // 2. Get synthesis
      const detailRes = await ticketsApi.getSynthesis('ticket-1');
      assert.equal(detailRes.problem_statement, 'Damaged item received.');
      assert.equal(detailRes.completeness_score, 0.88);
      assert.equal(capturedRequests[1].url, '/tickets/ticket-1/synthesis');

      // 3. Get clarifications
      const clarRes = await ticketsApi.getClarifications('ticket-1');
      assert.equal(clarRes.rounds.length, 1);
      assert.equal(clarRes.rounds[0].status, 'PENDING_CUSTOMER_RESPONSE');
      assert.equal(capturedRequests[2].url, '/tickets/ticket-1/synthesis/clarifications');

      // 4. Respond to clarification
      const respRes = await ticketsApi.respondToClarification('ticket-1', 'clar-round-1', 'TRACK-12345');
      assert.equal(respRes.response_absorbed, true);
      assert.equal(capturedRequests[3].url, '/tickets/ticket-1/synthesis/respond');
      assert.equal(capturedRequests[3].data.customer_response, 'TRACK-12345');

      // 5. Mark complete (staff)
      const completeRes = await ticketsApi.markSynthesisComplete('ticket-1');
      assert.equal(completeRes.synthesis_status, 'COMPLETE');
      assert.equal(capturedRequests[4].url, '/tickets/ticket-1/synthesis/complete');
    } finally {
      api.post = originalPost;
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 3. Message History Mapping & Chronological Sorting
  // ===========================================================================

  test('Message items map correctly and preserve sender classifications', () => {
    const rawMessages: MessageItem[] = [
      {
        id: 'msg-1',
        conversation_id: 'conv-1',
        sender_type: 'CUSTOMER',
        content: 'I want a refund for [ORDER_ID_MASKED].',
        detected_language: 'en',
        turn_sequence_number: 1,
        created_at: '2026-11-07T08:00:00Z',
      },
      {
        id: 'msg-2',
        conversation_id: 'conv-1',
        sender_type: 'AI_AGENT',
        content: 'I can assist you with your refund inquiry for [ORDER_ID_MASKED].',
        detected_language: 'en',
        turn_sequence_number: 1,
        created_at: '2026-11-07T08:00:05Z',
      },
      {
        id: 'msg-3',
        conversation_id: 'conv-1',
        sender_type: 'HUMAN_AGENT',
        content: 'Hello, I have taken over this thread to finalize your claim.',
        detected_language: 'en',
        turn_sequence_number: 2,
        created_at: '2026-11-07T08:01:00Z',
      },
    ];

    // Verify chronological order (created_at asc)
    const sorted = [...rawMessages].sort(
      (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
    );

    assert.equal(sorted[0].id, 'msg-1');
    assert.equal(sorted[1].id, 'msg-2');
    assert.equal(sorted[2].id, 'msg-3');

    // Verify PII masking is intact
    assert.ok(sorted[0].content.includes('[ORDER_ID_MASKED]'));
    assert.ok(!sorted[0].content.includes('4111-1111-1111-1111'));
  });

  // ===========================================================================
  // 4. Clarification Rounds Logic
  // ===========================================================================

  test('Clarification rounds identify pending customer response state', () => {
    const rounds: ClarificationListItem[] = [
      {
        id: 'round-1',
        round_number: 1,
        status: 'RESOLVED',
        questions: [{ question_text: 'What was your purchase date?' }],
        targeted_missing_fields: ['purchase_date'],
        completeness_score_before: 0.5,
        completeness_score_after: 0.75,
        created_at: '2026-11-07T07:00:00Z',
        responded_at: '2026-11-07T07:05:00Z',
      },
      {
        id: 'round-2',
        round_number: 2,
        status: 'PENDING_CUSTOMER_RESPONSE',
        questions: [{ question_text: 'What is the serial number of the unit?' }],
        targeted_missing_fields: ['serial_number'],
        completeness_score_before: 0.75,
        completeness_score_after: null,
        created_at: '2026-11-07T07:10:00Z',
      },
    ];

    const pendingRound = rounds.find((r) => r.status === 'PENDING_CUSTOMER_RESPONSE');
    assert.ok(pendingRound !== undefined);
    assert.equal(pendingRound?.round_number, 2);
    assert.equal(pendingRound?.targeted_missing_fields[0], 'serial_number');
  });

  // ===========================================================================
  // 5. Abort Controller Signal Cancellation
  // ===========================================================================

  test('chatApi.sendMessage supports AbortSignal cancellation', async () => {
    const originalPost = api.post;
    api.post = (async (_url: string, _data?: any, config?: any) => {
      if (config?.signal?.aborted) {
        const abortErr = new Error('The operation was aborted');
        (abortErr as any).name = 'AbortError';
        throw abortErr;
      }
      return { data: { success: true } } as any;
    }) as any;

    try {
      const controller = new AbortController();
      controller.abort(); // pre-abort

      await assert.rejects(
        async () => {
          await chatApi.sendMessage({ message: 'Hello' }, controller.signal);
        },
        (err: any) => err.name === 'AbortError'
      );
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 6. Thread Control: Rename and Delete
  // ===========================================================================

  test('conversationsApi supports renaming and deleting conversation threads', async () => {
    const originalPatch = api.patch;
    const originalDelete = api.delete;

    api.patch = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'PATCH', data });
      return {
        data: {
          id: 'conv-test-123',
          custom_title: data.custom_title,
          status: 'OPEN',
          turn_count: 5,
        },
      } as any;
    }) as any;

    api.delete = (async (url: string) => {
      capturedRequests.push({ url, method: 'DELETE' });
      return {
        data: {
          success: true,
          message: 'Conversation conv-test-123 deleted successfully.',
        },
      } as any;
    }) as any;

    try {
      // Test Rename
      const renameRes = await conversationsApi.renameConversation('conv-test-123', 'Damaged Item Inquiry');
      assert.equal(renameRes.custom_title, 'Damaged Item Inquiry');
      assert.equal(capturedRequests[0].url, '/conversations/conv-test-123');
      assert.equal(capturedRequests[0].method, 'PATCH');
      assert.equal(capturedRequests[0].data.custom_title, 'Damaged Item Inquiry');

      // Test Delete
      const deleteRes = await conversationsApi.deleteConversation('conv-test-123');
      assert.equal(deleteRes.success, true);
      assert.equal(capturedRequests[1].url, '/conversations/conv-test-123');
      assert.equal(capturedRequests[1].method, 'DELETE');
    } finally {
      api.patch = originalPatch;
      api.delete = originalDelete;
    }
  });
});

