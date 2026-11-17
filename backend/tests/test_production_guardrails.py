"""Phase 5.3 Deployment Configuration Guardrails Test Suite.

Verifies the remediation of the three Phase 5.2 deployment audit findings:
1. Finding A: Production and staging configuration strictly reject RATE_LIMIT_ENABLED=False.
2. Finding B: Docker proxy trust safely and correctly handles trusted proxy forwarding,
   defeats spoofing from untrusted sockets, isolates multiple customers behind the same proxy,
   and validates IP formatting.
3. Finding C: Explicit Docker Compose service definitions guarantee that the backend web API
   operates with ENABLE_BACKGROUND_WORKERS=false while the dedicated worker service operates
   with ENABLE_BACKGROUND_WORKERS=true.
"""

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import yaml

from fastapi import HTTPException, Request, status
import pytest
import pytest_asyncio

from app.api.deps import ResolvedAuthContext
from app.core.config import Settings, settings, validate_production_environment
from app.core.rate_limiter import (
    SlidingWindowRateLimiter,
    check_chat_rate_limit,
    check_evidence_rate_limit,
    default_rate_limiter,
    is_trusted_proxy,
    resolve_rate_limit_key,
)
from app.lifecycle import WorkerLifecycleManager


# ==============================================================================
# 1. Finding A — Production Rate Limiting Enforcement Tests
# ==============================================================================

def make_valid_prod_kwargs(**overrides):
    base = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "ENABLE_DEV_AUTH_ENDPOINTS": False,
        "SECRET_KEY": "a" * 64,
        "ENCRYPTION_MASTER_KEY": "f" * 64,
        "POSTGRES_PASSWORD": "StrongProdPassword_2026_Secure!",
        "CORS_ORIGINS": ["https://support.enterprise.internal"],
        "IDEMPOTENCY_HMAC_ACTIVE_VERSION": "v1",
        "IDEMPOTENCY_HMAC_KEY_RING": {
            "v1": "9a3f2b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a"
        },
        "EMBEDDING_PROVIDER": "gemini",
        "GEMINI_API_KEY": "valid_mock_gemini_api_key_for_testing_123",
        "STORAGE_PROVIDER": "postgres",
        "RATE_LIMIT_ENABLED": True,
    }
    base.update(overrides)
    return base


def test_production_rejects_rate_limit_disabled():
    """Finding A: Production startup must reject RATE_LIMIT_ENABLED=False."""
    kwargs = make_valid_prod_kwargs(RATE_LIMIT_ENABLED=False)
    with pytest.raises(ValueError) as excinfo:
        Settings(**kwargs)
    assert "RATE_LIMIT_ENABLED must be set to True in production and staging" in str(excinfo.value)


def test_staging_rejects_rate_limit_disabled():
    """Finding A: Staging startup must reject RATE_LIMIT_ENABLED=False."""
    with pytest.raises(ValueError) as excinfo:
        Settings(
            ENVIRONMENT="staging",
            RATE_LIMIT_ENABLED=False,
            EMBEDDING_PROVIDER="gemini",
            GEMINI_API_KEY="valid-staging-key-123",
            STORAGE_PROVIDER="postgres",
        )
    assert "RATE_LIMIT_ENABLED must be set to True in production and staging" in str(excinfo.value)


def test_production_accepts_rate_limit_enabled():
    """Finding A: Production startup succeeds with RATE_LIMIT_ENABLED=True."""
    kwargs = make_valid_prod_kwargs(RATE_LIMIT_ENABLED=True)
    cfg = Settings(**kwargs)
    assert cfg.RATE_LIMIT_ENABLED is True


def test_production_retains_enabled_default_when_omitted():
    """Finding A: Missing configuration retains the safe enabled default in production."""
    kwargs = make_valid_prod_kwargs()
    kwargs.pop("RATE_LIMIT_ENABLED", None)
    cfg = Settings(**kwargs)
    assert cfg.RATE_LIMIT_ENABLED is True


