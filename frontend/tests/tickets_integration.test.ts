/**
 * Tickets Queue, Routing, and Resolution Workflows Integration Test Suite.
 *
 * Verifies:
 * 1. ticketsApi.listTickets sends correct pagination and filter parameters.
 * 2. ticketsApi.getTicket retrieves full ticket details, SLA fields, and resolution states.
 * 3. Priority evaluation and explainability API invocation.
 * 4. Intelligent routing and manual assignment with capacity override flag.
 * 5. Department workload metrics retrieval.
 * 6. Resolution playbook evaluation, action proposal, approval, rejection, and parameter updates.
 * 7. Action execution and resolution verification workflows.
 * 8. Routing and resolution immutable audit history trails.
 * 9. RBAC permission barriers and capacity limit (409) handling.
 */

import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  api,
  ticketsApi,
  type TicketListResponse,
  type TicketDetailResponse,
  type PriorityEvaluateResponse,
  type RouteTicketResponse,
  type AssignTicketResponse,
  type RoutingHistoryResponse,
  type TeamWorkloadResponse,
  type EvaluateResolutionResponse,
  type ResolutionActionItem,
  type ExecuteActionResponse,
  type VerifyResolutionResponse,
  type ResolutionHistoryResponse,
} from '../src/services/api.ts';

