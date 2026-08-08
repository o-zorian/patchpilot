from __future__ import annotations

from pathlib import Path
from typing import Any


def clamp_page(page: int) -> int:
    return page


def first_or_none(values: list[str]) -> str | None:
    return values[0]


def paginate(values: list[int], page: int, size: int) -> list[int]:
    start = page * size
    return values[start : start + size]


def sort_names(values: list[str]) -> list[str]:
    return sorted(values)


def parse_enabled(payload: dict[str, Any]) -> bool:
    return bool(payload.get("enabled"))


def cache_key(tenant: str, key: str) -> str:
    return key


def build_user_query(active_only: bool) -> tuple[str, tuple[object, ...]]:
    return "SELECT id FROM users", ()


def safe_join(root: Path, name: str) -> Path:
    return root / name


def normalize_extension(extension: str) -> str:
    return extension.removeprefix(".")


def retry_delay(attempt: int, maximum: int) -> int:
    return 2**attempt


def merge_headers(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    return {**base, **extra}


def safe_divide(numerator: float, denominator: float) -> float | None:
    return numerator / denominator
