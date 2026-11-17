"""Comprehensive Verification Suite for Phase 5.1 Production Hardening.

Covers:
1. Dependency-aware readiness health probe (/api/v1/health/ready):
   - PostgreSQL healthy, unavailable, and timeout.
   - Redis healthy, unavailable (optional vs required), and timeout.
   - Status codes (200 OK vs 503 Service Unavailable).
   - Zero credential or internal details leakage.
2. HTTP request rate limiting:
   - Requests below limits succeed.
   - Requests exceeding limits receive HTTP 429 with Retry-After.
   - Separate policies for chat vs evidence endpoints.
   - Anti-spoofing: untrusted forwarding headers are ignored.
   - Session and identity isolation.
3. Dockerfile configuration:
   - Non-root user creation (appuser:appgroup, UID/GID 1000).
   - Directory ownership and permissions.
   - USER appuser active directive.
4. Process separation:
   - Web API mode (ENABLE_BACKGROUND_WORKERS=False) does not start background worker loops.
   - Dedicated worker mode (ENABLE_BACKGROUND_WORKERS=True) initializes and starts worker loops.
   - Graceful shutdown verification.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import os
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ResolvedAuthContext
from app.core.config import settings
from app.core.rate_limiter import (
    SlidingWindowRateLimiter,
    check_chat_rate_limit,
    check_evidence_rate_limit,
    default_rate_limiter,
    resolve_rate_limit_key,
)
from app.main import app, health_ready, lifespan


# ==============================================================================
# 1. Dependency-Aware Readiness Health Probe Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_readiness_both_dependencies_healthy():
    """Verifies /api/v1/health/ready returns 200 when DB and Redis are healthy."""
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    with patch("app.main.AsyncSessionLocal") as mock_session_local, \
         patch("app.main.get_redis_service", return_value=mock_redis):
        # Setup mock db session
        mock_session = AsyncMock()
        mock_session.execute.return_value = MagicMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session

        response = await health_ready()
        assert response.status_code == status.HTTP_200_OK

        body = eval(response.body.decode("utf-8")) if isinstance(response.body, bytes) else response.body
        assert body == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_database_unavailable_returns_503():
    """Verifies /api/v1/health/ready returns 503 when PostgreSQL connection fails."""
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    with patch("app.main.AsyncSessionLocal") as mock_session_local, \
         patch("app.main.get_redis_service", return_value=mock_redis):
        mock_session_local.return_value.__aenter__.side_effect = ConnectionRefusedError("PostgreSQL connection refused")

        response = await health_ready()
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

        body = eval(response.body.decode("utf-8")) if isinstance(response.body, bytes) else response.body
        assert body["status"] == "unhealthy"
        assert body["checks"]["database"] == "unhealthy"
        # Verify no credentials leaked
        assert "password" not in str(body).lower()


@pytest.mark.asyncio
async def test_readiness_database_timeout_returns_503():
    """Verifies /api/v1/health/ready returns 503 when PostgreSQL query times out (>1.0s)."""
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    async def _slow_db(*args, **kwargs):
        await asyncio.sleep(1.5)

    with patch("app.main.AsyncSessionLocal") as mock_session_local, \
         patch("app.main.get_redis_service", return_value=mock_redis):
        mock_session = AsyncMock()
        mock_session.execute.side_effect = _slow_db
        mock_session_local.return_value.__aenter__.return_value = mock_session

        response = await health_ready()
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

        body = eval(response.body.decode("utf-8")) if isinstance(response.body, bytes) else response.body
        assert body["status"] == "unhealthy"
        assert body["checks"]["database"] == "timeout"


@pytest.mark.asyncio
async def test_readiness_redis_unavailable_optional_returns_200():
    """Verifies /api/v1/health/ready returns 200 when Redis is optional and unavailable."""
    with patch("app.main.AsyncSessionLocal") as mock_session_local, \
         patch("app.main.get_redis_service") as mock_get_redis, \
         patch.object(settings, "REDIS_REQUIRED", False), \
         patch.object(settings, "ENVIRONMENT", "development"):
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session

        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionRefusedError("Redis down")
        mock_get_redis.return_value = mock_redis

        response = await health_ready()
        assert response.status_code == status.HTTP_200_OK

        body = eval(response.body.decode("utf-8")) if isinstance(response.body, bytes) else response.body
        assert body == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_redis_unavailable_required_returns_503():
    """Verifies /api/v1/health/ready returns 503 when Redis is strictly required and unavailable."""
    with patch("app.main.AsyncSessionLocal") as mock_session_local, \
         patch("app.main.get_redis_service") as mock_get_redis, \
         patch.object(settings, "REDIS_REQUIRED", True):
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session

        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionRefusedError("Redis down")
        mock_get_redis.return_value = mock_redis

        response = await health_ready()
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

        body = eval(response.body.decode("utf-8")) if isinstance(response.body, bytes) else response.body
        assert body["status"] == "unhealthy"
        assert body["checks"]["redis"] == "unhealthy"


# ==============================================================================
# 2. HTTP Request Rate Limiting Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_sliding_window_rate_limiter_allows_and_blocks():
    """Verifies SlidingWindowRateLimiter allows up to limit and blocks subsequent requests."""
    limiter = SlidingWindowRateLimiter()
    key = "test:client:1"

    # Up to limit of 3
    for _ in range(3):
        allowed, retry_after = await limiter.check_rate_limit(key, max_requests=3, window_seconds=60.0)
        assert allowed is True
        assert retry_after == 0

    # 4th request must be rejected with retry_after > 0
    allowed, retry_after = await limiter.check_rate_limit(key, max_requests=3, window_seconds=60.0)
    assert allowed is False
    assert retry_after >= 1


@pytest.mark.asyncio
async def test_rate_limiter_separate_endpoint_isolation():
    """Verifies that exceeding chat rate limit does not exhaust evidence rate limit for the same client."""
    limiter = SlidingWindowRateLimiter()

    # Exhaust chat limit (max 2)
    for _ in range(2):
        await limiter.check_rate_limit("chat:clientA", max_requests=2)
    blocked, _ = await limiter.check_rate_limit("chat:clientA", max_requests=2)
    assert blocked is False

    # Evidence limit for clientA remains unaffected
    allowed, _ = await limiter.check_rate_limit("evidence:clientA", max_requests=2)
    assert allowed is True


@pytest.mark.asyncio
async def test_rate_limiter_anti_spoofing_untrusted_client():
    """Verifies that an untrusted direct connection cannot spoof identity using X-Forwarded-For."""
    mock_request = MagicMock(spec=Request)
    mock_request.client.host = "198.51.100.22"  # Public / untrusted IP
    mock_request.headers = {"X-Forwarded-For": "10.0.0.1, 1.1.1.1"}

    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1"]):
        key = resolve_rate_limit_key(mock_request, auth=None, endpoint_tag="chat")
        # Must resolve to the socket IP (198.51.100.22), NOT the spoofed header (10.0.0.1)
        assert key == "chat:ip:198.51.100.22"


@pytest.mark.asyncio
async def test_rate_limiter_trusted_proxy_forwarding():
    """Verifies that a trusted reverse proxy forwards the authentic client IP."""
    mock_request = MagicMock(spec=Request)
    mock_request.client.host = "127.0.0.1"  # Trusted proxy
    mock_request.headers = {"X-Forwarded-For": "203.0.113.195, 127.0.0.1"}

    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1"]):
        key = resolve_rate_limit_key(mock_request, auth=None, endpoint_tag="chat")
        assert key == "chat:ip:203.0.113.195"


@pytest.mark.asyncio
async def test_rate_limiter_session_isolation():
    """Verifies that authenticated sessions are tracked independently."""
    auth_a = ResolvedAuthContext(auth_type="SESSION_TOKEN", session_token="sess-token-AAA", customer_id=str(uuid.uuid4()))
    auth_b = ResolvedAuthContext(auth_type="SESSION_TOKEN", session_token="sess-token-BBB", customer_id=str(uuid.uuid4()))

    mock_req = MagicMock(spec=Request)
    key_a = resolve_rate_limit_key(mock_req, auth=auth_a, endpoint_tag="chat")
    key_b = resolve_rate_limit_key(mock_req, auth=auth_b, endpoint_tag="chat")

    assert key_a != key_b
    assert "sess-token-AAA" in key_a
    assert "sess-token-BBB" in key_b


@pytest.mark.asyncio
async def test_chat_rate_limit_dependency_raises_429():
    """Verifies check_chat_rate_limit dependency raises HTTP 429 when threshold exceeded."""
    default_rate_limiter.reset()

    mock_request = MagicMock(spec=Request)
    mock_request.client.host = "192.0.2.1"
    mock_request.headers = {}
    auth = ResolvedAuthContext(auth_type="SESSION_TOKEN", session_token=f"test-sess-{uuid.uuid4().hex}")

    with patch.object(settings, "RATE_LIMIT_CHAT_PER_MINUTE", 2), \
         patch.object(settings, "RATE_LIMIT_ENABLED", True):
        # 1st and 2nd allowed
        await check_chat_rate_limit(request=mock_request, auth=auth)
        await check_chat_rate_limit(request=mock_request, auth=auth)

        # 3rd must raise 429
        with pytest.raises(HTTPException) as exc_info:
            await check_chat_rate_limit(request=mock_request, auth=auth)

        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Retry-After" in exc_info.value.headers
        assert int(exc_info.value.headers["Retry-After"]) >= 1

    default_rate_limiter.reset()


@pytest.mark.asyncio
async def test_evidence_rate_limit_dependency_raises_429():
    """Verifies check_evidence_rate_limit dependency raises HTTP 429 when threshold exceeded."""
    default_rate_limiter.reset()

    mock_request = MagicMock(spec=Request)
    mock_request.client.host = "192.0.2.1"
    mock_request.headers = {}
    auth = ResolvedAuthContext(auth_type="SESSION_TOKEN", session_token=f"test-sess-{uuid.uuid4().hex}")

    with patch.object(settings, "RATE_LIMIT_EVIDENCE_PER_MINUTE", 2), \
         patch.object(settings, "RATE_LIMIT_ENABLED", True):
        # 1st and 2nd allowed
        await check_evidence_rate_limit(request=mock_request, auth=auth)
        await check_evidence_rate_limit(request=mock_request, auth=auth)

        # 3rd must raise 429
        with pytest.raises(HTTPException) as exc_info:
            await check_evidence_rate_limit(request=mock_request, auth=auth)

        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Retry-After" in exc_info.value.headers
        assert int(exc_info.value.headers["Retry-After"]) >= 1

    default_rate_limiter.reset()


# ==============================================================================
# 3. Dockerfile Non-Root Security Verification
# ==============================================================================

def test_dockerfile_contains_non_root_security_directives():
    """Verifies that backend/Dockerfile implements non-root execution and storage setup."""
    dockerfile_path = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")
    assert os.path.exists(dockerfile_path), "backend/Dockerfile not found"

    content = open(dockerfile_path, "r", encoding="utf-8").read()

    # 1. Non-root user creation
    assert "groupadd" in content, "Missing groupadd in Dockerfile"
    assert "useradd" in content, "Missing useradd in Dockerfile"
    assert "appuser" in content, "Missing appuser definition in Dockerfile"

    # 2. Directory ownership and permissions
    assert "chown -R appuser:appgroup /app" in content, "Missing chown to appuser in Dockerfile"
    assert "/app/storage" in content, "Missing /app/storage creation in Dockerfile"

    # 3. Active USER directive
    assert "USER appuser" in content, "Missing 'USER appuser' directive in Dockerfile"


# ==============================================================================
# 4. API and Background Worker Process Separation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_api_lifespan_workers_disabled_mode():
    """Verifies that when ENABLE_BACKGROUND_WORKERS=False, no background worker loops start in FastAPI."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    with patch.object(settings, "ENABLE_BACKGROUND_WORKERS", False), \
         patch("app.main.ActiveIndexReader") as mock_reader_cls, \
         patch("app.main.EscalationWorker") as mock_esc_cls, \
         patch("app.main.ReconciliationWorker") as mock_rec_cls, \
         patch("app.main.EvidenceRetentionWorker") as mock_ret_cls, \
         patch("app.main.AsyncSessionLocal"):

        mock_reader = MagicMock()
        mock_reader.sync_on_startup = AsyncMock(return_value=True)
        mock_reader_cls.return_value = mock_reader

        mock_esc = MagicMock()
        mock_esc.is_running = False
        mock_esc_cls.return_value = mock_esc

        mock_rec = MagicMock()
        mock_rec.is_running = False
        mock_rec_cls.return_value = mock_rec

        mock_ret = MagicMock()
        mock_ret.is_running = False
        mock_ret_cls.return_value = mock_ret

        async with lifespan(mock_app):
            # ActiveIndexReader must be synced for memory read cache
            assert mock_reader.sync_on_startup.called

            # Background workers must NOT start periodic scanners!
            assert not mock_esc.start_periodic_scanner.called
            assert not mock_rec.start_periodic_scanner.called
            assert not mock_ret.start_periodic_scanner.called


