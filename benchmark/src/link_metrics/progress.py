"""Bounded execution shared by resumable benchmark workflows."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ExecutionBudget:
    """Permit new atomic work until a unit or wall-clock budget is exhausted."""

    maximum_units: int | None = None
    deadline: float | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    completed_units: int = 0

    @classmethod
    def for_hours(cls, hours: float) -> ExecutionBudget:
        if hours < 0:
            raise ValueError("time budget must not be negative")
        clock = time.monotonic
        return cls(deadline=clock() + hours * 60 * 60, clock=clock)

    def can_start(self) -> bool:
        units_available = (
            self.maximum_units is None or self.completed_units < self.maximum_units
        )
        time_available = self.deadline is None or self.clock() < self.deadline
        return units_available and time_available

    def record_completed(self) -> None:
        self.completed_units += 1
