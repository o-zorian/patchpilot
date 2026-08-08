from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from patchpilot.agent.events import DatabaseEventSink, EventEmitter, EventType, JsonlEventSink
from patchpilot.agent.loop import AgentLoop, AgentLoopResult
from patchpilot.agent.registry import build_default_registry
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.config import AppSettings
from patchpilot.domain.cancellation import CancellationToken
from patchpilot.domain.run import Run, RunStatus, RunStrategy
from patchpilot.domain.task import (
    TaskLimits,
    TaskRecord,
    TaskSpec,
    validate_task_spec_context,
)
from patchpilot.models.base import ModelClient, ModelConfig
from patchpilot.models.openai_compatible import OpenAICompatibleClient
from patchpilot.persistence.database import Database
from patchpilot.persistence.models import ArtifactRow, EventRow
from patchpilot.persistence.repositories import (
    ArtifactRepository,
    EventRepository,
    RunRepository,
    TaskRepository,
)
from patchpilot.quality.gate import QualityGate
from patchpilot.queue import RunQueue
from patchpilot.sandbox.workspace import Workspace, WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits

ModelClientFactory = Callable[[Run], ModelClient]
ModelConfigFactory = Callable[[Run], ModelConfig]


def task_limits(settings: AppSettings) -> TaskLimits:
    return TaskLimits(
        max_steps=settings.hard_max_steps,
        max_input_tokens=settings.hard_max_input_tokens,
        max_output_tokens=settings.hard_max_output_tokens,
        max_cost_usd=settings.hard_max_cost_usd,
        max_wall_time_seconds=settings.hard_max_wall_time_seconds,
        max_changed_files=settings.hard_max_changed_files,
        max_patch_lines=settings.hard_max_patch_lines,
        max_command_timeout_seconds=settings.hard_max_command_timeout_seconds,
        max_cpu_limit=settings.hard_max_cpu_limit,
        max_memory_limit_mb=settings.hard_max_memory_limit_mb,
    )


