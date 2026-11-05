"""Timezone-Aware Maintenance Window Enforcement Service.

Enforces configurable, timezone-aware maintenance windows determining when knowledge index
promotions are permitted. Rejects naive evaluation timestamps, detects malformed/overlapping
schedules, handles daylight-saving transitions, supports blackout dates and emergency overrides,
and provides next-window calculation for blocked promotions.
"""

from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import MaintenanceWindowError
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


# =====================================================================
# Pydantic Schemas for Schedule Configuration & Eligibility
# =====================================================================

class TimeRange(BaseModel):
    """Permitted daily time window [start_time, end_time)."""

    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_range_order(self) -> "TimeRange":
        if self.start_time >= self.end_time:
            raise ValueError(
                f"Invalid time range: start_time ({self.start_time}) must be strictly earlier "
                f"than end_time ({self.end_time})."
            )
        return self


class MaintenanceSchedule(BaseModel):
    """Configuration specification for index promotion maintenance windows."""

    enabled: bool = True
    timezone: str = "UTC"
    allowed_weekdays: List[int] = Field(default_factory=lambda: [6])  # 0=Monday, ..., 6=Sunday
    time_ranges: List[TimeRange] = Field(
        default_factory=lambda: [TimeRange(start_time=time(2, 0), end_time=time(4, 0))]
    )
    blackout_dates: List[date] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_iana_timezone(cls, v: str) -> str:
        clean = v.strip()
        try:
            ZoneInfo(clean)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid IANA timezone identifier: '{v}' ({exc})") from exc
        return clean

    @field_validator("allowed_weekdays")
    @classmethod
    def validate_weekdays(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("allowed_weekdays cannot be empty when maintenance window is configured.")
        for d in v:
            if not (0 <= d <= 6):
                raise ValueError(f"Invalid weekday {d}. Allowed values: 0 (Monday) to 6 (Sunday).")
        unique_days = sorted(list(set(v)))
        return unique_days

    @field_validator("time_ranges")
    @classmethod
    def validate_time_ranges(cls, v: List[TimeRange]) -> List[TimeRange]:
        if not v:
            raise ValueError("time_ranges list cannot be empty.")
        # Sort by start_time to check for overlaps
        sorted_ranges = sorted(v, key=lambda tr: tr.start_time)
        for i in range(1, len(sorted_ranges)):
            prev_range = sorted_ranges[i - 1]
            curr_range = sorted_ranges[i]
            # Overlap occurs if current start is before previous end
            if curr_range.start_time < prev_range.end_time:
                raise ValueError(
                    f"Overlapping maintenance time ranges detected: "
                    f"[{prev_range.start_time}-{prev_range.end_time}] conflicts with "
                    f"[{curr_range.start_time}-{curr_range.end_time}]."
                )
        return sorted_ranges


class MaintenanceWindowEligibility(BaseModel):
    """Structured decision explaining whether candidate promotion is currently permitted."""

    is_permitted: bool
    reason: str
    evaluated_at: datetime
    evaluated_local_time: str
    active_schedule_timezone: str
    current_window_end: Optional[datetime] = None
    next_window_start: Optional[datetime] = None
    next_window_end: Optional[datetime] = None
    admin_override_used: bool = False


# =====================================================================
# Maintenance Window Service
# =====================================================================

class MaintenanceWindowService:
    """Evaluates promotion eligibility against configured timezone-aware maintenance windows."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        schedule: Optional[MaintenanceSchedule] = None,
    ) -> None:
        self.settings = settings or default_settings
        self._schedule = schedule or self._build_default_schedule()

    def _build_default_schedule(self) -> MaintenanceSchedule:
        """Constructs default schedule from centralized platform settings."""
        try:
            tz_str = getattr(self.settings, "MAINTENANCE_WINDOW_TIMEZONE", "UTC")
            enabled = getattr(self.settings, "MAINTENANCE_WINDOW_ENABLED", True)
            weekdays = getattr(self.settings, "MAINTENANCE_WINDOW_ALLOWED_WEEKDAYS", [6])

            start_str = getattr(self.settings, "MAINTENANCE_WINDOW_START_TIME", "02:00:00")
            end_str = getattr(self.settings, "MAINTENANCE_WINDOW_END_TIME", "04:00:00")

            t_start = time.fromisoformat(start_str)
            t_end = time.fromisoformat(end_str)

            blackout_strs = getattr(self.settings, "MAINTENANCE_WINDOW_BLACKOUT_DATES", [])
            blackout_dates = [date.fromisoformat(d) for d in blackout_strs]

            return MaintenanceSchedule(
                enabled=enabled,
                timezone=tz_str,
                allowed_weekdays=weekdays,
                time_ranges=[TimeRange(start_time=t_start, end_time=t_end)],
                blackout_dates=blackout_dates,
            )
        except Exception as exc:
            logger.error("Failed to parse default maintenance settings: %s. Using safe fail-closed fallback.", exc)
            return MaintenanceSchedule(
                enabled=True,
                timezone="UTC",
                allowed_weekdays=[6],
                time_ranges=[TimeRange(start_time=time(2, 0), end_time=time(4, 0))],
                blackout_dates=[],
            )

    @property
    def schedule(self) -> MaintenanceSchedule:
        return self._schedule

    def update_schedule(self, new_schedule: MaintenanceSchedule) -> None:
        """Dynamically reconfigures the maintenance schedule."""
        self._schedule = new_schedule
        logger.info(
            "Updated maintenance schedule (timezone: %s, enabled: %s, ranges: %d).",
            new_schedule.timezone,
            new_schedule.enabled,
            len(new_schedule.time_ranges),
        )

    def compute_next_window(
        self,
        from_utc: datetime,
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Calculates the start and end of the next permitted maintenance window (in UTC).

        Searches forward day-by-day up to 366 days, skipping blackout dates and non-permitted weekdays.
        """
        tz = ZoneInfo(self._schedule.timezone)
        from_local = from_utc.astimezone(tz)
        local_date = from_local.date()

        for day_offset in range(367):
            candidate_date = local_date + timedelta(days=day_offset)

            # Skip blackout dates
            if candidate_date in self._schedule.blackout_dates:
                continue

            # Skip disallowed weekdays
            if candidate_date.weekday() not in self._schedule.allowed_weekdays:
                continue

            # Check daily time ranges
            for tr in self._schedule.time_ranges:
                cand_start_local = datetime.combine(candidate_date, tr.start_time, tzinfo=tz)
                cand_end_local = datetime.combine(candidate_date, tr.end_time, tzinfo=tz)

                cand_start_utc = cand_start_local.astimezone(timezone.utc)
                cand_end_utc = cand_end_local.astimezone(timezone.utc)

                # Next window must start in the future
                if cand_start_utc > from_utc:
                    return cand_start_utc, cand_end_utc

        return None, None

    def check_eligibility(
        self,
        evaluation_time: Optional[datetime] = None,
        admin_override: bool = False,
    ) -> MaintenanceWindowEligibility:
        """Determines whether index promotion is currently permitted.

        Args:
            evaluation_time: Explicit timezone-aware timestamp (for deterministic testing).
                             Rejects naive datetimes. If None, uses datetime.now(timezone.utc).
            admin_override: If True, grants emergency exemption permitting promotion.

        Returns:
            MaintenanceWindowEligibility structured report.
        """
        # 1. Enforce timezone-aware timestamp requirement
        if evaluation_time is not None:
            if evaluation_time.tzinfo is None or evaluation_time.tzinfo.utcoffset(evaluation_time) is None:
                raise ValueError(
                    f"Evaluation timestamp must be timezone-aware (tzinfo required). Got naive: {evaluation_time}."
                )
            eval_utc = evaluation_time.astimezone(timezone.utc)
        else:
            eval_utc = datetime.now(timezone.utc)

        # 2. Emergency Admin Override
        if admin_override:
            logger.warning("Maintenance window bypassed via admin override at %s UTC.", eval_utc.isoformat())
            return MaintenanceWindowEligibility(
                is_permitted=True,
                reason="Promotion permitted via emergency administrative override.",
                evaluated_at=eval_utc,
                evaluated_local_time=eval_utc.isoformat(),
                active_schedule_timezone=self._schedule.timezone,
                admin_override_used=True,
            )

        # 3. Disabled Switch
        if not self._schedule.enabled:
            return MaintenanceWindowEligibility(
                is_permitted=True,
                reason="Maintenance window enforcement is disabled by configuration.",
                evaluated_at=eval_utc,
                evaluated_local_time=eval_utc.isoformat(),
                active_schedule_timezone=self._schedule.timezone,
                admin_override_used=False,
            )

        # 4. Convert UTC to configured IANA schedule timezone
        try:
            tz = ZoneInfo(self._schedule.timezone)
        except Exception as exc:
            # Fail closed if timezone is invalid
            logger.error("Failed to load timezone '%s': %s. Failing closed.", self._schedule.timezone, exc)
            return MaintenanceWindowEligibility(
                is_permitted=False,
                reason=f"Schedule configuration error: invalid timezone '{self._schedule.timezone}' (fail-closed).",
                evaluated_at=eval_utc,
                evaluated_local_time=eval_utc.isoformat(),
                active_schedule_timezone=self._schedule.timezone,
            )

        local_dt = eval_utc.astimezone(tz)
        local_date = local_dt.date()
        local_time = local_dt.time()
        local_time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")

        # 5. Blackout Date Check
        if local_date in self._schedule.blackout_dates:
            next_start, next_end = self.compute_next_window(eval_utc)
            return MaintenanceWindowEligibility(
                is_permitted=False,
                reason=f"Promotion blocked: {local_date} is a designated blackout date in {self._schedule.timezone}.",
                evaluated_at=eval_utc,
                evaluated_local_time=local_time_str,
                active_schedule_timezone=self._schedule.timezone,
                next_window_start=next_start,
                next_window_end=next_end,
            )

        # 6. Permitted Weekdays Check
        current_weekday = local_dt.weekday()
        if current_weekday not in self._schedule.allowed_weekdays:
            weekday_name = local_dt.strftime("%A")
            next_start, next_end = self.compute_next_window(eval_utc)
            return MaintenanceWindowEligibility(
                is_permitted=False,
                reason=(
                    f"Promotion blocked: current day {weekday_name} is outside permitted weekdays "
                    f"{self._schedule.allowed_weekdays} in {self._schedule.timezone}."
                ),
                evaluated_at=eval_utc,
                evaluated_local_time=local_time_str,
                active_schedule_timezone=self._schedule.timezone,
                next_window_start=next_start,
                next_window_end=next_end,
            )

        # 7. Permitted Time Range Check
        # Range is evaluated as [start_time, end_time) (inclusive start, exclusive end)
        for tr in self._schedule.time_ranges:
            if tr.start_time <= local_time < tr.end_time:
                # Inside active maintenance window
                window_end_local = datetime.combine(local_date, tr.end_time, tzinfo=tz)
                window_end_utc = window_end_local.astimezone(timezone.utc)
                return MaintenanceWindowEligibility(
                    is_permitted=True,
                    reason=f"Promotion permitted: inside maintenance window [{tr.start_time}-{tr.end_time} {self._schedule.timezone}].",
                    evaluated_at=eval_utc,
                    evaluated_local_time=local_time_str,
                    active_schedule_timezone=self._schedule.timezone,
                    current_window_end=window_end_utc,
                )

        # Outside all permitted time ranges
        next_start, next_end = self.compute_next_window(eval_utc)
        return MaintenanceWindowEligibility(
            is_permitted=False,
            reason=(
                f"Promotion blocked: current time {local_time.strftime('%H:%M:%S')} is outside permitted time ranges "
                f"in {self._schedule.timezone}."
            ),
            evaluated_at=eval_utc,
            evaluated_local_time=local_time_str,
            active_schedule_timezone=self._schedule.timezone,
            next_window_start=next_start,
            next_window_end=next_end,
        )

    def enforce_promotion_eligibility(
        self,
        evaluation_time: Optional[datetime] = None,
        admin_override: bool = False,
    ) -> None:
        """Enforces maintenance window eligibility, raising MaintenanceWindowError if blocked."""
        eligibility = self.check_eligibility(evaluation_time=evaluation_time, admin_override=admin_override)
        if not eligibility.is_permitted:
            raise MaintenanceWindowError(
                message=eligibility.reason,
                detail={
                    "evaluated_at": eligibility.evaluated_at.isoformat(),
                    "evaluated_local_time": eligibility.evaluated_local_time,
                    "timezone": eligibility.active_schedule_timezone,
                    "next_window_start": eligibility.next_window_start.isoformat() if eligibility.next_window_start else None,
                    "next_window_end": eligibility.next_window_end.isoformat() if eligibility.next_window_end else None,
                },
            )