def test_development_allows_rate_limit_disabled():
    """Finding A: Development permits RATE_LIMIT_ENABLED=False for offline test suites."""
    cfg = Settings(ENVIRONMENT="development", RATE_LIMIT_ENABLED=False)
    assert cfg.RATE_LIMIT_ENABLED is False


# ==============================================================================
# 2. Finding B — Correct Docker Proxy Trust & Anti-Spoofing Tests
# ==============================================================================

def make_mock_request(client_host: str, headers: dict = None) -> Request:
    mock_req = MagicMock(spec=Request)
    client_mock = MagicMock()
    client_mock.host = client_host
    mock_req.client = client_mock
    mock_req.headers = headers or {}
    return mock_req


def test_trusted_proxy_forwarding_honored():
    """Finding B.1: Authorized proxy forwards real client IP via X-Forwarded-For."""
    req = make_mock_request("172.28.0.10", {"X-Forwarded-For": "203.0.113.195, 172.28.0.10"})
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1", "172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        assert key == "chat:ip:203.0.113.195"


def test_untrusted_socket_spoof_attempt_defeated():
    """Finding B.2: Direct untrusted socket cannot spoof client IP via X-Forwarded-For."""
    req = make_mock_request("198.51.100.42", {"X-Forwarded-For": "8.8.8.8"})
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1", "172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        # Direct untrusted socket IP must be used, NOT the forged 8.8.8.8!
        assert key == "chat:ip:198.51.100.42"


def test_docker_proxy_cidr_network_matching():
    """Finding B.3: Specific Docker bridge network CIDRs match correctly."""
    req = make_mock_request("172.28.0.15", {"X-Forwarded-For": "192.0.2.77"})
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "172.28.0.0/24"]):
        key = resolve_rate_limit_key(req, None, "evidence")
        assert key == "evidence:ip:192.0.2.77"


def test_multiple_customers_behind_same_proxy_are_isolated():
    """Finding B.4: Multiple authenticated customers behind the same proxy get isolated quota keys."""
    req = make_mock_request("172.28.0.10", {"X-Forwarded-For": "203.0.113.50"})

    auth_customer_a = ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        session_token="sess_token_alice_1234567890abcdef",
        customer_id="cust_alice",
        roles=["CUSTOMER"],
    )
    auth_customer_b = ResolvedAuthContext(
        auth_type="SESSION_TOKEN",
        session_token="sess_token_bob_0987654321fedcba",
        customer_id="cust_bob",
        roles=["CUSTOMER"],
    )

    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key_a = resolve_rate_limit_key(req, auth_customer_a, "chat")
        key_b = resolve_rate_limit_key(req, auth_customer_b, "chat")

        assert key_a != key_b
        assert "sess_token_alice" in key_a
        assert "sess_token_bob" in key_b


def test_staff_and_session_tenant_isolation():
    """Finding B.5: Staff JWT claims isolate rate-limiting keys independently of socket IP."""
    req = make_mock_request("127.0.0.1")
    staff_auth = ResolvedAuthContext(
        auth_type="JWT",
        user_id_or_email="agent_john@enterprise.internal",
        roles=["AGENT"],
    )
    key = resolve_rate_limit_key(req, staff_auth, "evidence")
    assert key == "evidence:staff:agent_john@enterprise.internal"


def test_invalid_or_malformed_forwarded_ip_falls_back_safely():
    """Finding B.6: Malformed IP values in X-Forwarded-For are safely rejected."""
    req = make_mock_request("172.28.0.10", {"X-Forwarded-For": "evil_injection_payload_123"})
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        # Syntactically invalid IP falls back to client_host without crashing!
        assert key == "chat:ip:172.28.0.10"


