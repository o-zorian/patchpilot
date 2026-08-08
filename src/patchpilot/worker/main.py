from __future__ import annotations

import asyncio
import logging
import socket
from uuid import uuid4

from patchpilot.config import AppSettings
from patchpilot.domain.cancellation import CancellationToken
from patchpilot.logging import configure_logging
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.repositories import RunRepository
from patchpilot.queue import RedisRunQueue, RunQueue
from patchpilot.services import RunExecutor

logger = logging.getLogger(__name__)


class RunWorker:
    """Claims queued Runs atomically and executes them through the shared RunExecutor."""

    def __init__(
        self,
        database: Database,
        queue: RunQueue,
        executor: RunExecutor,
        *,
        worker_id: str | None = None,
        poll_seconds: float = 1,
    ) -> None:
        self.database = database
        self.queue = queue
        self.executor = executor
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:12]}"
        self.poll_seconds = poll_seconds

    async def run_once(self) -> bool:
        run_id = await self.queue.dequeue(timeout_seconds=self.poll_seconds)
        if run_id is None:
            return False
        async with self.database.session() as session:
            claimed = await RunRepository(session).claim(run_id, worker_id=self.worker_id)
        if claimed is None:
            logger.info(
                "queued run was already claimed or completed", extra={"run_id": str(run_id)}
            )
            return True
        token = CancellationToken()
        await self.executor.execute(
            run_id,
            worker_id=self.worker_id,
            cancellation_token=token,
            already_claimed=True,
        )
        return True

    async def serve(self) -> None:
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker failed to process queued run")


async def _serve(settings: AppSettings) -> None:
    settings = settings.model_copy(update={"database_url": settings.postgres_database_url})
    settings.ensure_runtime_directories()
    await asyncio.to_thread(upgrade_database, settings.database_url)
    database = Database(settings.database_url)
    queue = RedisRunQueue(settings.redis_url, settings.redis_queue_name)
    executor = RunExecutor(database, settings)
    worker = RunWorker(
        database,
        queue,
        executor,
        poll_seconds=settings.worker_poll_seconds,
    )
    try:
        await worker.serve()
    finally:
        await queue.close()
        await database.close()


def main() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        logger.info("worker stopped")
