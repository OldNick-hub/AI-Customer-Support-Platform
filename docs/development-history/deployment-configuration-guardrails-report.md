# Phase 5.3 — Deployment Configuration Guardrail Remediation Report

**Project:** AI Customer Support Platform  
**Mode:** Controlled Implementation & Regression Verification  
**Date:** November 8, 2026  
**Status:** COMPLETED & VERIFIED  
**Lifetime Cost:** ₹0.00  

---

## 1. Executive Summary

Phase 5.3 addressed the three operational and architectural deployment guardrail gaps identified in the Phase 5.2 read-only deployment verification audit:

1. **Finding A — Production Rate Limiting Enforcement:** Staging and production runtime environments strictly reject configurations attempting to disable HTTP request rate limiting (`RATE_LIMIT_ENABLED=False`), throwing explicit startup validation exceptions. Development retains local configurability.
2. **Finding B — Narrow Reverse Proxy Trust & Client IP Validation:** Reverse proxy trust in `app.core.rate_limiter` is strictly scoped to explicit trusted proxy addresses (`127.0.0.1`, `::1`, and the deterministic Nginx container IP `172.28.0.10` on the isolated bridge subnet `172.28.0.0/24`). Broad CIDRs (`172.0.0.0/8`) and wildcards are completely eliminated. Client IP addresses extracted from `X-Forwarded-For` are strictly validated using standard library `ipaddress` routines, falling back to direct socket client addresses when malformed. Authenticated customer session tokens and staff IDs take precedence over IP-based limits.
3. **Finding C — Explicit API & Dedicated Worker Service Separation:** In `docker-compose.yml`, the `backend` API service explicitly configures `ENABLE_BACKGROUND_WORKERS: "false"`, preventing duplicate background sweeps and race conditions. The dedicated `worker` service under the `dedicated-worker` Compose profile explicitly configures `ENABLE_BACKGROUND_WORKERS: "true"`. `WorkerLifecycleManager` in `backend/app/lifecycle.py` respects this setting during startup, executing reader and watchdog cache synchronization while conditionally skipping worker-only startup recovery sweeps when background workers are disabled.

---

## 2. Remediation Details

### Finding A: Strict Rate Limiting Validation in Production and Staging
- **File:** [`backend/app/core/config.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/core/config.py)
- **Changes:**
  - Added strict guardrail in `validate_production_configuration()`: If `ENVIRONMENT` is `production` or `staging` and `RATE_LIMIT_ENABLED` is `False`, raises `ValueError("RATE_LIMIT_ENABLED must be set to True in production and staging environments.")`.
  - Added non-raising audit check in `validate_production_environment()`: Flags `"RATE_LIMIT_ENABLED is False (must be True in production and staging)"`.
  - Added `@field_validator("TRUSTED_PROXIES", mode="before")` on `Settings` to parse list structures, JSON strings, or comma-delimited strings with safe defaults.

### Finding B: Reverse Proxy Trust & Rate Limiting Identity Scoping
- **File:** [`backend/app/core/rate_limiter.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/core/rate_limiter.py)
- **Changes:**
  - Added helper function `is_trusted_proxy(client_host: Optional[str], trusted_proxies: Sequence[str]) -> bool` using `ipaddress.ip_address` and `ipaddress.ip_network` for exact and CIDR matching without broad network assumptions.
  - Refactored `resolve_rate_limit_key()`:
    1. Authenticated customer session tokens (`X-Session-Token`) are parsed first (`customer_session:{token}`), ensuring customers sharing a NAT or corporate IP do not starve each other.
    2. Authenticated user IDs from request state or JWT claims are parsed next (`staff_user:{user_id}`).
    3. Unauthenticated requests resolve to IP:
       - If direct socket client is in `TRUSTED_PROXIES`, parses `X-Forwarded-For` leftmost IP.
       - Extracted IP is validated via `ipaddress.ip_address(raw_ip)`. If malformed or spoofed, falls back to direct socket client IP.
       - Returns `ip:{validated_ip}`.

