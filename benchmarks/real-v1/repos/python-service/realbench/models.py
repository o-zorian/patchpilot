from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class User:
    id: str
    name: str
    active: bool | None = None
    quota: int | None = None


@dataclass
class CacheEntry:
    value: Any


@dataclass(frozen=True)
class Job:
    id: str
    priority: int
    submitted_at: int


@dataclass(frozen=True)
class Dependency:
    name: str
    requires: tuple[str, ...] = ()
    optional: bool = False


@dataclass
class Account:
    id: str
    balance: int


@dataclass
class EventState:
    applied: list[str] = field(default_factory=list)
