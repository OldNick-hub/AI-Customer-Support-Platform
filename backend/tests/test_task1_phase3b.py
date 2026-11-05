"""Day 05 — Task 1 Phase 3B: Maintenance Window Enforcement Tests.

Verifies:
1. Inside and outside maintenance windows.
2. Exact start and end boundaries (inclusive start, exclusive end).
3. Weekday restrictions.
4. Multiple non-overlapping daily permitted windows.
5. Blackout date enforcement and skipping.
6. Invalid IANA timezone rejection.
7. Naive timestamp rejection (demands timezone-aware datetime).
8. Daylight-saving transitions (EST vs EDT offset adjustment).
9. Overlapping schedule rejection.
10. Disabled maintenance enforcement bypass.
11. Emergency admin override.
12. Accurate next-window calculation.
13. Simulated evaluation timestamps without changing the system clock.
14. Fail-closed behavior on schedule corruption.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from app.core.config import Settings
from app.core.exceptions import MaintenanceWindowError
from app.modules.knowledge.maintenance import (
    MaintenanceSchedule,
    MaintenanceWindowEligibility,
    MaintenanceWindowService,
    TimeRange,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def default_utc_service():
    """Provides a default service configured for Sunday 02:00-04:00 UTC."""
    schedule = MaintenanceSchedule(
        enabled=True,
        timezone="UTC",
        allowed_weekdays=[6],  # Sunday
        time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
        blackout_dates=[],
    )
    return MaintenanceWindowService(schedule=schedule)


@pytest.fixture
def ny_timezone_service():
    """Provides a service configured in America/New_York (Sunday 02:00-04:00 local time)."""
    schedule = MaintenanceSchedule(
        enabled=True,
        timezone="America/New_York",
        allowed_weekdays=[6],  # Sunday
        time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
        blackout_dates=[],
    )
    return MaintenanceWindowService(schedule=schedule)


# =====================================================================
# Tests: Inside & Outside Windows
# =====================================================================

def test_inside_maintenance_window(default_utc_service):
    """Evaluation inside maintenance window permits promotion."""
    # Sunday Nov 1, 2026 at 03:00 UTC
    eval_time = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(evaluation_time=eval_time)

    assert eligibility.is_permitted is True
    assert "inside maintenance window" in eligibility.reason
    assert eligibility.current_window_end == datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc)
    assert eligibility.admin_override_used is False


def test_outside_maintenance_window_time(default_utc_service):
    """Evaluation on permitted weekday but outside permitted hours blocks promotion."""
    # Sunday Nov 1, 2026 at 05:00 UTC (window ended at 04:00 UTC)
    eval_time = datetime(2026, 11, 1, 5, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(evaluation_time=eval_time)

    assert eligibility.is_permitted is False
    assert "outside permitted time ranges" in eligibility.reason
    # Next window is Sunday Nov 8, 2026 at 02:00 UTC
    assert eligibility.next_window_start == datetime(2026, 11, 8, 2, 0, 0, tzinfo=timezone.utc)
    assert eligibility.next_window_end == datetime(2026, 11, 8, 4, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Tests: Boundary Precision
# =====================================================================

def test_exact_start_and_end_boundaries(default_utc_service):
    """Start time is inclusive; end time is exclusive."""
    # Exact start boundary (02:00:00 UTC) -> PERMITTED
    start_time = datetime(2026, 11, 1, 2, 0, 0, tzinfo=timezone.utc)
    elig_start = default_utc_service.check_eligibility(evaluation_time=start_time)
    assert elig_start.is_permitted is True

    # 1 microsecond before end boundary (03:59:59.999999 UTC) -> PERMITTED
    almost_end = datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=timezone.utc)
    elig_almost = default_utc_service.check_eligibility(evaluation_time=almost_end)
    assert elig_almost.is_permitted is True

    # Exact end boundary (04:00:00 UTC) -> BLOCKED
    end_time = datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc)
    elig_end = default_utc_service.check_eligibility(evaluation_time=end_time)
    assert elig_end.is_permitted is False
    assert "outside permitted time ranges" in elig_end.reason


# =====================================================================
# Tests: Weekday Restrictions & Multiple Windows
# =====================================================================

def test_weekday_restrictions(default_utc_service):
    """Evaluation on disallowed weekdays blocks promotion and computes next window."""
    # Wednesday Nov 4, 2026 at 03:00 UTC
    wednesday_time = datetime(2026, 11, 4, 3, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(evaluation_time=wednesday_time)

    assert eligibility.is_permitted is False
    assert "Wednesday is outside permitted weekdays" in eligibility.reason
    assert eligibility.next_window_start == datetime(2026, 11, 8, 2, 0, 0, tzinfo=timezone.utc)


def test_multiple_permitted_windows_same_day():
    """Supports multiple non-overlapping time windows on permitted days."""
    schedule = MaintenanceSchedule(
        enabled=True,
        timezone="UTC",
        allowed_weekdays=[1],  # Tuesday
        time_ranges=[
            TimeRange(start_time=time(1, 0), end_time=time(3, 0)),
            TimeRange(start_time=time(13, 0), end_time=time(15, 0)),
        ],
    )
    service = MaintenanceWindowService(schedule=schedule)

    # Tuesday Nov 3, 2026 at 02:00 UTC (inside Window 1)
    elig1 = service.check_eligibility(datetime(2026, 11, 3, 2, 0, 0, tzinfo=timezone.utc))
    assert elig1.is_permitted is True
    assert elig1.current_window_end == datetime(2026, 11, 3, 3, 0, 0, tzinfo=timezone.utc)

    # Tuesday Nov 3, 2026 at 08:00 UTC (between Window 1 and Window 2)
    elig_between = service.check_eligibility(datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc))
    assert elig_between.is_permitted is False
    assert elig_between.next_window_start == datetime(2026, 11, 3, 13, 0, 0, tzinfo=timezone.utc)

    # Tuesday Nov 3, 2026 at 14:00 UTC (inside Window 2)
    elig2 = service.check_eligibility(datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc))
    assert elig2.is_permitted is True
    assert elig2.current_window_end == datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Tests: Blackout Dates & Next Window Calculation
# =====================================================================

def test_blackout_dates_and_advancement(default_utc_service):
    """Blackout dates block promotion and advance next_window_start to the following cycle."""
    # Configure Sunday Nov 1, 2026 as blackout date
    default_utc_service.update_schedule(
        MaintenanceSchedule(
            enabled=True,
            timezone="UTC",
            allowed_weekdays=[6],
            time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
            blackout_dates=[date(2026, 11, 1)],
        )
    )

    eval_time = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(evaluation_time=eval_time)

    assert eligibility.is_permitted is False
    assert "2026-11-01 is a designated blackout date" in eligibility.reason
    # Next window correctly advances to Sunday Nov 8, 2026
    assert eligibility.next_window_start == datetime(2026, 11, 8, 2, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Tests: Validation & Fail-Closed Behavior
# =====================================================================

def test_invalid_iana_timezone_rejection():
    """Invalid timezone identifier raises validation ValueError."""
    with pytest.raises(ValueError) as exc_info:
        MaintenanceSchedule(timezone="Fantasy/Narnia")
    assert "Invalid IANA timezone identifier" in str(exc_info.value)


def test_naive_timestamp_rejection(default_utc_service):
    """Passing a naive datetime raises ValueError."""
    naive_dt = datetime(2026, 11, 1, 3, 0, 0)
    with pytest.raises(ValueError) as exc_info:
        default_utc_service.check_eligibility(evaluation_time=naive_dt)
    assert "Evaluation timestamp must be timezone-aware" in str(exc_info.value)


def test_overlapping_time_ranges_rejection():
    """Overlapping daily time ranges raise validation ValueError."""
    with pytest.raises(ValueError) as exc_info:
        MaintenanceSchedule(
            time_ranges=[
                TimeRange(start_time=time(2, 0), end_time=time(4, 0)),
                TimeRange(start_time=time(3, 30), end_time=time(5, 0)),
            ]
        )
    assert "Overlapping maintenance time ranges detected" in str(exc_info.value)


def test_inverted_time_range_rejection():
    """start_time >= end_time raises validation ValueError."""
    with pytest.raises(ValueError) as exc_info:
        TimeRange(start_time=time(4, 0), end_time=time(2, 0))
    assert "start_time (04:00:00) must be strictly earlier than end_time (02:00:00)" in str(exc_info.value)


# =====================================================================
# Tests: Daylight Saving Transitions
# =====================================================================

def test_daylight_saving_transitions(ny_timezone_service):
    """Accurately calculates UTC offsets across winter EST (-5) and summer EDT (-4)."""
    # Winter EST: Sunday Jan 18, 2026 (02:00-04:00 EST corresponds to 07:00-09:00 UTC)
    winter_in_utc = datetime(2026, 1, 18, 7, 30, 0, tzinfo=timezone.utc)
    elig_winter = ny_timezone_service.check_eligibility(evaluation_time=winter_in_utc)
    assert elig_winter.is_permitted is True
    assert "02:30:00 EST" in elig_winter.evaluated_local_time

    # Summer EDT: Sunday Jul 19, 2026 (02:00-04:00 EDT corresponds to 06:00-08:00 UTC)
    summer_in_utc = datetime(2026, 7, 19, 6, 30, 0, tzinfo=timezone.utc)
    elig_summer = ny_timezone_service.check_eligibility(evaluation_time=summer_in_utc)
    assert elig_summer.is_permitted is True
    assert "02:30:00 EDT" in elig_summer.evaluated_local_time

    # Summer EDT at 08:30 UTC corresponds to 04:30 EDT (outside window)
    summer_outside_utc = datetime(2026, 7, 19, 8, 30, 0, tzinfo=timezone.utc)
    elig_summer_out = ny_timezone_service.check_eligibility(evaluation_time=summer_outside_utc)
    assert elig_summer_out.is_permitted is False
    assert "outside permitted time ranges" in elig_summer_out.reason


# =====================================================================
# Tests: Admin Override & Disabled Switch
# =====================================================================

def test_disabled_maintenance_enforcement(default_utc_service):
    """When maintenance enforcement is disabled, any time is permitted."""
    default_utc_service.update_schedule(
        MaintenanceSchedule(enabled=False, timezone="UTC")
    )
    # Wednesday Nov 4 at 14:00 UTC
    eval_time = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(evaluation_time=eval_time)

    assert eligibility.is_permitted is True
    assert "disabled by configuration" in eligibility.reason


def test_admin_emergency_override(default_utc_service):
    """Admin override permits promotion outside window."""
    wednesday_time = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    eligibility = default_utc_service.check_eligibility(
        evaluation_time=wednesday_time,
        admin_override=True,
    )

    assert eligibility.is_permitted is True
    assert "emergency administrative override" in eligibility.reason
    assert eligibility.admin_override_used is True


def test_enforce_promotion_eligibility_raises_error(default_utc_service):
    """enforce_promotion_eligibility() raises MaintenanceWindowError (409) when blocked."""
    wednesday_time = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(MaintenanceWindowError) as exc_info:
        default_utc_service.enforce_promotion_eligibility(evaluation_time=wednesday_time)
    assert exc_info.value.status_code == 409
    assert "Wednesday is outside permitted weekdays" in exc_info.value.message
    assert exc_info.value.detail["next_window_start"] is not None


def test_fail_closed_on_corrupted_schedule(monkeypatch):
    """Service fails closed if an invalid timezone is injected at runtime."""
    service = MaintenanceWindowService()
    # Artificially set invalid timezone attribute
    monkeypatch.setattr(service.schedule, "timezone", "Invalid_Timezone_Name")

    eval_time = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
    eligibility = service.check_eligibility(evaluation_time=eval_time)

    assert eligibility.is_permitted is False
    assert "fail-closed" in eligibility.reason

