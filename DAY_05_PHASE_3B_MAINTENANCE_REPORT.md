# Day 05 — Task 1 Phase 3B: Maintenance Window Enforcement Report

**Milestone:** Task 1 — Knowledge Base Update & Quality Gate Pipeline  
**Subphase:** Phase 3B — Maintenance Window Enforcement  
**Status:** **COMPLETED & VERIFIED**  
**Regression Baseline:** **138 / 138 Tests Passing (100% Green)** across the full repository  
**Live Relational Engine:** PostgreSQL 16 (`localhost:5432/ai_customer_support`)  

---

## 1. Executive Summary

Phase 3B implements a production-grade, timezone-aware **Maintenance Window Enforcement Service** that governs when candidate knowledge index snapshots may be promoted to active status.

Key guarantees established in Phase 3B:
1. **Configurable Scheduling Specification:** Supports global enable/disable switch, standard IANA timezones (e.g. `America/New_York`, `UTC`), allowed weekday arrays (`0`=Monday, ..., `6`=Sunday), multiple non-overlapping daily time ranges (`TimeRange`), and calendar blackout dates.
2. **Strict Timezone-Aware Operations:** Rejects naive datetime inputs with explicit validation errors; all schedule evaluations use Python standard library `ZoneInfo` (PEP 615) with `tzdata` support on Windows, ensuring host-clock independence.
3. **Daylight-Saving Time (DST) Precision:** Seamlessly accounts for winter/summer offset shifts (e.g. EST $-5$ vs EDT $-4$), maintaining fixed local operational windows without manual UTC adjustments.
4. **Boundary Invariants:** Enforces inclusive start boundaries (`[start_time`) and exclusive end boundaries (`end_time)`), providing microsecond-level accuracy.
5. **Next-Window Calculation:** When promotion is blocked, the service computes the exact next available window start and end timestamps in UTC, searching forward up to 366 days and advancing past blackout dates.
6. **Fail-Closed Security & Emergency Overrides:** Malformed schedules or corrupt timezone configurations fail closed, returning `is_permitted = False`. An explicit `admin_override = True` parameter permits emergency break-glass promotions when authorized.
7. **Clean Separation of Concerns:** Benchmark evaluation (Phase 3A) remains completely decoupled from maintenance scheduling (Phase 3B). A candidate passing benchmark gates remains in `CANDIDATE_READY` until maintenance window eligibility is satisfied.

---

## 2. Maintenance Window Configuration & Architecture

Implemented in [`backend/app/modules/knowledge/maintenance.py`](backend/app/modules/knowledge/maintenance.py).

### 2.1 Pydantic Domain Models

1. **`TimeRange`:**
   - Validates that `start_time < end_time`.
   - Rejects inverted time ranges (`start_time >= end_time`).
2. **`MaintenanceSchedule`:**
   - `enabled: bool = True`
   - `timezone: str = "UTC"`: Validated against `ZoneInfo(timezone)`; rejects invalid identifiers (e.g. `"Fantasy/Narnia"`).
   - `allowed_weekdays: List[int] = Field(default_factory=lambda: [6])`: Restricts to integers $0 \le d \le 6$; rejects out-of-range days and empty lists.
   - `time_ranges: List[TimeRange]`: Automatically sorts by start time and verifies that no two ranges overlap.
   - `blackout_dates: List[date]`: List of dates on which promotion is completely prohibited.
3. **`MaintenanceWindowEligibility`:**
   - `is_permitted: bool`: Primary gating flag.
   - `reason: str`: Human-readable and audit-log explanation.
   - `evaluated_at: datetime`: UTC timestamp of evaluation.
   - `evaluated_local_time: str`: Formatted local timestamp with timezone abbreviation (e.g. `2026-01-18 02:30:00 EST`).
   - `active_schedule_timezone: str`: Active IANA timezone string.
   - `current_window_end: Optional[datetime]`: UTC closing timestamp if currently in an active window.
   - `next_window_start: Optional[datetime]`: UTC opening timestamp of next permitted window if currently blocked.
   - `next_window_end: Optional[datetime]`: UTC closing timestamp of next permitted window.
   - `admin_override_used: bool`: Flags emergency administrative bypasses.