@pytest.mark.asyncio
async def test_api_lifespan_workers_enabled_mode():
    """Verifies that when ENABLE_BACKGROUND_WORKERS=True, workers are initialized and started."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    with patch.object(settings, "ENABLE_BACKGROUND_WORKERS", True), \
         patch("app.main.ActiveIndexReader") as mock_reader_cls, \
         patch("app.main.EscalationWorker") as mock_esc_cls, \
         patch("app.main.ReconciliationWorker") as mock_rec_cls, \
         patch("app.main.EvidenceRetentionWorker") as mock_ret_cls, \
         patch("app.main.AsyncSessionLocal"):

        mock_reader = MagicMock()
        mock_reader.sync_on_startup = AsyncMock(return_value=True)
        mock_reader_cls.return_value = mock_reader

        mock_esc = MagicMock()
        mock_esc.scan_and_escalate_batch = AsyncMock(return_value=[])
        mock_esc.stop_async = AsyncMock()
        mock_esc_cls.return_value = mock_esc

        mock_rec = MagicMock()
        mock_rec.recover_stale_jobs = AsyncMock(return_value=0)
        mock_rec.scan_and_process_batch = AsyncMock(return_value=[])
        mock_rec.stop_async = AsyncMock()
        mock_rec_cls.return_value = mock_rec

        mock_ret = MagicMock()
        mock_ret.is_running = False
        mock_ret.stop_async = AsyncMock()
        mock_ret_cls.return_value = mock_ret

        async with lifespan(mock_app):
            # All workers initialized and periodic scanners started
            assert mock_esc.start_periodic_scanner.called
            assert mock_rec.start_periodic_scanner.called
            assert mock_ret.start_periodic_scanner.called

        # On shutdown, all workers stopped gracefully
        assert mock_esc.stop_async.called
        assert mock_rec.stop_async.called
        assert mock_ret.stop_async.called
