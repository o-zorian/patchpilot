from __future__ import annotations

from datetime import datetime


def seconds_until(target: datetime, now: datetime) -> int:
    return int((target - now).total_seconds())
