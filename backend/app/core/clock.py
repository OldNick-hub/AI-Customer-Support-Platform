"""Deterministic Clock Abstraction.

Provides a unified time source for scheduling, SLA computation, after-hours
determination, escalation timeouts, and reliable testing without sleep().
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional, Union


class Clock(ABC):
    """Abstract base class for time providers."""

    @abstractmethod
    def now(self) -> datetime:
        """Returns the current timezone-aware UTC datetime."""
        raise NotImplementedError


class SystemClock(Clock):
    """Standard system clock providing real wall-clock time."""

    def now(self) -> datetime:
        """Returns the current real-world UTC datetime."""
        return datetime.now(timezone.utc)


class SettableClock(Clock):
    """Deterministic, simulated clock for reproducible testing and simulation."""

    def __init__(self, initial_time: Optional[datetime] = None) -> None:
        if initial_time is None:
            self._current_time = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
        elif initial_time.tzinfo is None:
            self._current_time = initial_time.replace(tzinfo=timezone.utc)
        else:
            self._current_time = initial_time.astimezone(timezone.utc)

    def now(self) -> datetime:
        """Returns the currently set simulated UTC datetime."""
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        """Explicitly sets the simulated time."""
        if new_time.tzinfo is None:
            self._current_time = new_time.replace(tzinfo=timezone.utc)
        else:
            self._current_time = new_time.astimezone(timezone.utc)

    def advance(self, delta: Union[timedelta, int, float]) -> datetime:
        """Advances the clock forward by timedelta or seconds and returns the new time."""
        if isinstance(delta, (int, float)):
            delta = timedelta(seconds=delta)
        self._current_time += delta
        return self._current_time


# Default system clock singleton
system_clock = SystemClock()

