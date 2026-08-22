from __future__ import annotations

from collections.abc import Callable


class PermissionCache:
    def __init__(self) -> None:
        self._values: dict[str, bool] = {}

    def authorize(
        self,
        user_id: str,
        project_id: str,
        loader: Callable[[str, str], bool],
    ) -> bool:
        if user_id not in self._values:
            self._values[user_id] = loader(user_id, project_id)
        return self._values[user_id]
