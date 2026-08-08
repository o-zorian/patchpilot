from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.domain.run import Run, RunActor, RunStatus, RunStrategy
from patchpilot.domain.scorecard import QualityResult, ScorecardMetrics
from patchpilot.domain.task import TaskRecord, TaskSpec
from patchpilot.persistence.models import ArtifactRow, EventRow, RunRow, TaskRow


class TaskNotFoundError(LookupError):
    """Raised when a persisted Task ID does not exist."""


class RunNotFoundError(LookupError):
    """Raised when a persisted Run ID does not exist."""


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, spec: TaskSpec, *, owner_id: str = "local") -> TaskRecord:
        task_id = uuid4()
        created_at = datetime.now(UTC)
        row = TaskRow(
            id=str(task_id),
            external_id=spec.id,
            owner_id=owner_id,
            title=spec.title,
            task_spec=spec.model_dump(mode="json"),
            spec_version=spec.version,
            created_at=created_at,
        )
        self.session.add(row)
        await self.session.commit()
        return TaskRecord(id=task_id, owner_id=owner_id, spec=spec, created_at=created_at)

    async def get(self, task_id: UUID) -> TaskRecord:
        row = await self.session.scalar(select(TaskRow).where(TaskRow.id == str(task_id)))
        if row is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        return self._from_row(row)

    async def list(self, *, owner_id: str, offset: int, limit: int) -> list[TaskRecord]:
        rows = await self.session.scalars(
            select(TaskRow)
            .where(TaskRow.owner_id == owner_id)
            .order_by(TaskRow.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: TaskRow) -> TaskRecord:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return TaskRecord(
            id=UUID(row.id),
            owner_id=row.owner_id,
            spec=TaskSpec.model_validate(row.task_spec),
            created_at=created_at,
        )


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        spec: TaskSpec,
        *,
        strategy: RunStrategy,
        model: str,
        prompt_version: str = "1",
        idempotency_key: str | None = None,
        owner_id: str = "local",
    ) -> Run:
        task = await TaskRepository(self.session).create(spec, owner_id=owner_id)
        run, _ = await self.create_for_task(
            task.id,
            strategy=strategy,
            model=model,
            prompt_version=prompt_version,
            idempotency_key=idempotency_key,
        )
        return run

    async def create_for_task(
        self,
        task_id: UUID,
        *,
        strategy: RunStrategy,
        model: str,
        prompt_version: str = "m2-v1",
        idempotency_key: str | None = None,
    ) -> tuple[Run, bool]:
        if idempotency_key is not None:
            existing = await self._find_idempotent(task_id, strategy, model, idempotency_key)
            if existing is not None:
                return existing, False
        run = Run(
            task_id=task_id,
            strategy=strategy,
            model=model,
            prompt_version=prompt_version,
            idempotency_key=idempotency_key,
        )
        self.session.add(self._to_row(run))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            if idempotency_key is None:
                raise
            existing = await self._find_idempotent(task_id, strategy, model, idempotency_key)
            if existing is None:
                raise
            return existing, False
        return run, True

    async def _find_idempotent(
        self,
        task_id: UUID,
        strategy: RunStrategy,
        model: str,
        idempotency_key: str,
    ) -> Run | None:
        row = await self.session.scalar(
            select(RunRow).where(
                RunRow.task_id == str(task_id),
                RunRow.strategy == strategy.value,
                RunRow.model == model,
                RunRow.idempotency_key == idempotency_key,
            )
        )
        return None if row is None else self._from_row(row)

    async def get(self, run_id: UUID) -> Run:
        row = await self.session.scalar(select(RunRow).where(RunRow.id == str(run_id)))
        if row is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return self._from_row(row)

    async def list(
        self,
        *,
        owner_id: str,
        offset: int,
        limit: int,
    ) -> list[Run]:
        rows = await self.session.scalars(
            select(RunRow)
            .join(TaskRow, TaskRow.id == RunRow.task_id)
            .where(TaskRow.owner_id == owner_id)
            .order_by(RunRow.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [self._from_row(row) for row in rows]

    async def claim(self, run_id: UUID, *, worker_id: str) -> Run | None:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(RunRow)
            .where(RunRow.id == str(run_id), RunRow.status == RunStatus.PENDING.value)
            .values(
                status=RunStatus.PREPARING.value,
                worker_id=worker_id,
                claimed_at=now,
                heartbeat_at=now,
                started_at=now,
            )
            .returning(RunRow)
        )
        row = result.scalar_one_or_none()
        await self.session.commit()
        return None if row is None else self._from_row(row)

    async def mark_running(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        workspace_id: str,
        sandbox_mode: str,
        sandbox_image: str | None,
    ) -> Run:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(RunRow)
            .where(
                RunRow.id == str(run_id),
                RunRow.status == RunStatus.PREPARING.value,
                RunRow.worker_id == worker_id,
            )
            .values(
                status=RunStatus.RUNNING.value,
                workspace_id=workspace_id,
                sandbox_mode=sandbox_mode,
                sandbox_image=sandbox_image,
                heartbeat_at=now,
            )
            .returning(RunRow)
        )
        row = result.scalar_one_or_none()
        if row is None:
            await self.session.rollback()
            raise ValueError("Run is not claimed by this Worker")
        await self.session.commit()
        return self._from_row(row)

    async def heartbeat(self, run_id: UUID, *, worker_id: str) -> None:
        await self.session.execute(
            update(RunRow)
            .where(
                RunRow.id == str(run_id),
                RunRow.worker_id == worker_id,
                RunRow.status.in_([RunStatus.PREPARING.value, RunStatus.RUNNING.value]),
            )
            .values(heartbeat_at=datetime.now(UTC))
        )
        await self.session.commit()

    async def request_cancel(self, run_id: UUID) -> tuple[Run, bool]:
        row = await self.session.scalar(
            select(RunRow).where(RunRow.id == str(run_id)).with_for_update()
        )
        if row is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        run = self._from_row(row)
        if run.status.is_terminal or run.cancel_requested_at is not None:
            return run, False
        now = datetime.now(UTC)
        row.cancel_requested_at = now
        if run.status == RunStatus.PENDING:
            row.status = RunStatus.CANCELLED.value
            row.result_code = QualityResult.CANCELLED.value
            row.finished_at = now
        await self.session.commit()
        return self._from_row(row), True

    async def cancel_requested(self, run_id: UUID) -> bool:
        value = await self.session.scalar(
            select(RunRow.cancel_requested_at).where(RunRow.id == str(run_id))
        )
        return value is not None

    async def mark_system_error(self, run_id: UUID, *, error_code: str) -> Run:
        row = await self.session.scalar(select(RunRow).where(RunRow.id == str(run_id)))
        if row is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        if row.status not in {status.value for status in RunStatus if status.is_terminal}:
            row.status = RunStatus.SYSTEM_ERROR.value
            row.result_code = QualityResult.SYSTEM_ERROR.value
            row.error_code = error_code
            row.finished_at = datetime.now(UTC)
            await self.session.commit()
        return self._from_row(row)

    async def finalize(
        self,
        run_id: UUID,
        *,
        status: RunStatus,
        workspace_id: str,
        metrics: ScorecardMetrics,
        result_code: str,
        error_code: str | None = None,
    ) -> Run:
        if not status.is_terminal:
            raise ValueError("persisted Run finalization requires a terminal status")
        row = await self.session.scalar(select(RunRow).where(RunRow.id == str(run_id)))
        if row is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        run = self._from_row(row)
        if run.status.is_terminal:
            if run.status != status or run.result_code != result_code:
                raise ValueError("cannot replace a persisted Run terminal outcome")
            return run
        if run.status in {RunStatus.PENDING, RunStatus.PREPARING}:
            run = run.transition(RunStatus.RUNNING, actor=RunActor.WORKER)
        run = run.transition(status, actor=RunActor.SYSTEM)
        run = run.model_copy(
            update={
                "workspace_id": workspace_id,
                "step_count": metrics.steps,
                "prompt_tokens": metrics.prompt_tokens,
                "completion_tokens": metrics.completion_tokens,
                "estimated_cost_usd": metrics.estimated_cost_usd,
                "result_code": result_code,
                "error_code": error_code,
            }
        )
        self._copy_run_to_row(run, row)
        await self.session.commit()
        return run

    @staticmethod
    def _copy_run_to_row(run: Run, row: RunRow) -> None:
        row.status = run.status.value
        row.workspace_id = run.workspace_id
        row.step_count = run.step_count
        row.prompt_tokens = run.prompt_tokens
        row.completion_tokens = run.completion_tokens
        row.estimated_cost_usd = run.estimated_cost_usd
        row.result_code = run.result_code
        row.error_code = run.error_code
        row.started_at = run.started_at
        row.finished_at = run.finished_at

    @staticmethod
    def _to_row(run: Run) -> RunRow:
        return RunRow(
            id=str(run.id),
            task_id=str(run.task_id),
            status=run.status.value,
            strategy=run.strategy.value,
            model=run.model,
            prompt_version=run.prompt_version,
            idempotency_key=run.idempotency_key,
            workspace_id=run.workspace_id,
            step_count=run.step_count,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            estimated_cost_usd=run.estimated_cost_usd,
            result_code=run.result_code,
            error_code=run.error_code,
            started_at=run.started_at,
            finished_at=run.finished_at,
            cancel_requested_at=run.cancel_requested_at,
            claimed_at=run.claimed_at,
            heartbeat_at=run.heartbeat_at,
            worker_id=run.worker_id,
            sandbox_mode=run.sandbox_mode,
            sandbox_image=run.sandbox_image,
            created_at=run.created_at,
        )

    @staticmethod
    def _from_row(row: RunRow) -> Run:
        values: dict[str, Any] = {
            "id": UUID(row.id),
            "task_id": UUID(row.task_id),
            "status": row.status,
            "strategy": row.strategy,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "idempotency_key": row.idempotency_key,
            "workspace_id": row.workspace_id,
            "step_count": row.step_count,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "estimated_cost_usd": row.estimated_cost_usd,
            "result_code": row.result_code,
            "error_code": row.error_code,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "cancel_requested_at": row.cancel_requested_at,
            "claimed_at": row.claimed_at,
            "heartbeat_at": row.heartbeat_at,
            "worker_id": row.worker_id,
            "sandbox_mode": row.sandbox_mode,
            "sandbox_image": row.sandbox_image,
            "created_at": row.created_at,
        }
        for name in (
            "created_at",
            "started_at",
            "finished_at",
            "cancel_requested_at",
            "claimed_at",
            "heartbeat_at",
        ):
            value = values[name]
            if value is not None and value.tzinfo is None:
                values[name] = value.replace(tzinfo=UTC)
        return Run.model_validate(values)


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_after(self, run_id: UUID, *, after: int, limit: int) -> list[EventRow]:
        rows = await self.session.scalars(
            select(EventRow)
            .where(EventRow.run_id == str(run_id), EventRow.sequence > after)
            .order_by(EventRow.sequence)
            .limit(limit)
        )
        return list(rows)

    async def append(
        self,
        run_id: UUID,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> EventRow:
        maximum = await self.session.scalar(
            select(func.max(EventRow.sequence)).where(EventRow.run_id == str(run_id))
        )
        row = EventRow(
            id=str(uuid4()),
            run_id=str(run_id),
            sequence=(maximum or 0) + 1,
            event_type=event_type,
            payload=payload,
            duration_ms=None,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        await self.session.commit()
        return row


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, run_id: UUID, kind: str) -> ArtifactRow | None:
        row: ArtifactRow | None = await self.session.scalar(
            select(ArtifactRow).where(ArtifactRow.run_id == str(run_id), ArtifactRow.kind == kind)
        )
        return row