### Finding C: Explicit API & Worker Service Definitions
- **Files:** [`docker-compose.yml`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/docker-compose.yml), [`backend/app/lifecycle.py`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/backend/app/lifecycle.py), [`.env.example`](file:///c:/Users/bhavs/OneDrive/Desktop/AI-Customer-Support-Platform/.env.example)
- **Changes:**
  - In `docker-compose.yml`:
    - Fixed network bridge subnet: `acsp-network` configured with IPAM `172.28.0.0/24`.
    - `acsp-frontend` (Nginx) assigned static IP `172.28.0.10`.
    - `backend` API service configured with `ENABLE_BACKGROUND_WORKERS: "false"` and `TRUSTED_PROXIES: '["127.0.0.1", "::1", "172.28.0.10"]'`.
    - `worker` service configured with `ENABLE_BACKGROUND_WORKERS: "true"`, `command: python -m app.worker`, and assigned profile `dedicated-worker`.
  - In `backend/app/lifecycle.py`:
    - In `WorkerLifecycleManager.on_startup()`, wrapped escalation recovery, reconciliation recovery, evidence retention sweeps, and KB ingestion retry scanner under `if enable_workers:` check. Reader cache hydration and watchdog checks remain active for API instances.
  - In `.env.example`:
    - Documented `ENABLE_BACKGROUND_WORKERS`, `RATE_LIMIT_ENABLED`, and `TRUSTED_PROXIES` settings.

---

## 3. Verification & Test Results

### 1. Dedicated Production Guardrails Suite
- **File:** `backend/tests/test_production_guardrails.py` (New suite)
- **Tests:** 14 passed in 1.16s
  - `test_production_rejects_rate_limit_disabled`
  - `test_staging_rejects_rate_limit_disabled`
  - `test_development_allows_rate_limit_disabled`
  - `test_production_environment_validator_flags_disabled_rate_limit`
  - `test_rate_limiter_trusted_proxy_valid_client_ip`
  - `test_rate_limiter_untrusted_proxy_ignores_forwarded_for`
  - `test_rate_limiter_malformed_forwarded_for_falls_back`
  - `test_rate_limiter_session_token_isolation`
  - `test_trusted_proxies_field_validator_parsing`
  - `test_docker_compose_has_explicit_worker_settings`
  - `test_docker_compose_has_narrow_trusted_proxies`
  - `test_worker_lifecycle_manager_respects_disabled_workers_flag`
  - `test_worker_lifecycle_manager_runs_workers_when_enabled`
  - `test_compose_syntax_and_profile_configs`

### 2. Targeted Production Configuration Suite
- **File:** `backend/tests/test_production_configuration.py`
- **Tests:** 44 passed in 3.50s (including rate limit rejection assertions)

### 3. Full Platform Backend Regression Suite
- **Command:** `.venv\Scripts\python.exe -m pytest backend/tests`
- **Collected:** 1,044 items
- **Passed:** 1,044 (100% green)
- **Failed:** 0
- **Skipped:** 0
- **Duration:** 176.64s (2m 56s)

### 4. Docker Compose Validation
- `docker compose config`: Exit code 0 (valid syntax).
- `docker compose --profile dedicated-worker config`: Exit code 0 (valid syntax with worker profile).

---

## 4. Backups and Git Integrity

- **Pre-Remediation Workspace Backup:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_pre_phase5_3_workspace.zip` (1,487 files, 7,393,872 bytes). Verified with `zipfile.testzip() == None`.
- **Pre-Remediation Git Bundle:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_pre_phase5_3.bundle` (1,732,326 bytes, 4 refs verified).
- **Post-Remediation Workspace Backup:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_post_phase5_3_workspace.zip` (1,495 files, 7,419,103 bytes). Verified with `zipfile.testzip() == None`.
- **Post-Remediation Git Bundle:** `C:\Users\bhavs\OneDrive\Desktop\ai_customer_support_backups\ai_customer_support_post_phase5_3.bundle` (1,117,801 bytes, complete history verified).
- **Git Stage Status:** Exactly **0 staged files** (`git diff --cached` output empty). Untracked files and working tree edits preserved.
- **Lifetime Financial Cost:** ₹0.00.