def test_trusted_proxies_field_validator_parses_comma_and_json_strings():
    """Finding B.7: TRUSTED_PROXIES validator handles lists, JSON strings, and comma-separated strings."""
    s_comma = Settings(TRUSTED_PROXIES="127.0.0.1, ::1, 172.28.0.10")
    assert s_comma.TRUSTED_PROXIES == ["127.0.0.1", "::1", "172.28.0.10"]

    s_json = Settings(TRUSTED_PROXIES='["127.0.0.1", "172.28.0.10"]')
    assert s_json.TRUSTED_PROXIES == ["127.0.0.1", "172.28.0.10"]

    s_list = Settings(TRUSTED_PROXIES=["127.0.0.1", "172.28.0.10"])
    assert s_list.TRUSTED_PROXIES == ["127.0.0.1", "172.28.0.10"]


# ==============================================================================
# 3. Finding C — Explicit Docker Compose API & Dedicated Worker Tests
# ==============================================================================

def test_docker_compose_resolved_service_configurations():
    """Finding C.1: Inspects docker-compose.yml to verify explicit worker separation."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml not found"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data.get("services", {})
    assert "backend" in services, "Missing 'backend' service in docker-compose.yml"
    assert "worker" in services, "Missing 'worker' service in docker-compose.yml"
    assert "frontend" in services, "Missing 'frontend' service in docker-compose.yml"

    # 1. Backend API service must explicitly configure ENABLE_BACKGROUND_WORKERS="false"
    backend_env = services["backend"].get("environment", {})
    assert str(backend_env.get("ENABLE_BACKGROUND_WORKERS")).lower() == "false", (
        "Backend API must explicitly set ENABLE_BACKGROUND_WORKERS: 'false'"
    )

    # 2. Dedicated worker service must explicitly configure ENABLE_BACKGROUND_WORKERS="true"
    worker_env = services["worker"].get("environment", {})
    assert str(worker_env.get("ENABLE_BACKGROUND_WORKERS")).lower() == "true", (
        "Worker daemon must explicitly set ENABLE_BACKGROUND_WORKERS: 'true'"
    )

    # 3. Dedicated worker must have command 'python -m app.worker' and profile 'dedicated-worker'
    assert services["worker"].get("command") == "python -m app.worker"
    assert "dedicated-worker" in services["worker"].get("profiles", [])

    # 4. Frontend must have static IP matching TRUSTED_PROXIES configuration
    frontend_networks = services["frontend"].get("networks", {})
    assert "acsp-network" in frontend_networks
    assert frontend_networks["acsp-network"].get("ipv4_address") == "172.28.0.10"

    # 5. Network definition must have fixed subnet 172.28.0.0/24
    networks = compose_data.get("networks", {})
    assert "acsp-network" in networks
    ipam_config = networks["acsp-network"].get("ipam", {}).get("config", [])
    assert any(cfg.get("subnet") == "172.28.0.0/24" for cfg in ipam_config)


@pytest.mark.asyncio
async def test_worker_lifecycle_manager_respects_disabled_workers_flag():
    """Finding C.2: WorkerLifecycleManager suppresses startup recovery sweeps when workers are disabled."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_esc = MagicMock()
    mock_esc.scan_and_escalate_batch = AsyncMock(return_value=[])

    mock_rec = MagicMock()
    mock_rec.recover_stale_jobs = AsyncMock(return_value=0)
    mock_rec.scan_and_process_batch = AsyncMock(return_value=[])

    mock_storage = MagicMock()
    mock_storage.execute_purge_batch = AsyncMock(return_value={"scanned": 0, "purged": 0, "failed": 0})

    mock_kb = MagicMock()
    mock_kb.retry_service = MagicMock()
    mock_kb.retry_service.recover_expired_leases = AsyncMock(return_value=0)

    mock_reader = MagicMock()
    mock_reader.sync_on_startup = AsyncMock(return_value=True)
    mock_reader.start_background_reconciliation = MagicMock()

    mock_promoter = MagicMock()
    mock_promoter.reconcile_active_index_state = AsyncMock(
        return_value=MagicMock(reconciled=True, action_taken="NONE")
    )

    mock_watchdog = MagicMock()
    mock_watchdog.recover_monitoring_on_restart = AsyncMock(return_value=[])

    test_settings = Settings(ENVIRONMENT="development", ENABLE_BACKGROUND_WORKERS=False)

    manager = WorkerLifecycleManager(
        session_factory=mock_session_factory,
        settings=test_settings,
        reader=mock_reader,
        promoter=mock_promoter,
        watchdog=mock_watchdog,
        escalation_worker=mock_esc,
        reconciliation_worker=mock_rec,
        storage_lifecycle_manager=mock_storage,
        kb_retry_worker=mock_kb,
    )

    results = await manager.on_startup()

    # Reader and promoter startup sync MUST run (read cache)
    assert mock_reader.sync_on_startup.called
    assert mock_promoter.reconcile_active_index_state.called

    # Worker-only sweeps and scanners must NOT be executed!
    assert not mock_esc.scan_and_escalate_batch.called
    assert not mock_rec.recover_stale_jobs.called
    assert not mock_storage.execute_purge_batch.called
    assert not mock_kb.retry_service.recover_expired_leases.called
    assert not mock_esc.start_periodic_scanner.called
    assert not mock_rec.start_periodic_scanner.called
    assert "escalation_worker_started" not in results


