"""Operational Business Hours and After-Hours Calendar Engine.

Evaluates operational business hours, after-hours detection, weekend handling,
holiday exclusion, and deterministic rolling forward to the next eligible business time.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.models.ticket import SLACalendar

logger = configure_safe_logger(logging.getLogger(__name__))


@dataclass(frozen=True)
class AfterHoursEvaluation:
    """Detailed result of an after-hours status evaluation."""

    is_after_hours: bool
    reason: str  # "WITHIN_BUSINESS_HOURS", "BEFORE_WORK_HOURS", "AFTER_WORK_HOURS", "WEEKEND", "HOLIDAY"
    calendar_timezone: str
    local_time: datetime
    next_eligible_business_time: datetime  # Always UTC


def _parse_time(val: Union[time, str]) -> time:
    if isinstance(val, time):
        return val
    parts = str(val).strip().split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def _get_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Unrecognized timezone '%s', defaulting to UTC.", tz_name)
        return ZoneInfo("UTC")


def evaluate_after_hours(
    dt: datetime,
    calendar: Optional[Union[SLACalendar, Dict[str, Any]]] = None,
) -> AfterHoursEvaluation:
    """Evaluates whether the given timestamp falls outside business hours.

    Deterministic rules:
    1. Converts `dt` to the calendar's configured timezone.
    2. Checks if the day of week is in `work_days` (1=Monday ... 7=Sunday).
    3. Checks if the date matches any entry in `holidays` (ISO 'YYYY-MM-DD').
    4. Checks if the local time is before `work_start_time` or >= `work_end_time`.
    5. Calculates the exact next eligible business time in UTC.
    """
    # 1. Resolve configuration
    if calendar is not None and isinstance(calendar, SLACalendar):
        tz_name = calendar.timezone or settings.BUSINESS_HOURS_TIMEZONE
        start_time = calendar.work_start_time
        end_time = calendar.work_end_time
        work_days = set(calendar.work_days or [1, 2, 3, 4, 5])
        holidays_raw = calendar.holidays or []
    elif calendar is not None and isinstance(calendar, dict):
        tz_name = calendar.get("timezone", settings.BUSINESS_HOURS_TIMEZONE)
        start_time = _parse_time(calendar.get("work_start_time", settings.BUSINESS_HOUR_START))
        end_time = _parse_time(calendar.get("work_end_time", settings.BUSINESS_HOUR_END))
        work_days = set(calendar.get("work_days", [1, 2, 3, 4, 5]))
        holidays_raw = calendar.get("holidays", [])
    else:
        tz_name = settings.BUSINESS_HOURS_TIMEZONE
        start_time = _parse_time(settings.BUSINESS_HOUR_START)
        end_time = _parse_time(settings.BUSINESS_HOUR_END)
        work_days = {1, 2, 3, 4, 5}
        holidays_raw = []

    # Normalize holiday strings: 'YYYY-MM-DD'
    holidays: set[str] = set()
    for h in holidays_raw:
        if isinstance(h, (date, datetime)):
            holidays.add(h.strftime("%Y-%m-%d"))
        elif isinstance(h, str):
            holidays.add(h.strip())

    # 2. Localize timestamp
    target_tz = _get_tz(tz_name)
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=timezone.utc)
    else:
        utc_dt = dt.astimezone(timezone.utc)
    local_dt = utc_dt.astimezone(target_tz)

    current_date_str = local_dt.strftime("%Y-%m-%d")
    iso_weekday = local_dt.isoweekday()  # 1=Monday, 7=Sunday
    current_time = local_dt.time()

    # 3. Determine status
    is_after = False
    reason = "WITHIN_BUSINESS_HOURS"

    if current_date_str in holidays:
        is_after = True
        reason = "HOLIDAY"
    elif iso_weekday not in work_days:
        is_after = True
        reason = "WEEKEND"
    elif current_time < start_time:
        is_after = True
        reason = "BEFORE_WORK_HOURS"
    elif current_time >= end_time:
        is_after = True
        reason = "AFTER_WORK_HOURS"

    # 4. Calculate next eligible business time
    if not is_after:
        next_business_utc = utc_dt
    else:
        next_business_utc = _calculate_next_business_time_internal(
            local_dt=local_dt,
            target_tz=target_tz,
            start_time=start_time,
            end_time=end_time,
            work_days=work_days,
            holidays=holidays,
        )

    return AfterHoursEvaluation(
        is_after_hours=is_after,
        reason=reason,
        calendar_timezone=tz_name,
        local_time=local_dt,
        next_eligible_business_time=next_business_utc,
    )


def is_after_hours(
    dt: datetime,
    calendar: Optional[Union[SLACalendar, Dict[str, Any]]] = None,
) -> bool:
    """Convenience boolean check for after-hours."""
    return evaluate_after_hours(dt, calendar).is_after_hours


def calculate_next_business_time(
    dt: datetime,
    calendar: Optional[Union[SLACalendar, Dict[str, Any]]] = None,
) -> datetime:
    """Convenience calculation returning next eligible business time in UTC."""
    return evaluate_after_hours(dt, calendar).next_eligible_business_time


def _calculate_next_business_time_internal(
    local_dt: datetime,
    target_tz: ZoneInfo,
    start_time: time,
    end_time: time,
    work_days: set[int],
    holidays: set[str],
) -> datetime:
    """Rolls forward day by day until finding an eligible work day at start_time."""
    current_time = local_dt.time()
    current_date = local_dt.date()
    iso_weekday = local_dt.isoweekday()
    date_str = current_date.strftime("%Y-%m-%d")

    # If it is today and currently BEFORE work hours, and today is a valid work day & not holiday:
    if current_time < start_time and iso_weekday in work_days and date_str not in holidays:
        next_local = datetime.combine(current_date, start_time).replace(tzinfo=target_tz)
        return next_local.astimezone(timezone.utc)

    # Otherwise, start searching from tomorrow
    candidate_date = current_date + timedelta(days=1)
    # Search up to 30 days to avoid infinite loop on bad config
    for _ in range(30):
        c_weekday = candidate_date.isoweekday()
        c_str = candidate_date.strftime("%Y-%m-%d")
        if c_weekday in work_days and c_str not in holidays:
            next_local = datetime.combine(candidate_date, start_time).replace(tzinfo=target_tz)
            return next_local.astimezone(timezone.utc)
        candidate_date += timedelta(days=1)

    # Fallback to next calendar day at start_time
    fallback_local = datetime.combine(current_date + timedelta(days=1), start_time).replace(
        tzinfo=target_tz
    )
    return fallback_local.astimezone(timezone.utc)

