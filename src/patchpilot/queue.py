from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis


class RunQueue(Protocol):
    async def enqueue(self, run_id: UUID) -> None: ...

    async def dequeue(self, *, timeout_seconds: float) -> UUID | None: ...

    async def publish_cancel(self, run_id: UUID) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class RedisRunQueue:
    def __init__(self, url: str, queue_name: str) -> None:
        self.redis = Redis.from_url(url, decode_responses=True)
        self.queue_name = queue_name
        self.cancel_channel = f"{queue_name}:cancel"

    async def enqueue(self, run_id: UUID) -> None:
        await self.redis.lpush(self.queue_name, str(run_id))

    async def dequeue(self, *, timeout_seconds: float) -> UUID | None:
        timeout = max(1, round(timeout_seconds))
        item = await self.redis.brpop(self.queue_name, timeout=timeout)
        if item is None:
            return None
        value = item[1]
        return UUID(value.decode() if isinstance(value, bytes) else value)

    async def publish_cancel(self, run_id: UUID) -> None:
        await self.redis.publish(self.cancel_channel, str(run_id))

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()


class InMemoryRunQueue:
    """Deterministic queue used by local foreground mode and automated tests."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._cancelled: asyncio.Queue[UUID] = asyncio.Queue()
        self.closed = False

    async def enqueue(self, run_id: UUID) -> None:
        if self.closed:
            raise RuntimeError("queue is closed")
        await self._queue.put(run_id)

    async def dequeue(self, *, timeout_seconds: float) -> UUID | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None

    async def publish_cancel(self, run_id: UUID) -> None:
        await self._cancelled.put(run_id)

    async def cancellations(self) -> AsyncIterator[UUID]:
        while not self.closed:
            yield await self._cancelled.get()

    async def ping(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True