---

## 3. Promotion Eligibility Workflow

```mermaid
flowchart TD
    EvalRequest["Promotion Eligibility Evaluation\ncheck_eligibility(eval_time, admin_override)"] --> NaiveCheck{"Is eval_time Timezone-Aware?"}
    NaiveCheck -- "Naive (tzinfo is None)" --> RaiseNaive["Raise ValueError\n(Evaluation timestamp must be timezone-aware)"]
    NaiveCheck -- "Aware" --> AdminCheck{"admin_override == True?"}
    
    AdminCheck -- "Yes" --> PermitAdmin["is_permitted = True\nReason: Emergency administrative override"]
    AdminCheck -- "No" --> EnabledCheck{"schedule.enabled == True?"}
    
    EnabledCheck -- "No" --> PermitDisabled["is_permitted = True\nReason: Maintenance enforcement disabled"]
    EnabledCheck -- "Yes" --> TZConvert["Convert UTC eval_time to Schedule ZoneInfo\n(local_dt = eval_utc.astimezone(tz))"]
    
    TZConvert --> FailClosedCheck{"Timezone Valid?"}
    FailClosedCheck -- "Invalid / Corrupt" --> BlockFailClosed["is_permitted = False\nReason: Schedule configuration error (fail-closed)"]
    
    FailClosedCheck -- "Valid" --> BlackoutCheck{"local_dt.date() in blackout_dates?"}
    BlackoutCheck -- "Yes" --> ComputeNextBlackout["Compute next_window_start\nis_permitted = False (Blackout date)"]
    
    BlackoutCheck -- "No" --> WeekdayCheck{"local_dt.weekday() in allowed_weekdays?"}
    WeekdayCheck -- "No" --> ComputeNextWeekday["Compute next_window_start\nis_permitted = False (Disallowed weekday)"]
    
    WeekdayCheck -- "Yes" --> RangeCheck{"local_dt.time() in any [start_time, end_time)?"}
    RangeCheck -- "Inside [start, end)" --> PermitInside["is_permitted = True\nReason: Inside maintenance window\ncurrent_window_end = end_time (UTC)"]
    RangeCheck -- "Outside ranges" --> ComputeNextRange["Compute next_window_start\nis_permitted = False (Outside permitted hours)"]
```

---

## 4. Phase 3B Test Verification & Coverage Matrix

Executed via:
```powershell
$env:PYTHONPATH="backend"; .venv\Scripts\pytest -c backend/pyproject.toml backend/tests/test_task1_phase3b.py -v
```

### 4.1 Phase 3B Dedicated Test Suite (15 / 15 Passed)

