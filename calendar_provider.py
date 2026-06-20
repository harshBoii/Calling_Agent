"""Calendar slot provider abstraction (stub for v1)."""

from __future__ import annotations

import datetime as dt
from typing import Protocol


class CalendarProvider(Protocol):
    async def get_available_slots(self, source_id: str, count: int) -> list[str]: ...


class StubCalendarProvider:
    """Returns human-readable placeholder slots relative to now."""

    async def get_available_slots(self, source_id: str, count: int) -> list[str]:
        now = dt.datetime.now(dt.timezone.utc)
        slots: list[str] = []
        for i, day_offset in enumerate((1, 2, 3, 4), start=1):
            if len(slots) >= count:
                break
            day = now + dt.timedelta(days=day_offset)
            if day.weekday() >= 5:
                continue
            hour = 10 + (i % 3) * 2
            slot_dt = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            slots.append(slot_dt.strftime("%A %d %b at %I:%M %p UTC"))
        while len(slots) < count:
            idx = len(slots) + 1
            slot_dt = now + dt.timedelta(days=idx, hours=10)
            slots.append(slot_dt.strftime("%A %d %b at %I:%M %p UTC"))
        return slots[:count]


default_calendar_provider: CalendarProvider = StubCalendarProvider()
