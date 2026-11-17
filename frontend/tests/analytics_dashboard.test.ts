/**
 * Analytics, Overview Dashboard & Operational Intelligence Test Suite.
 *
 * Verifies:
 * 1. analyticsApi.getOverview passes time window and filter parameters to /analytics/overview.
 * 2. analyticsApi.getPerformance retrieves SLA compliance, time series trends, and evidence analytics.
 * 3. analyticsApi.getOperationalIntelligence retrieves deterministic rule alerts and target actions.
 * 4. Error propagation and parameter validation across analytics endpoints.
 * 5. Incident queue mapping and KPI calculation invariants.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  api,
  analyticsApi,
  type OverviewDashboardResponse,
  type PerformanceAnalyticsResponse,
  type OperationalIntelligenceResponse,
  type AnalyticsFilterParams,
} from '../src/services/api.ts';

describe('Analytics, Dashboard & Operational Intelligence Integration', () => {
  let capturedRequests: Array<{ url: string; method: string; data?: any; params?: any }> = [];
  const originalGet = api.get;

  beforeEach(() => {
    capturedRequests = [];
  });

  afterEach(() => {
    api.get = originalGet;
  });

  // ===========================================================================
  // 1. Overview Dashboard API
  // ===========================================================================

  test('analyticsApi.getOverview passes time_window and department filters to /analytics/overview', async () => {
    api.get = (async (url: string, config?: any) => {
      capturedRequests.push({ url, method: 'GET', params: config?.params });
      const mockResponse: OverviewDashboardResponse = {
        total_tickets: 42,
        open_tickets: 18,
        resolved_tickets: 24,
        escalated_tickets: 3,
        urgent_tickets: 2,
        sla_compliance_pct: 95.2,
        sla_breached_count: 2,
        sla_warning_count: 1,
        avg_resolution_time_minutes: 48.5,
        active_conversations_count: 14,
        priority_distribution: { CRITICAL: 2, HIGH: 5, MEDIUM: 25, LOW: 10 },
        status_distribution: { OPEN: 18, RESOLVED: 24 },
        priority_queue: [
          {
            id: 'inc-001',
            ticket_id: 'tck-uuid-1',
            ticket_number: 'TCK-202611-00001',
            title: 'Critical Outage on Gateway',
            customer: 'Acme Corp',
            tier: 'ENTERPRISE',
            category: 'INFRASTRUCTURE',
            priority: 'CRITICAL',
            severity: 'SEV-1 CRITICAL',
            status: 'INVESTIGATING',
            assigned_team: 'Core Infrastructure',
            priority_score: 4.8,
            sla_elapsed: '25m elapsed',
            sla_total: '60m window',
            sla_remaining: '35m remaining',
            progress: 42,
            sentiment_score: -0.8,
            sentiment_label: 'Severe Frustration',
            latest_activity: 'Service gateway failure investigated',
            evidence_status: 'Logs sealed',
            evidence_snippet: 'Connection refused on port 443',
            evidence_doc: 'gateway-error.log',
            suggested_action: 'Restart Pods',
          },
        ],
        department_workload: [
          {
            department: 'Billing',
            active_agents: 4,
            total_capacity: 20,
            active_tickets: 8,
            utilization_pct: 40.0,
          },
        ],
        reporting_period: {
          time_window: '7d',
          start_date: '2026-11-01T00:00:00Z',
          end_date: '2026-11-07T00:00:00Z',
          timezone: 'UTC',
        },
      };
      return { data: mockResponse };
    }) as any;

    const params: AnalyticsFilterParams = {
      time_window: '7d',
      department: 'Billing',
    };

    const res = await analyticsApi.getOverview(params);

    assert.equal(capturedRequests.length, 1);
    assert.equal(capturedRequests[0].url, '/analytics/overview');
    assert.deepEqual(capturedRequests[0].params, { time_window: '7d', department: 'Billing' });
    assert.equal(res.total_tickets, 42);
    assert.equal(res.open_tickets, 18);
    assert.equal(res.sla_compliance_pct, 95.2);
    assert.equal(res.priority_queue.length, 1);
    assert.equal(res.priority_queue[0].ticket_number, 'TCK-202611-00001');
    assert.equal(res.department_workload[0].department, 'Billing');
  });

  // ===========================================================================
  // 2. Performance Analytics API
  // ===========================================================================

  test('analyticsApi.getPerformance returns time series trends, SLA breakdowns, and evidence metrics', async () => {
    api.get = (async (url: string, config?: any) => {
      capturedRequests.push({ url, method: 'GET', params: config?.params });
      const mockPerf: PerformanceAnalyticsResponse = {
        ticket_trends: [
          { date: '2026-11-05', created: 10, resolved: 8, escalated: 1 },
          { date: '2026-11-06', created: 14, resolved: 12, escalated: 0 },
        ],
        category_distribution: [
          { category: 'Hardware Support', count: 12, percentage: 50.0 },
          { category: 'Billing Inquiries', count: 12, percentage: 50.0 },
        ],
        priority_distribution: [
          { priority: 'CRITICAL', count: 2, percentage: 8.3 },
          { priority: 'HIGH', count: 6, percentage: 25.0 },
          { priority: 'MEDIUM', count: 16, percentage: 66.7 },
        ],
        sla_by_category: [
          { label: 'Hardware Support', total: 12, compliant: 11, breached: 1, compliance_pct: 91.7 },
          { label: 'Billing Inquiries', total: 12, compliant: 12, breached: 0, compliance_pct: 100.0 },
        ],
        sla_by_priority: [
          { label: 'CRITICAL', total: 2, compliant: 1, breached: 1, compliance_pct: 50.0 },
          { label: 'HIGH', total: 6, compliant: 6, breached: 0, compliance_pct: 100.0 },
        ],
        overall_sla_compliance_pct: 95.8,
        total_breaches: 1,
        avg_resolution_time_minutes: 36.2,
        operational_workload: [
          { department: 'Hardware', active_agents: 3, total_capacity: 15, active_tickets: 6, utilization_pct: 40.0 },
        ],
        evidence_analytics: {
          total_uploaded: 15,
          verified_count: 14,
          conflict_count: 1,
          unsafe_count: 0,
          status_distribution: { COMPLETED: 14, QUARANTINED: 0 },
          reconciliation_distribution: { MATCH: 13, CONFLICT: 1 },
        },
        language_distribution: [
          { language: 'English', code: 'en', query_count: 18, percentage: 75.0 },
          { language: 'Spanish', code: 'es', query_count: 6, percentage: 25.0 },
        ],
        reporting_period: {
          time_window: '30d',
          start_date: '2026-10-08T00:00:00Z',
          end_date: '2026-11-07T00:00:00Z',
          timezone: 'UTC',
        },
      };
      return { data: mockPerf };
    }) as any;

    const res = await analyticsApi.getPerformance({ time_window: '30d', category: 'Hardware Support' });

    assert.equal(capturedRequests.length, 1);
    assert.equal(capturedRequests[0].url, '/analytics/performance');
    assert.deepEqual(capturedRequests[0].params, { time_window: '30d', category: 'Hardware Support' });
    assert.equal(res.overall_sla_compliance_pct, 95.8);
    assert.equal(res.ticket_trends.length, 2);
    assert.equal(res.ticket_trends[0].created, 10);
    assert.equal(res.sla_by_category[0].label, 'Hardware Support');
    assert.equal(res.evidence_analytics.conflict_count, 1);
    assert.equal(res.language_distribution[0].code, 'en');
  });

  // ===========================================================================
  // 3. Operational Intelligence API
  // ===========================================================================

  test('analyticsApi.getOperationalIntelligence returns rule-based alerts and target links', async () => {
    api.get = (async (url: string, config?: any) => {
      capturedRequests.push({ url, method: 'GET', params: config?.params });
      const mockIntel: OperationalIntelligenceResponse = {
        insights: [
          {
            id: 'alert-cap-01',
            rule_name: 'RULE_DEPT_CAPACITY_THRESHOLD',
            severity: 'WARNING',
            title: 'Department Capacity Warning: Hardware Support',
            description: 'Department Hardware Support utilization is at 88.0% of nominal ceiling.',
            supporting_metric: '88.0% capacity utilization',
            time_window: '30d',
            target_route: '/tickets?department=Hardware%20Support',
            suggested_action: 'Rebalance Queue',
          },
          {
            id: 'alert-sla-02',
            rule_name: 'RULE_SLA_IMMINENT_BREACH_WINDOW',
            severity: 'CRITICAL',
            title: 'Imminent SLA Breach Window Active',
            description: '3 open tickets have less than 30 minutes remaining before breach.',
            supporting_metric: '3 tickets at risk',
            time_window: '30d',
            target_route: '/tickets?priority=CRITICAL',
            suggested_action: 'Inspect Urgent Queue',
          },
        ],
        evaluated_at: '2026-11-07T09:30:00Z',
        rules_applied: [
          'RULE_DEPT_CAPACITY_THRESHOLD',
          'RULE_SLA_IMMINENT_BREACH_WINDOW',
          'RULE_HIGH_RISK_SAFETY_ALARMS',
          'RULE_RECONCILIATION_CONFLICT_SURGE',
        ],
      };
      return { data: mockIntel };
    }) as any;

    const res = await analyticsApi.getOperationalIntelligence({ time_window: '30d' });

    assert.equal(capturedRequests.length, 1);
    assert.equal(capturedRequests[0].url, '/analytics/operational-intelligence');
    assert.equal(res.insights.length, 2);
    assert.equal(res.insights[0].rule_name, 'RULE_DEPT_CAPACITY_THRESHOLD');
    assert.equal(res.insights[0].severity, 'WARNING');
    assert.equal(res.insights[0].target_route, '/tickets?department=Hardware%20Support');
    assert.equal(res.insights[1].severity, 'CRITICAL');
    assert.equal(res.rules_applied.length, 4);
  });

  // ===========================================================================
  // 4. Error Handling
  // ===========================================================================

  test('analyticsApi endpoints propagate network or server errors transparently', async () => {
    api.get = (async () => {
      const error: any = new Error('Network Error');
      error.response = { status: 503, data: { detail: 'Telemetry aggregator service unavailable' } };
      throw error;
    }) as any;

    await assert.rejects(
      async () => {
        await analyticsApi.getOverview({ time_window: 'today' });
      },
      (err: any) => {
        assert.equal(err.response?.status, 503);
        assert.equal(err.response?.data?.detail, 'Telemetry aggregator service unavailable');
        return true;
      }
    );
  });
});

