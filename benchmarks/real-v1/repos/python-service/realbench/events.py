from __future__ import annotations

from collections.abc import Callable

from realbench.models import EventState


class EventProcessor:
    def __init__(self, state: EventState) -> None:
        self.state = state
        self._seen: set[str] = set()

    def apply(self, event_id: str, effect: Callable[[], None]) -> bool:
        if event_id in self._seen:
            return False
        effect()
        self._seen.add(event_id)
        self.state.applied.append(event_id)
        return True
