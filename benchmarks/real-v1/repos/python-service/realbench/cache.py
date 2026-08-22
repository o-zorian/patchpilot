from __future__ import annotations

from realbench.models import CacheEntry


class UserCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, user_id: str) -> object | None:
        entry = self._entries.get(user_id)
        return None if entry is None else entry.value

    def put(self, user_id: str, value: object) -> None:
        self._entries[user_id] = CacheEntry(value)