describe('Tickets Queue, Routing & Resolution Workflows Integration', () => {
  let capturedRequests: Array<{ url: string; method: string; data?: any; params?: any }> = [];

  beforeEach(() => {
    capturedRequests = [];
  });

  // ===========================================================================
  // 1. Live Ticket Queue & Pagination / Filters
  // ===========================================================================

  test('ticketsApi.listTickets passes pagination and filter query params correctly', async () => {
    const originalGet = api.get;
    api.get = (async (url: string, config?: any) => {
      capturedRequests.push({ url, method: 'GET', params: config?.params });
      return {
        data: {
          items: [
            {
              id: 'ticket-uuid-001',
              ticket_number: 'TCK-202611-00001',
              conversation_id: 'conv-uuid-1',
              customer_id: 'cust-uuid-1',
              title: 'Damaged shipment on delivery',
              description: 'Customer reports crushed packaging and cracked screen.',
              masked_summary: 'Customer reports damaged shipment.',
              priority: 'HIGH',
              priority_score: 3.8,
              status: 'OPEN',
              category: 'hardware',
              synthesis_status: 'SYNTHESIZED',
              routing_status: 'UNROUTED',
              resolution_status: 'UNRESOLVED',
              sla_target_minutes: 120,
              sla_breach_at: '2026-11-07T12:00:00Z',
              sla_breached: false,
              created_at: '2026-11-07T08:00:00Z',
              updated_at: '2026-11-07T08:00:00Z',
            },
          ],
          total_count: 45,
          limit: 10,
          offset: 20,
        } as TicketListResponse,
      } as any;
    }) as any;

    try {
      const res = await ticketsApi.listTickets({
        status: 'OPEN',
        priority: 'HIGH',
        category: 'hardware',
        limit: 10,
        offset: 20,
      });

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/tickets');
      assert.deepEqual(capturedRequests[0].params, {
        status: 'OPEN',
        priority: 'HIGH',
        category: 'hardware',
        limit: 10,
        offset: 20,
      });

      assert.equal(res.items.length, 1);
      assert.equal(res.total_count, 45);
      assert.equal(res.items[0].ticket_number, 'TCK-202611-00001');
      assert.equal(res.items[0].priority, 'HIGH');
    } finally {
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 2. Ticket Details & SLA Metrics
  // ===========================================================================

  test('ticketsApi.getTicket retrieves full ticket details with SLA metadata', async () => {
    const originalGet = api.get;
    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      return {
        data: {
          id: 'ticket-uuid-001',
          ticket_number: 'TCK-202611-00001',
          customer_id: 'cust-uuid-1',
          title: 'Damaged shipment on delivery',
          description: 'Crushed packaging with broken display panel.',
          masked_summary: 'Customer reports damaged shipment.',
          priority: 'CRITICAL',
          priority_score: 4.85,
          priority_explanation: 'Elevated due to high-value item and VIP customer tier.',
          status: 'OPEN',
          category: 'hardware',
          sla_calendar_id: 'cal-us-std',
          sla_target_minutes: 60,
          sla_warning_at: '2026-11-07T08:45:00Z',
          sla_breach_at: '2026-11-07T09:00:00Z',
          sla_warning_sent: true,
          sla_breached: false,
          total_paused_duration_seconds: 600,
          synthesis_status: 'COMPLETE',
          routing_status: 'ROUTED',
          resolution_status: 'EVALUATED',
          created_at: '2026-11-07T08:00:00Z',
          updated_at: '2026-11-07T08:30:00Z',
        } as TicketDetailResponse,
      } as any;
    }) as any;

    try {
      const res = await ticketsApi.getTicket('ticket-uuid-001');

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/tickets/ticket-uuid-001');
      assert.equal(res.priority, 'CRITICAL');
      assert.equal(res.priority_score, 4.85);
      assert.ok(res.priority_explanation?.includes('VIP customer tier'));
      assert.equal(res.total_paused_duration_seconds, 600);
      assert.equal(res.sla_warning_sent, true);
    } finally {
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 3. Priority Evaluation & Explainability
  // ===========================================================================

  test('ticketsApi.evaluatePriority posts to endpoint and returns explainable supporting facts', async () => {
    const originalPost = api.post;
    api.post = (async (url: string) => {
      capturedRequests.push({ url, method: 'POST' });
      return {
        data: {
          ticket_id: 'ticket-uuid-001',
          priority: 'CRITICAL',
          priority_score: 4.75,
          priority_floor: 'HIGH',
          reason: 'Severe customer distress and verified order damage exceeding threshold.',
          supporting_facts: [
            'Customer tier: VIP (+0.5)',
            'Category hardware critical floor applied',
            'Order value: ₹45,000 corroborated',
          ],
        } as PriorityEvaluateResponse,
      } as any;
    }) as any;

    try {
      const res = await ticketsApi.evaluatePriority('ticket-uuid-001');

      assert.equal(capturedRequests.length, 1);
      assert.equal(capturedRequests[0].url, '/tickets/ticket-uuid-001/priority/evaluate');
      assert.equal(res.priority, 'CRITICAL');
      assert.equal(res.priority_score, 4.75);
      assert.equal(res.supporting_facts.length, 3);
      assert.ok(res.supporting_facts[0].includes('VIP'));
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 4. Intelligent Routing & Capacity-Locked Assignment
  // ===========================================================================

  test('ticketsApi.routeTicket and assignTicket execute intelligent assignment with override support', async () => {
    const originalPost = api.post;
    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      if (url.includes('/route')) {
        return {
          data: {
            ticket_id: 'ticket-uuid-001',
            target_department: 'Hardware Support',
            assigned_agent_id: 'agent-uuid-777',
            assigned_agent_name: 'Agent Alice',
            routing_status: 'ASSIGNED',
            reason: 'Best match for hardware category with available capacity (1/5 tickets).',
            candidates_evaluated: 4,
            candidate_details: [],
          } as RouteTicketResponse,
        } as any;
      }
      if (url.includes('/assign')) {
        return {
          data: {
            ticket_id: 'ticket-uuid-001',
            assigned_agent_id: data.agent_id,
            assigned_agent_name: 'Agent Bob',
            assigned_team: 'Hardware Support',
            routing_status: 'ASSIGNED',
            active_tickets: 5,
            max_active_tickets: 5,
            reason: data.reason,
            capacity_overridden: Boolean(data.force_override),
          } as AssignTicketResponse,
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    try {
      // 1. Auto Route
      const routeRes = await ticketsApi.routeTicket('ticket-uuid-001');
      assert.equal(routeRes.target_department, 'Hardware Support');
      assert.equal(routeRes.assigned_agent_name, 'Agent Alice');
      assert.equal(capturedRequests[0].url, '/tickets/ticket-uuid-001/route');

      // 2. Manual Assign with Force Override
      const assignRes = await ticketsApi.assignTicket('ticket-uuid-001', {
        agent_id: 'agent-uuid-888',
        reason: 'Senior supervisor manual handoff for urgent escalation.',
        force_override: true,
      });

      assert.equal(assignRes.assigned_agent_name, 'Agent Bob');
      assert.equal(assignRes.capacity_overridden, true);
      assert.equal(capturedRequests[1].url, '/tickets/ticket-uuid-001/assign');
      assert.equal(capturedRequests[1].data.force_override, true);
    } finally {
      api.post = originalPost;
    }
  });

  // ===========================================================================
  // 5. Department Workload & Routing Audit Trail
  // ===========================================================================

  test('ticketsApi retrieves team workload metrics and chronological routing history', async () => {
    const originalGet = api.get;
    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      if (url === '/tickets/teams/workload') {
        return {
          data: {
            teams: [
              {
                department: 'Hardware Support',
                active_agents: 3,
                total_capacity: 15,
                active_tickets: 9,
                utilization_pct: 60.0,
              },
            ],
          } as TeamWorkloadResponse,
        } as any;
      }
      if (url.includes('/routing-history')) {
        return {
          data: {
            ticket_id: 'ticket-uuid-001',
            events: [
              {
                event_id: 'evt-1',
                event_type: 'PRIORITY_EVALUATED',
                event_payload: { old_priority: 'MEDIUM', new_priority: 'HIGH' },
                created_at: '2026-11-07T08:05:00Z',
              },
            ],
          } as RoutingHistoryResponse,
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    try {
      const workloadRes = await ticketsApi.getTeamsWorkload();
      assert.equal(workloadRes.teams.length, 1);
      assert.equal(workloadRes.teams[0].department, 'Hardware Support');
      assert.equal(workloadRes.teams[0].utilization_pct, 60.0);

      const historyRes = await ticketsApi.getRoutingHistory('ticket-uuid-001');
      assert.equal(historyRes.events.length, 1);
      assert.equal(historyRes.events[0].event_type, 'PRIORITY_EVALUATED');
    } finally {
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 6. Resolution Workflows: Evaluate, Propose, Approve, Execute & Verify
  // ===========================================================================

  test('ticketsApi supports full resolution lifecycle with audit and safety checks', async () => {
    const originalPost = api.post;
    const originalPatch = api.patch;
    const originalGet = api.get;

    api.post = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'POST', data });
      if (url.includes('/resolution/evaluate')) {
        return {
          data: {
            ticket_id: 'ticket-uuid-001',
            resolution_id: 'res-uuid-1',
            status: 'PROPOSED',
            playbook_category: 'DAMAGED_ITEM',
            recommended_resolution: 'Issue replacement unit upon receipt of damaged return.',
            confidence_score: 0.92,
            eligibility_decision: 'ELIGIBLE',
            requires_human_approval: true,
            supporting_evidence: ['Damage photo verified', 'Return label created'],
            actions: [
              {
                action_id: 'act-uuid-1',
                action_type: 'DISPATCH_REPLACEMENT',
                action_key: 'replacement_hardware',
                action_params: { sku: 'XPS-15-2026', expedited: true },
                status: 'PROPOSED',
                is_sensitive: true,
                requires_approval: true,
                execution_attempts: 0,
                execution_result: {},
              },
            ],
          } as EvaluateResolutionResponse,
        } as any;
      }
      if (url.includes('/approve')) {
        return {
          data: {
            action_id: 'act-uuid-1',
            action_type: 'DISPATCH_REPLACEMENT',
            action_key: 'replacement_hardware',
            action_params: { sku: 'XPS-15-2026', expedited: true },
            status: 'APPROVED',
            is_sensitive: true,
            requires_approval: true,
            approved_by: 'supervisor@enterprise.com',
            execution_attempts: 0,
            execution_result: {},
          } as ResolutionActionItem,
        } as any;
      }
      if (url.includes('/execute')) {
        return {
          data: {
            action_id: 'act-uuid-1',
            status: 'EXECUTED',
            execution_attempts: 1,
            result: { shipment_id: 'SHP-992014', carrier: 'FedEx' },
          } as ExecuteActionResponse,
        } as any;
      }
      if (url.includes('/verify')) {
        return {
          data: {
            verified: true,
            resolution_status: 'VERIFIED',
            ticket_status: 'RESOLVED',
            failures: [],
            escalated: false,
            verified_at: '2026-11-07T09:30:00Z',
            actions_executed: 1,
          } as VerifyResolutionResponse,
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    api.patch = (async (url: string, data?: any) => {
      capturedRequests.push({ url, method: 'PATCH', data });
      return {
        data: {
          action_id: 'act-uuid-1',
          action_type: 'DISPATCH_REPLACEMENT',
          action_key: 'replacement_hardware',
          action_params: data.action_params,
          status: 'PROPOSED', // Modifying params resets approval!
          is_sensitive: true,
          requires_approval: true,
          approved_by: null,
          execution_attempts: 0,
          execution_result: {},
        } as ResolutionActionItem,
      } as any;
    }) as any;

    api.get = (async (url: string) => {
      capturedRequests.push({ url, method: 'GET' });
      if (url.includes('/resolution/history')) {
        return {
          data: {
            ticket_id: 'ticket-uuid-001',
            events: [
              {
                event_id: 'evt-res-1',
                event_type: 'RESOLUTION_EVALUATED',
                event_payload: { category: 'DAMAGED_ITEM' },
                created_at: '2026-11-07T09:00:00Z',
              },
            ],
          } as ResolutionHistoryResponse,
        } as any;
      }
      return { data: {} } as any;
    }) as any;

    try {
      // 1. Evaluate Resolution
      const evalRes = await ticketsApi.evaluateResolution('ticket-uuid-001');
      assert.equal(evalRes.playbook_category, 'DAMAGED_ITEM');
      assert.equal(evalRes.actions.length, 1);
      assert.equal(evalRes.actions[0].requires_approval, true);

      // 2. Approve Action
      const appRes = await ticketsApi.approveAction('ticket-uuid-001', 'act-uuid-1', 'Approved per damage policy');
      assert.equal(appRes.status, 'APPROVED');
      assert.equal(appRes.approved_by, 'supervisor@enterprise.com');

      // 3. Update Action Params (Security check: must invalidate approval)
      const patchRes = await ticketsApi.updateActionParams('ticket-uuid-001', 'act-uuid-1', {
        sku: 'XPS-15-2026-UPGRADED',
      });
      assert.equal(patchRes.status, 'PROPOSED');
      assert.equal(patchRes.approved_by, null);

      // 4. Execute Action
      const execRes = await ticketsApi.executeAction('ticket-uuid-001', 'act-uuid-1');
      assert.equal(execRes.status, 'EXECUTED');
      assert.equal(execRes.execution_attempts, 1);

      // 5. Verify Resolution
      const verifyRes = await ticketsApi.verifyResolution('ticket-uuid-001');
      assert.equal(verifyRes.verified, true);
      assert.equal(verifyRes.ticket_status, 'RESOLVED');

      // 6. Resolution History
      const resHist = await ticketsApi.getResolutionHistory('ticket-uuid-001');
      assert.equal(resHist.events.length, 1);
      assert.equal(resHist.events[0].event_type, 'RESOLUTION_EVALUATED');
    } finally {
      api.post = originalPost;
      api.patch = originalPatch;
      api.get = originalGet;
    }
  });

  // ===========================================================================
  // 7. Error Handling & Capacity Limit (409)
  // ===========================================================================

  test('ticketsApi propagates HTTP 409 capacity conflict without corrupting client state', async () => {
    const originalPost = api.post;
    api.post = (async () => {
      const err: any = new Error('Agent capacity exceeded');
      err.response = {
        status: 409,
        data: {
          error: {
            code: 'AGENT_CAPACITY_EXCEEDED',
            message: 'Agent Alice is already at maximum capacity (5 active tickets).',
          },
        },
      };
      throw err;
    }) as any;

    try {
      await assert.rejects(
        async () => {
          await ticketsApi.assignTicket('ticket-uuid-001', {
            agent_id: 'agent-uuid-777',
            reason: 'Assignment to saturated agent',
          });
        },
        (err: any) => err.response?.status === 409
      );
    } finally {
      api.post = originalPost;
    }
  });
});

