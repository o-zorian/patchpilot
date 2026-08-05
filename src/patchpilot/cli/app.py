from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import ValidationError

from patchpilot import __version__
from patchpilot.config import AppSettings, SettingsError
from patchpilot.domain.run import Run, RunStrategy
from patchpilot.domain.task import (
    LoadedTaskSpec,
    TaskLimits,
    TaskSpecLoadError,
    load_task_spec,
    task_spec_json_schema,
)
from patchpilot.logging import configure_logging
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.repositories import RunNotFoundError, RunRepository

app = typer.Typer(no_args_is_help=True, help="PatchPilot controlled coding-agent harness.")
task_app = typer.Typer(no_args_is_help=True, help="Validate and inspect TaskSpec documents.")
run_app = typer.Typer(no_args_is_help=True, help="Create and inspect persisted Run records.")
db_app = typer.Typer(no_args_is_help=True, help="Manage the M0 SQLite database.")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(db_app, name="db")
logger = logging.getLogger(__name__)


def _settings() -> AppSettings:
    settings = AppSettings()
    configure_logging(settings.log_level)
    return settings


def _limits(settings: AppSettings) -> TaskLimits:
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


def _load(path: Path, settings: AppSettings) -> LoadedTaskSpec:
    return load_task_spec(path, _limits(settings))


def _emit(payload: dict[str, Any], *, json_output: bool, human: str) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str) if json_output else human)


def _fail(message: str, *, json_output: bool, details: Any = None) -> None:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": "VALIDATION_ERROR", "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    _emit(payload, json_output=json_output, human=f"Error: {message}")
    raise typer.Exit(code=1)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the PatchPilot version and exit."),
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@task_app.command("validate")
def validate_task(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    settings = _settings()
    try:
        loaded = _load(path, settings)
    except ValidationError as exc:
        _fail("TaskSpec schema validation failed", json_output=json_output, details=exc.errors())
    except TaskSpecLoadError as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "task_id": loaded.spec.id,
        "version": loaded.spec.version,
        "repository": str(loaded.repository_path),
    }
    _emit(
        payload,
        json_output=json_output,
        human=f"Valid TaskSpec {loaded.spec.id} (version {loaded.spec.version})",
    )


@task_app.command("schema")
def write_schema(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", dir_okay=False, help="Write Schema to this file."),
    ] = None,
) -> None:
    schema_text = json.dumps(task_spec_json_schema(), ensure_ascii=False, indent=2) + "\n"
    if output is None:
        typer.echo(schema_text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(schema_text, encoding="utf-8")
    typer.echo(f"Wrote TaskSpec v1 JSON Schema to {output.resolve()}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    settings = _settings()
    try:
        settings.ensure_runtime_directories()
        upgrade_database(settings.database_url)
    except (SettingsError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=False)
    typer.echo(f"Database upgraded: {settings.sqlite_database_path()}")


async def _create_run(
    settings: AppSettings,
    loaded: LoadedTaskSpec,
    strategy: RunStrategy,
    model: str,
    prompt_version: str,
) -> Run:
    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            return await RunRepository(session).create(
                loaded.spec,
                strategy=strategy,
                model=model,
                prompt_version=prompt_version,
            )
    finally:
        await database.close()


@run_app.command("create")
def create_run(
    task_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    model: Annotated[str, typer.Option("--model", help="Model label recorded on this Run.")],
    strategy: Annotated[RunStrategy, typer.Option("--strategy")] = RunStrategy.FULL,
    prompt_version: Annotated[str, typer.Option("--prompt-version")] = "1",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        loaded = _load(task_path, settings)
        settings.ensure_runtime_directories()
        upgrade_database(settings.database_url)
        run = asyncio.run(_create_run(settings, loaded, strategy, model, prompt_version))
    except ValidationError as exc:
        _fail("TaskSpec schema validation failed", json_output=json_output, details=exc.errors())
    except (TaskSpecLoadError, SettingsError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    logger.info("run created", extra={"run_id": str(run.id), "task_id": loaded.spec.id})
    payload = run.model_dump(mode="json")
    payload["ok"] = True
    _emit(
        payload,
        json_output=json_output,
        human=f"Created pending Run {run.id} for TaskSpec {loaded.spec.id}",
    )


async def _get_run(settings: AppSettings, run_id: UUID) -> Run:
    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            return await RunRepository(session).get(run_id)
    finally:
        await database.close()


@run_app.command("show")
def show_run(
    run_id: Annotated[UUID, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        settings.sqlite_database_path()
        run = asyncio.run(_get_run(settings, run_id))
    except (SettingsError, RunNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = run.model_dump(mode="json")
    payload["ok"] = True
    _emit(payload, json_output=json_output, human=f"Run {run.id}: {run.status.value}")
