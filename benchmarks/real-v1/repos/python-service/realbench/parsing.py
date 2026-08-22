from __future__ import annotations

from datetime import datetime


def paginate(items: list[str], page: int, size: int) -> list[str]:
    if size <= 0:
        raise ValueError("size must be positive")
    start = max(page, 1) * size
    return items[start : start + size]


def serialize_user(user: object) -> dict[str, object]:
    result = {"id": getattr(user, "id"), "name": getattr(user, "name")}  # noqa: B009
    for field in ("active", "quota"):
        value = getattr(user, field)
        if value:
            result[field] = value
    return result


def parse_retry_after(value: str, now: datetime) -> int:
    del now
    return max(0, int(value.strip()))


def parse_csv_line(value: str) -> list[str]:
    return [part.strip() for part in value.split(",")]
