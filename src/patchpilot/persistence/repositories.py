from __future__ import annotations

from datetime import UTC
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.domain.run import Run, RunStrategy
from patchpilot.domain.task import TaskSpec
from patchpilot.persistence.models import RunRow, TaskRow


class RunNotFoundError(LookupError):
    """Raised when a persisted Run ID does not exist."""


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
    ) -> Run:
        task_id = uuid4()
        run = Run(
            task_id=task_id,
            strategy=strategy,
            model=model,
            prompt_version=prompt_version,
            idempotency_key=idempotency_key,
        )
        task_row = TaskRow(
            id=str(task_id),
            external_id=spec.id,
            title=spec.title,
            task_spec=spec.model_dump(mode="json"),
            spec_version=spec.version,
            created_at=run.created_at,
        )
        run_row = self._to_row(run)
        self.session.add_all([task_row, run_row])
        await self.session.commit()
        return run

    async def get(self, run_id: UUID) -> Run:
        row = await self.session.scalar(select(RunRow).where(RunRow.id == str(run_id)))
        if row is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return self._from_row(row)

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
            created_at=run.created_at,
        )

    @staticmethod
    def _from_row(row: RunRow) -> Run:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        started_at = row.started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        finished_at = row.finished_at
        if finished_at is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        return Run(
            id=UUID(row.id),
            task_id=UUID(row.task_id),
            status=row.status,
            strategy=row.strategy,
            model=row.model,
            prompt_version=row.prompt_version,
            idempotency_key=row.idempotency_key,
            workspace_id=row.workspace_id,
            step_count=row.step_count,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            estimated_cost_usd=row.estimated_cost_usd,
            result_code=row.result_code,
            error_code=row.error_code,
            started_at=started_at,
            finished_at=finished_at,
            created_at=created_at,
        )
