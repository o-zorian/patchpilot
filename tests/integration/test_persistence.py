from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from patchpilot.domain.run import RunStatus, RunStrategy
from patchpilot.domain.task import TaskSpec
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.models import RunRow, TaskRow
from patchpilot.persistence.repositories import RunRepository


@pytest.mark.asyncio
async def test_migration_and_persistent_run_creation(
    tmp_path: Path,
    valid_task_data: dict[str, object],
) -> None:
    database_path = tmp_path / "patchpilot.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    spec = TaskSpec.model_validate(valid_task_data)
    database = Database(database_url)

    try:
        async with database.session() as session:
            created = await RunRepository(session).create(
                spec,
                strategy=RunStrategy.FULL,
                model="scripted-test",
            )
        async with database.session() as session:
            persisted = await RunRepository(session).get(created.id)
            task_count = await session.scalar(select(func.count()).select_from(TaskRow))
            run_count = await session.scalar(select(func.count()).select_from(RunRow))
    finally:
        await database.close()

    assert persisted == created
    assert persisted.status == RunStatus.PENDING
    assert task_count == 1
    assert run_count == 1