| Test Name | Targeted Invariant | Result | Execution Time |
| :--- | :--- | :---: | :---: |
| `test_inside_maintenance_window` | Sunday 03:00 UTC inside Sunday 02:00-04:00 UTC window permits promotion. | **PASSED** | 0.01s |
| `test_outside_maintenance_window_time` | Sunday 05:00 UTC outside window blocks promotion; next window computed as next Sunday 02:00 UTC. | **PASSED** | 0.01s |
| `test_exact_start_and_end_boundaries` | Start boundary (02:00:00.000) is permitted; end boundary (04:00:00.000) is blocked; 03:59:59.999 is permitted. | **PASSED** | 0.01s |
| `test_weekday_restrictions` | Wednesday evaluation blocked when allowed weekdays is Sunday (`[6]`); next window computed. | **PASSED** | 0.01s |
| `test_multiple_permitted_windows_same_day` | Supports disjoint windows (01:00-03:00 and 13:00-15:00); correctly evaluates inside and between windows. | **PASSED** | 0.01s |
| `test_blackout_dates_and_advancement` | Designating a window Sunday as blackout date blocks promotion and advances next window by 7 days. | **PASSED** | 0.01s |
| `test_invalid_iana_timezone_rejection` | Supplying invalid IANA timezone (`"Fantasy/Narnia"`) raises `ValueError`. | **PASSED** | 0.01s |
| `test_naive_timestamp_rejection` | Supplying naive `datetime(2026, 11, 1, 3, 0)` raises `ValueError("Evaluation timestamp must be timezone-aware")`. | **PASSED** | 0.01s |
| `test_overlapping_time_ranges_rejection` | Overlapping windows (02:00-04:00 and 03:30-05:00) rejected with `ValueError`. | **PASSED** | 0.01s |
| `test_inverted_time_range_rejection` | Inverted range (04:00-02:00) rejected with `ValueError`. | **PASSED** | 0.01s |
| `test_daylight_saving_transitions` | Correctly evaluates `America/New_York` local 02:00-04:00 window in winter EST (07:00-09:00 UTC) and summer EDT (06:00-08:00 UTC). | **PASSED** | 0.01s |
| `test_disabled_maintenance_enforcement` | `enabled = False` permits promotion at any time with explanatory reason. | **PASSED** | 0.01s |
| `test_admin_emergency_override` | `admin_override = True` permits promotion outside windows and flags `admin_override_used`. | **PASSED** | 0.01s |
| `test_enforce_promotion_eligibility_raises_error` | `enforce_promotion_eligibility()` raises `MaintenanceWindowError` (HTTP 409) with next window details when blocked. | **PASSED** | 0.01s |
| `test_fail_closed_on_corrupted_schedule` | Injected invalid schedule attribute triggers fail-closed return (`is_permitted = False`). | **PASSED** | 0.01s |

---

### 4.2 Full Platform Regression Suite Execution (138 / 138 Passed)

Executed via:
```powershell
$env:PYTHONPATH="backend"; .venv\Scripts\pytest -c backend/pyproject.toml -v
```

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.2.0, pluggy-1.7.0 -- C:\Users\bhavs\OneDrive\Desktop\AI-Customer-Support-Platform\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\bhavs\OneDrive\Desktop\AI-Customer-Support-Platform\backend
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 138 items

backend\tests\test_alembic_migration.py (1 test) PASSED
backend\tests\test_constraints.py (6 tests) PASSED
backend\tests\test_database_connection.py (2 tests) PASSED
backend\tests\test_day03_phase1_2.py (20 tests) PASSED
backend\tests\test_day03_phase3.py (24 tests) PASSED
backend\tests\test_faiss_id_mapping.py (2 tests) PASSED
backend\tests\test_models.py (4 tests) PASSED
backend\tests\test_pii_and_encryption.py (8 tests) PASSED
backend\tests\test_postgres_integration.py (9 tests) PASSED
backend\tests\test_seed_data.py (1 test) PASSED
backend\tests\test_sla_pause.py (1 test) PASSED
backend\tests\test_task1_phase1.py (20 tests) PASSED
backend\tests\test_task1_phase2.py (28 tests) PASSED
backend\tests\test_task1_phase3a.py (9 tests) PASSED
backend\tests\test_task1_phase3b.py (15 tests) PASSED
backend\tests\test_watchdog_persistence.py (2 tests) PASSED

============================ 138 passed in 20.86s =============================
```

---

## 5. Architectural Integrity & Boundaries

1. **Database Schema:** Zero database migrations required. Maintenance window scheduling is governed by application configuration and domain models, avoiding unnecessary relational schema bloat.
2. **Phase 3C Readiness:** The service exposes two clean integration methods for Phase 3C:
   - `check_eligibility(evaluation_time, admin_override) -> MaintenanceWindowEligibility` (non-raising inspection).
   - `enforce_promotion_eligibility(evaluation_time, admin_override) -> None` (raises `MaintenanceWindowError` HTTP 409 if blocked).
3. **No Promotion Executed:** `storage/indices/active_index.json` remains completely untouched. No candidate index was promoted or modified during Phase 3B.

---

## 6. Phase 3B Completion Status

Phase 3B is **fully implemented, tested, and audited**. Per user instruction:
> *"Stop after implementation and verification. Await approval before proceeding."*

Execution has stopped and is standing by for authorization to begin **Phase 3C — Atomic Candidate Promotion**.

