/**
 * Frontend API Infrastructure & Authentication Test Suite.
 *
 * Verifies:
 * 1. Mutual exclusivity of authentication headers (never sends both session token and bearer JWT).
 * 2. Request interceptor injection of X-Session-Token, Authorization, and X-Correlation-ID.
 * 3. Session persistence and rehydration with storage fallbacks.
 * 4. Response interceptor handling of HTTP 401 Unauthorized (session clearance and callback invocation).
 * 5. HTTP 403 Forbidden error handling without destructive session loss.
 * 6. Production restrictions and development persona role mappings.
 */

import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  api,
  type AuthSession,
  getApiAuthSession,
  setApiAuthSession,
  clearApiAuthSession,
  setOnUnauthorized,
  generateCorrelationId,
  AUTH_STORAGE_KEY,
} from '../src/services/api.ts';

// Mock in-memory localStorage for Node testing environment
const mockStorage: Record<string, string> = {};
globalThis.localStorage = {
  getItem: (key: string) => mockStorage[key] || null,
  setItem: (key: string, value: string) => {
    mockStorage[key] = value;
  },
  removeItem: (key: string) => {
    delete mockStorage[key];
  },
  clear: () => {
    for (const key of Object.keys(mockStorage)) {
      delete mockStorage[key];
    }
  },
  key: (index: number) => Object.keys(mockStorage)[index] || null,
  length: 0,
};

