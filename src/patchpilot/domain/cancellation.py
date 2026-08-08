from __future__ import annotations

import asyncio
import threading


class CancellationToken:
    """Thread-safe cooperative cancellation shared by Worker, Agent, and tools."""

    def __init__(self) -> None:
        self.event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self) -> None:
        self.event.set()

    async def wait(self, *, poll_seconds: float = 0.05) -> None:
        while not self.event.is_set():
            await asyncio.sleep(poll_seconds)