class RunService:
    """Shared Task/Run application service used by CLI, API, and Worker."""

    def __init__(self, database: Database, queue: RunQueue, settings: AppSettings) -> None:
        self.database = database
        self.queue = queue
        self.settings = settings

    async def create_task(
        self,
        spec: TaskSpec,
        *,
        owner_id: str,
        base_directory: Path | None = None,
    ) -> TaskRecord:
        normalized = validate_task_spec_context(
            spec,
            base_directory=base_directory or Path.cwd(),
            limits=task_limits(self.settings),
        )
        async with self.database.session() as session:
            return await TaskRepository(session).create(normalized, owner_id=owner_id)

    async def get_task(self, task_id: UUID, *, owner_id: str) -> TaskRecord:
        async with self.database.session() as session:
            task = await TaskRepository(session).get(task_id)
        self._require_owner(task, owner_id)
        return task

    async def list_tasks(self, *, owner_id: str, offset: int, limit: int) -> list[TaskRecord]:
        async with self.database.session() as session:
            return await TaskRepository(session).list(owner_id=owner_id, offset=offset, limit=limit)

    async def submit_run(
        self,
        task_id: UUID,
        *,
        owner_id: str,
        strategy: RunStrategy,
        model: str,
        idempotency_key: str | None,
        prompt_version: str = "m2-v1",
    ) -> tuple[Run, bool]:
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 255:
                raise ValueError("Idempotency-Key must contain 1 to 255 characters")
        task = await self.get_task(task_id, owner_id=owner_id)
        async with self.database.session() as session:
            run, created = await RunRepository(session).create_for_task(
                task.id,
                strategy=strategy,
                model=model,
                prompt_version=prompt_version,
                idempotency_key=idempotency_key,
            )
        if created:
            try:
                await self.queue.enqueue(run.id)
            except Exception:
                async with self.database.session() as session:
                    await RunRepository(session).mark_system_error(
                        run.id, error_code="QUEUE_UNAVAILABLE"
                    )
                raise
        return run, created

    async def get_run(self, run_id: UUID, *, owner_id: str) -> Run:
        async with self.database.session() as session:
            run = await RunRepository(session).get(run_id)
            task = await TaskRepository(session).get(run.task_id)
        self._require_owner(task, owner_id)
        return run

    async def list_runs(self, *, owner_id: str, offset: int, limit: int) -> list[Run]:
        async with self.database.session() as session:
            return await RunRepository(session).list(owner_id=owner_id, offset=offset, limit=limit)

    async def cancel_run(self, run_id: UUID, *, owner_id: str) -> Run:
        await self.get_run(run_id, owner_id=owner_id)
        async with self.database.session() as session:
            run, changed = await RunRepository(session).request_cancel(run_id)
            if changed and run.status == RunStatus.CANCELLED:
                await EventRepository(session).append(
                    run_id,
                    event_type=EventType.RUN_CANCELLED.value,
                    payload={
                        "status": RunStatus.CANCELLED.value,
                        "result_code": "CANCELLED",
                    },
                )
        if changed:
            await self.queue.publish_cancel(run_id)
        return run

    async def list_events(
        self,
        run_id: UUID,
        *,
        owner_id: str,
        after: int,
        limit: int,
    ) -> list[EventRow]:
        await self.get_run(run_id, owner_id=owner_id)
        async with self.database.session() as session:
            return await EventRepository(session).list_after(run_id, after=after, limit=limit)

    async def get_artifact(
        self, run_id: UUID, kind: ArtifactKind, *, owner_id: str
    ) -> tuple[ArtifactRow, Path]:
        await self.get_run(run_id, owner_id=owner_id)
        async with self.database.session() as session:
            row = await ArtifactRepository(session).get(run_id, kind.value)
        if row is None:
            raise FileNotFoundError(f"Artifact not found: {kind.value}")
        root = self.settings.artifact_root.expanduser().resolve()
        path = (root / row.path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError("Artifact path is invalid")
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != row.sha256 or len(data) != row.size_bytes:
            raise OSError("Artifact integrity check failed")
        return row, path

    @staticmethod
    def _require_owner(task: TaskRecord, owner_id: str) -> None:
        if task.owner_id != owner_id:
            raise PermissionError("resource does not belong to this owner")


class RunExecutor:
    """The one execution path shared by foreground CLI and asynchronous Workers."""

    def __init__(
        self,
        database: Database,
        settings: AppSettings,
        *,
        model_client_factory: ModelClientFactory | None = None,
        model_config_factory: ModelConfigFactory | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.model_client_factory = model_client_factory or (lambda _: OpenAICompatibleClient())
        self.model_config_factory = model_config_factory or self._real_model_config

    def _real_model_config(self, run: Run) -> ModelConfig:
        return self.settings.real_model_config().model_copy(update={"model": run.model})

    async def execute(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        cancellation_token: CancellationToken | None = None,
        already_claimed: bool = False,
    ) -> AgentLoopResult | None:
        token = cancellation_token or CancellationToken()
        async with self.database.session() as session:
            repository = RunRepository(session)
            run = await repository.get(run_id)
            if not already_claimed:
                claimed = await repository.claim(run_id, worker_id=worker_id)
                if claimed is None:
                    return None
                run = claimed
            task = await TaskRepository(session).get(run.task_id)

        workspace: Workspace | None = None
        client: ModelClient | None = None
        monitor_tasks: list[asyncio.Task[None]] = []
        try:
            workspace = await asyncio.to_thread(
                WorkspaceManager(self.settings.workspace_root).create,
                Path(task.spec.repository.path),
                workspace_id=str(run.id),
                base_ref=task.spec.repository.base_ref,
            )
            async with self.database.session() as session:
                run = await RunRepository(session).mark_running(
                    run.id, worker_id=worker_id, workspace_id=workspace.id
                )
            context = ToolContext.create(
                workspace,
                task.spec,
                ToolLimits(
                    output_max_chars=self.settings.tool_output_max_chars,
                    list_max_files=self.settings.tool_list_max_files,
                    search_max_results=self.settings.tool_search_max_results,
                    read_max_lines=self.settings.tool_read_max_lines,
                    max_file_bytes=self.settings.tool_max_file_bytes,
                ),
                cancellation_token=token,
            )
            artifacts = ArtifactStore(
                self.settings.artifact_root,
                run.id,
                database=self.database,
            )
            events = EventEmitter(
                run.id,
                [
                    JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG)),
                    DatabaseEventSink(self.database),
                ],
            )
            gate = QualityGate(
                run_id=run.id,
                context=context,
                events=events,
                artifacts=artifacts,
            )
            client = self.model_client_factory(run)
            monitor_tasks = [
                asyncio.create_task(self._monitor_cancel(run.id, token)),
                asyncio.create_task(self._heartbeat(run.id, worker_id, token)),
            ]
            loop = AgentLoop(
                model_client=client,
                model_config=self.model_config_factory(run),
                tool_context=context,
                registry=build_default_registry(context),
                events=events,
                quality_gate=gate,
                cancellation_token=token,
            )
            return await loop.run(run.id)
        except Exception:
            async with self.database.session() as session:
                await RunRepository(session).mark_system_error(
                    run_id, error_code="WORKER_EXECUTION_ERROR"
                )
            raise
        finally:
            token.cancel()
            for task_monitor in monitor_tasks:
                task_monitor.cancel()
            for task_monitor in monitor_tasks:
                try:
                    await task_monitor
                except asyncio.CancelledError:
                    pass
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close_result = close()
                    if inspect.isawaitable(close_result):
                        await cast(Any, close_result)
            if workspace is not None:
                await asyncio.to_thread(
                    WorkspaceManager(self.settings.workspace_root).cleanup,
                    workspace,
                )

    async def _monitor_cancel(self, run_id: UUID, token: CancellationToken) -> None:
        while not token.is_cancelled:
            async with self.database.session() as session:
                if await RunRepository(session).cancel_requested(run_id):
                    token.cancel()
                    return
            await asyncio.sleep(self.settings.worker_cancel_poll_seconds)

    async def _heartbeat(self, run_id: UUID, worker_id: str, token: CancellationToken) -> None:
        while not token.is_cancelled:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            async with self.database.session() as session:
                await RunRepository(session).heartbeat(run_id, worker_id=worker_id)