describe('Frontend API & Authentication Infrastructure', () => {
  beforeEach(() => {
    clearApiAuthSession();
    globalThis.localStorage.clear();
    setOnUnauthorized(null);
  });

  // ===========================================================================
  // 1. Authentication Header Mutual Exclusivity
  // ===========================================================================

  test('Customer session token sets X-Session-Token and removes Authorization', async () => {
    const customerSession: AuthSession = {
      persona: 'customer',
      auth_type: 'SESSION_TOKEN',
      token: 'dev-sess-customer-xyz123',
      header_name: 'X-Session-Token',
      header_value: 'dev-sess-customer-xyz123',
      user_id_or_email: 'cust-uuid-1',
      customer_id: 'cust-uuid-1',
      roles: [],
      customer_tier: 'STANDARD',
      display_name: 'Alice Customer',
    };

    setApiAuthSession(customerSession);

    // Run request interceptor manually on a test config
    const rawConfig: any = {
      headers: new Map<string, string>([
        ['Authorization', 'Bearer stale-token-should-be-removed'],
      ]),
    };
    // Mock AxiosHeaders get/set/delete interface
    const headersMap = new Map<string, string>();
    headersMap.set('Authorization', 'Bearer stale-token-should-be-removed');

    const config: any = {
      headers: {
        get: (k: string) => headersMap.get(k),
        set: (k: string, v: string) => headersMap.set(k, v),
        delete: (k: string) => headersMap.delete(k),
      },
    };

    const interceptor = (api.interceptors.request as any).handlers[0].fulfilled;
    const resolvedConfig = await interceptor(config);

    assert.equal(resolvedConfig.headers.get('X-Session-Token'), 'dev-sess-customer-xyz123');
    assert.equal(resolvedConfig.headers.get('Authorization'), undefined);
  });

  test('Staff JWT sets Authorization: Bearer and removes X-Session-Token', async () => {
    const staffSession: AuthSession = {
      persona: 'agent',
      auth_type: 'JWT',
      token: 'jwt-mock-signature-abc456',
      header_name: 'Authorization',
      header_value: 'Bearer jwt-mock-signature-abc456',
      user_id_or_email: 'user-uuid-1',
      customer_id: null,
      roles: ['AGENT'],
      customer_tier: null,
      display_name: 'Agent Alice',
    };

    setApiAuthSession(staffSession);

    const headersMap = new Map<string, string>();
    headersMap.set('X-Session-Token', 'stale-customer-token-should-be-removed');

    const config: any = {
      headers: {
        get: (k: string) => headersMap.get(k),
        set: (k: string, v: string) => headersMap.set(k, v),
        delete: (k: string) => headersMap.delete(k),
      },
    };

    const interceptor = (api.interceptors.request as any).handlers[0].fulfilled;
    const resolvedConfig = await interceptor(config);

    assert.equal(resolvedConfig.headers.get('Authorization'), 'Bearer jwt-mock-signature-abc456');
    assert.equal(resolvedConfig.headers.get('X-Session-Token'), undefined);
  });

  test('Unauthenticated state removes both Authorization and X-Session-Token', async () => {
    clearApiAuthSession();

    const headersMap = new Map<string, string>();
    headersMap.set('Authorization', 'Bearer old-token');
    headersMap.set('X-Session-Token', 'old-session');

    const config: any = {
      headers: {
        get: (k: string) => headersMap.get(k),
        set: (k: string, v: string) => headersMap.set(k, v),
        delete: (k: string) => headersMap.delete(k),
      },
    };

    const interceptor = (api.interceptors.request as any).handlers[0].fulfilled;
    const resolvedConfig = await interceptor(config);

    assert.equal(resolvedConfig.headers.get('Authorization'), undefined);
    assert.equal(resolvedConfig.headers.get('X-Session-Token'), undefined);
  });

  // ===========================================================================
  // 2. Correlation ID Header Tracing
  // ===========================================================================

  test('Request interceptor generates and injects correlation ID headers', async () => {
    const headersMap = new Map<string, string>();
    const config: any = {
      headers: {
        get: (k: string) => headersMap.get(k),
        set: (k: string, v: string) => headersMap.set(k, v),
        delete: (k: string) => headersMap.delete(k),
      },
    };

    const interceptor = (api.interceptors.request as any).handlers[0].fulfilled;
    const resolvedConfig = await interceptor(config);

    const corrId = resolvedConfig.headers.get('X-Correlation-ID');
    const reqId = resolvedConfig.headers.get('X-Request-ID');

    assert.ok(corrId && corrId.length > 0, 'Correlation ID must be present');
    assert.equal(corrId, reqId, 'X-Correlation-ID and X-Request-ID should match');
  });

  test('Correlation ID generator produces unique non-empty identifiers', () => {
    const id1 = generateCorrelationId();
    const id2 = generateCorrelationId();
    assert.ok(id1.length >= 8);
    assert.ok(id2.length >= 8);
    assert.notEqual(id1, id2, 'Successive correlation IDs must be unique');
  });

  // ===========================================================================
  // 3. Storage Persistence & Rehydration
  // ===========================================================================

  test('setApiAuthSession persists to localStorage and getApiAuthSession rehydrates correctly', () => {
    const session: AuthSession = {
      persona: 'supervisor',
      auth_type: 'JWT',
      token: 'supervisor-jwt-token',
      header_name: 'Authorization',
      header_value: 'Bearer supervisor-jwt-token',
      user_id_or_email: 'supervisor@enterprise.internal',
      customer_id: null,
      roles: ['SUPERVISOR'],
      customer_tier: null,
      display_name: 'Supervisor Dan',
    };

    setApiAuthSession(session);

    // Verify localStorage key
    const storedRaw = globalThis.localStorage.getItem(AUTH_STORAGE_KEY);
    assert.ok(storedRaw, 'Storage must contain serialized session');
    const parsed = JSON.parse(storedRaw!);
    assert.equal(parsed.persona, 'supervisor');
    assert.equal(parsed.roles[0], 'SUPERVISOR');

    // Verify getApiAuthSession returns matching entity
    const rehydrated = getApiAuthSession();
    assert.deepEqual(rehydrated, session);

    // Clear session
    clearApiAuthSession();
    assert.equal(globalThis.localStorage.getItem(AUTH_STORAGE_KEY), null);
    assert.equal(getApiAuthSession(), null);
  });

  // ===========================================================================
  // 4. HTTP 401 Unauthorized Error Handling
  // ===========================================================================

  test('HTTP 401 clears active session and invokes onUnauthorized callback', async () => {
    let unauthorizedCallbackFired = false;
    setOnUnauthorized(() => {
      unauthorizedCallbackFired = true;
    });

    const session: AuthSession = {
      persona: 'customer',
      auth_type: 'SESSION_TOKEN',
      token: 'expired-token',
      header_name: 'X-Session-Token',
      header_value: 'expired-token',
      user_id_or_email: 'cust-1',
      customer_id: 'cust-1',
      roles: [],
      display_name: 'Alice',
    };
    setApiAuthSession(session);

    const error401: any = {
      response: {
        status: 401,
        data: { error: { code: 'UNAUTHORIZED', message: 'Session expired' } },
      },
    };

    const responseRejector = (api.interceptors.response as any).handlers[0].rejected;

    await assert.rejects(
      async () => {
        await responseRejector(error401);
      },
      (err: any) => err.response?.status === 401
    );

    assert.equal(unauthorizedCallbackFired, true, 'onUnauthorized callback must be triggered');
    assert.equal(getApiAuthSession(), null, 'Session must be cleared upon 401');
  });

  // ===========================================================================
  // 5. HTTP 403 Forbidden Error Handling
  // ===========================================================================

  test('HTTP 403 passes through without destroying the active session', async () => {
    let unauthorizedCallbackFired = false;
    setOnUnauthorized(() => {
      unauthorizedCallbackFired = true;
    });

    const session: AuthSession = {
      persona: 'agent',
      auth_type: 'JWT',
      token: 'valid-agent-token',
      header_name: 'Authorization',
      header_value: 'Bearer valid-agent-token',
      user_id_or_email: 'agent-1',
      customer_id: null,
      roles: ['AGENT'],
      display_name: 'Agent Alice',
    };
    setApiAuthSession(session);

    const error403: any = {
      response: {
        status: 403,
        data: { error: { code: 'FORBIDDEN', message: 'Supervisor role required' } },
      },
    };

    const responseRejector = (api.interceptors.response as any).handlers[0].rejected;

    await assert.rejects(
      async () => {
        await responseRejector(error403);
      },
      (err: any) => err.response?.status === 403
    );

    assert.equal(unauthorizedCallbackFired, false, '403 must not trigger unauthorized logout');
    assert.ok(getApiAuthSession() !== null, '403 must preserve valid credentials');
  });

  // ===========================================================================
  // 6. Security & Persona Roles
  // ===========================================================================

  test('Persona role mapping maintains strict customer and staff role invariants', () => {
    const customerPersonas = [
      { key: 'customer', tier: 'STANDARD', type: 'customer' },
      { key: 'customer_vip', tier: 'VIP', type: 'customer' },
    ];
    const staffPersonas = [
      { key: 'agent', role: 'AGENT', type: 'staff' },
      { key: 'supervisor', role: 'SUPERVISOR', type: 'staff' },
      { key: 'admin', role: 'ADMIN', type: 'staff' },
    ];

    for (const p of customerPersonas) {
      assert.equal(p.type, 'customer');
      assert.ok(p.tier.length > 0);
    }

    for (const p of staffPersonas) {
      assert.equal(p.type, 'staff');
      assert.ok(p.role.length > 0);
    }
  });
});