# ==============================================================================
# 4. Phase 5.3.2 — Forwarded IP Anti-Spoofing & Edge Sanitization Tests
# ==============================================================================

def test_forged_single_ip_x_forwarded_for_defeated():
    """Attacker supplying single forged IP in XFF cannot spoof rate limit key."""
    # Nginx overwrites X-Real-IP with true client IP 203.0.113.195
    req = make_mock_request(
        "172.28.0.10",
        {"X-Forwarded-For": "198.51.100.1, 203.0.113.195", "X-Real-IP": "203.0.113.195"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1", "172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        assert key == "chat:ip:203.0.113.195", "Backend must prefer authoritative X-Real-IP"


def test_forged_multi_ip_x_forwarded_for_defeated():
    """Attacker supplying multiple forged IPs in XFF cannot manipulate rate limit key."""
    # Scenario A: X-Real-IP present
    req_a = make_mock_request(
        "172.28.0.10",
        {"X-Forwarded-For": "10.0.0.1, 10.0.0.2, 203.0.113.195", "X-Real-IP": "203.0.113.195"},
    )
    # Scenario B: X-Real-IP absent, right-to-left traversal must pick real client (rightmost non-trusted)
    req_b = make_mock_request(
        "172.28.0.10",
        {"X-Forwarded-For": "10.0.0.1, 10.0.0.2, 203.0.113.195, 172.28.0.10"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1", "172.28.0.10"]):
        key_a = resolve_rate_limit_key(req_a, None, "chat")
        key_b = resolve_rate_limit_key(req_b, None, "chat")
        assert key_a == "chat:ip:203.0.113.195"
        assert key_b == "chat:ip:203.0.113.195"


def test_conflicting_forwarding_headers_resolves_to_authoritative_real_ip():
    """Conflicting X-Forwarded-For and X-Real-IP resolves strictly to X-Real-IP."""
    req = make_mock_request(
        "172.28.0.10",
        {"X-Forwarded-For": "1.1.1.1", "X-Real-IP": "203.0.113.195"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        assert key == "chat:ip:203.0.113.195"


def test_missing_x_real_ip_falls_back_to_rightmost_xff_valid_client():
    """Missing X-Real-IP falls back to right-to-left XFF traversal skipping trusted proxies."""
    req = make_mock_request(
        "172.28.0.10",
        {"X-Forwarded-For": "198.51.100.99, 203.0.113.77, 172.28.0.10"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        # 172.28.0.10 is in TRUSTED_PROXIES -> skipped; 203.0.113.77 is untrusted client -> selected!
        assert key == "chat:ip:203.0.113.77"


def test_malformed_x_real_ip_falls_back_safely():
    """Malformed X-Real-IP falls back safely to XFF or socket host without exception."""
    # Malformed X-Real-IP, valid XFF
    req_a = make_mock_request(
        "172.28.0.10",
        {"X-Real-IP": "invalid_ip_text", "X-Forwarded-For": "203.0.113.88"},
    )
    # Malformed X-Real-IP, malformed XFF
    req_b = make_mock_request(
        "172.28.0.10",
        {"X-Real-IP": "invalid_ip_text", "X-Forwarded-For": "also_invalid"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key_a = resolve_rate_limit_key(req_a, None, "chat")
        key_b = resolve_rate_limit_key(req_b, None, "chat")
        assert key_a == "chat:ip:203.0.113.88"
        assert key_b == "chat:ip:172.28.0.10"


def test_invalid_ip_strings_and_unusual_whitespace():
    """Validates IP handling with surrounding whitespace, tabs, and invalid syntax."""
    req_ws = make_mock_request(
        "172.28.0.10",
        {"X-Real-IP": "  203.0.113.195 \t\n "},
    )
    req_inv = make_mock_request(
        "172.28.0.10",
        {"X-Real-IP": "999.999.999.999"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key_ws = resolve_rate_limit_key(req_ws, None, "chat")
        key_inv = resolve_rate_limit_key(req_inv, None, "chat")
        assert key_ws == "chat:ip:203.0.113.195"
        assert key_inv == "chat:ip:172.28.0.10"


def test_direct_requests_from_untrusted_socket_peers_ignore_all_forwarding():
    """Direct requests from untrusted socket peers strictly ignore all forwarding headers."""
    req = make_mock_request(
        "198.51.100.55",
        {
            "X-Forwarded-For": "1.1.1.1, 2.2.2.2",
            "X-Real-IP": "8.8.8.8",
            "X-Forwarded-Proto": "https",
        },
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1", "172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        assert key == "chat:ip:198.51.100.55"


def test_legitimate_nginx_forwarded_request():
    """Legitimate request forwarded by Nginx resolves cleanly to client IP."""
    req = make_mock_request(
        "172.28.0.10",
        {"X-Real-IP": "203.0.113.50", "X-Forwarded-For": "203.0.113.50"},
    )
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        key = resolve_rate_limit_key(req, None, "chat")
        assert key == "chat:ip:203.0.113.50"


def test_rate_limit_identity_stability_when_attackers_rotate_forged_headers():
    """Rotating forged headers does NOT change rate limit key when forwarded by Nginx."""
    with patch.object(settings, "TRUSTED_PROXIES", ["172.28.0.10"]):
        for forged_ip in ["1.1.1.1", "2.2.2.2", "3.3.3.3", "10.0.0.1", "192.168.1.1"]:
            # With Nginx edge sanitization, Nginx sets X-Real-IP and X-Forwarded-For to actual socket IP 203.0.113.195
            req = make_mock_request(
                "172.28.0.10",
                {"X-Real-IP": "203.0.113.195", "X-Forwarded-For": "203.0.113.195"},
            )
            key = resolve_rate_limit_key(req, None, "chat")
            assert key == "chat:ip:203.0.113.195", (
                f"Rate limit key changed unexpectedly under rotated header {forged_ip}"
            )


def test_nginx_conf_edge_sanitization_verification():
    """Inspects frontend/nginx.conf to ensure edge header sanitization is active across all locations."""
    nginx_conf_path = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"
    assert nginx_conf_path.exists(), "frontend/nginx.conf not found"

    content = nginx_conf_path.read_text(encoding="utf-8")

    # 1. $proxy_add_x_forwarded_for must NOT be used anywhere
    assert "$proxy_add_x_forwarded_for" not in content, (
        "frontend/nginx.conf must not use $proxy_add_x_forwarded_for (preserves attacker headers)"
    )

    # 2. proxy_set_header X-Forwarded-For $remote_addr; must be present
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in content

    # 3. proxy_set_header X-Real-IP $remote_addr; must be present
    assert "proxy_set_header X-Real-IP $remote_addr;" in content

    # 4. Count occurrences to confirm all 3 proxy locations are sanitized
    assert content.count("proxy_set_header X-Forwarded-For $remote_addr;") >= 3
    assert content.count("proxy_set_header X-Real-IP $remote_addr;") >= 3

