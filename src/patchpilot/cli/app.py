from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import ValidationError

from patchpilot import __version__
from patchpilot.benchmark.models import BenchmarkError, load_benchmark
from patchpilot.benchmark.real_models import (
    RealBenchmarkRunRecord,
    load_real_benchmark,
    load_real_experiment_profile,
)
from patchpilot.benchmark.real_runner import (
    RealBenchmarkStopped,
    effective_experiment_task_spec,
    estimate_full_matrix,
    ping_real_model,
    run_real_benchmark,
    verify_real_fixtures,
)
from patchpilot.benchmark.runner import compare_summaries, run_benchmark
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
from patchpilot.persistence.repositories import RunNotFoundError, TaskNotFoundError
from patchpilot.queue import InMemoryRunQueue
from patchpilot.services import RunExecutor, RunService, task_limits

app = typer.Typer(no_args_is_help=True, help="PatchPilot controlled coding-agent harness.")
task_app = typer.Typer(no_args_is_help=True, help="Validate and inspect TaskSpec documents.")
run_app = typer.Typer(no_args_is_help=True, help="Create and inspect persisted Run records.")
db_app = typer.Typer(no_args_is_help=True, help="Manage the PatchPilot database schema.")
benchmark_app = typer.Typer(no_args_is_help=True, help="Validate and run local benchmarks.")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(db_app, name="db")
app.add_typer(benchmark_app, name="benchmark")
logger = logging.getLogger(__name__)


def _settings() -> AppSettings:
    settings = AppSettings()
    configure_logging(settings.log_level)
    return settings


def _limits(settings: AppSettings) -> TaskLimits:
    return task_limits(settings)


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
    typer.echo(f"Database upgraded: {settings.database_url}")


async def _create_run(
    settings: AppSettings,
    loaded: LoadedTaskSpec,
    strategy: RunStrategy,
    model: str,
    prompt_version: str,
) -> Run:
    database = Database(settings.database_url)
    queue = InMemoryRunQueue()
    try:
        service = RunService(database, queue, settings)
        task = await service.create_task(
            loaded.spec,
            owner_id=settings.service_owner_id,
            base_directory=loaded.source_path.parent,
        )
        run, _ = await service.submit_run(
            task.id,
            owner_id=settings.service_owner_id,
            strategy=strategy,
            model=model,
            idempotency_key=None,
            prompt_version=prompt_version,
        )
        return run
    finally:
        await queue.close()
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
    queue = InMemoryRunQueue()
    try:
        return await RunService(database, queue, settings).get_run(
            run_id, owner_id=settings.service_owner_id
        )
    finally:
        await queue.close()
        await database.close()


@run_app.command("show")
def show_run(
    run_id: Annotated[UUID, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        settings.database_backend()
        run = asyncio.run(_get_run(settings, run_id))
    except (SettingsError, RunNotFoundError, TaskNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = run.model_dump(mode="json")
    payload["ok"] = True
    _emit(payload, json_output=json_output, human=f"Run {run.id}: {run.status.value}")


async def _execute_run(
    settings: AppSettings,
    loaded: LoadedTaskSpec,
    strategy: RunStrategy,
    model: str,
) -> Run:
    database = Database(settings.database_url)
    queue = InMemoryRunQueue()
    try:
        service = RunService(database, queue, settings)
        task = await service.create_task(
            loaded.spec,
            owner_id=settings.service_owner_id,
            base_directory=loaded.source_path.parent,
        )
        run, _ = await service.submit_run(
            task.id,
            owner_id=settings.service_owner_id,
            strategy=strategy,
            model=model,
            idempotency_key=None,
        )
        await RunExecutor(database, settings).execute(run.id, worker_id="cli-foreground")
        return await service.get_run(run.id, owner_id=settings.service_owner_id)
    finally:
        await queue.close()
        await database.close()


@run_app.command("execute")
def execute_run(
    task_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    model: Annotated[str, typer.Option("--model")],
    strategy: Annotated[RunStrategy, typer.Option("--strategy")] = RunStrategy.FULL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute a Run in the foreground through the same Agent Loop as the Worker."""
    settings = _settings()
    try:
        loaded = _load(task_path, settings)
        settings.ensure_runtime_directories()
        upgrade_database(settings.database_url)
        run = asyncio.run(_execute_run(settings, loaded, strategy, model))
    except (TaskSpecLoadError, SettingsError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = run.model_dump(mode="json")
    payload["ok"] = True
    _emit(payload, json_output=json_output, human=f"Run {run.id}: {run.status.value}")


async def _cancel_run(settings: AppSettings, run_id: UUID) -> Run:
    database = Database(settings.database_url)
    queue = InMemoryRunQueue()
    try:
        return await RunService(database, queue, settings).cancel_run(
            run_id, owner_id=settings.service_owner_id
        )
    finally:
        await queue.close()
        await database.close()


@run_app.command("cancel")
def cancel_run(
    run_id: Annotated[UUID, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        settings.database_backend()
        run = asyncio.run(_cancel_run(settings, run_id))
    except (SettingsError, RunNotFoundError, TaskNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = run.model_dump(mode="json")
    payload["ok"] = True
    _emit(payload, json_output=json_output, human=f"Run {run.id}: cancellation requested")


@benchmark_app.command("validate")
def validate_benchmark(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        suite = load_benchmark(path)
    except BenchmarkError as exc:
        _fail(str(exc), json_output=json_output)
    languages: dict[str, int] = {}
    difficulties: dict[str, int] = {}
    defects: set[str] = set()
    for benchmark_task in suite.tasks:
        languages[benchmark_task.language] = languages.get(benchmark_task.language, 0) + 1
        difficulties[benchmark_task.difficulty] = difficulties.get(benchmark_task.difficulty, 0) + 1
        defects.add(benchmark_task.defect)
    payload = {
        "ok": True,
        "benchmark_id": suite.manifest.id,
        "tasks": len(suite.tasks),
        "languages": languages,
        "difficulties": difficulties,
        "defect_categories": sorted(defects),
        "strategies": [strategy.value for strategy in suite.manifest.strategies],
        "task_set_sha256": suite.task_set_sha256,
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Valid benchmark {suite.manifest.id}: {len(suite.tasks)} tasks, "
            f"{len(defects)} defect categories"
        ),
    )


@benchmark_app.command("run")
def execute_benchmark(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", file_okay=False)],
    strategy: Annotated[
        list[RunStrategy] | None,
        typer.Option("--strategy", help="Repeat to select strategies; default runs all four."),
    ] = None,
    repetitions: Annotated[int | None, typer.Option("--repetitions", min=1, max=10)] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", min=1, max=16)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = asyncio.run(
            run_benchmark(
                path,
                output,
                strategies=tuple(strategy) if strategy else None,
                repetitions=repetitions,
                limit=limit,
                concurrency=concurrency,
            )
        )
    except (BenchmarkError, OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "benchmark_id": result.summary.benchmark_id,
        "runs": result.summary.overall.runs,
        "pass_rate": result.summary.overall.pass_rate,
        "output_directory": str(result.output_directory),
        "artifacts": ["raw.jsonl", "summary.json", "report.md", "report.html"],
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Benchmark {result.summary.benchmark_id}: {result.summary.overall.runs} runs, "
            f"pass rate {result.summary.overall.pass_rate:.1%}; "
            f"reports in {result.output_directory}"
        ),
    )


@benchmark_app.command("compare")
def compare_benchmark(
    summaries: Annotated[
        list[Path],
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    try:
        typer.echo(compare_summaries(summaries), nl=False)
    except BenchmarkError as exc:
        _fail(str(exc), json_output=False)


@benchmark_app.command("real-validate")
def validate_real_benchmark(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    verify_fixtures: Annotated[
        bool,
        typer.Option(
            "--verify-fixtures",
            help="Run every baseline and human reference repair in the Docker Sandbox.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate an independent real-model suite without making model API calls."""

    settings = _settings()
    try:
        suite = load_real_benchmark(path)
        fixture_results = (
            asyncio.run(verify_real_fixtures(path, settings=settings)) if verify_fixtures else []
        )
    except (BenchmarkError, OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    languages = {
        language: sum(task.language == language for task in suite.tasks)
        for language in ("python", "go")
    }
    difficulties = {
        difficulty: sum(task.difficulty == difficulty for task in suite.tasks)
        for difficulty in ("easy", "medium", "hard")
    }
    payload = {
        "ok": True,
        "benchmark_id": suite.manifest.id,
        "suite_kind": suite.manifest.suite_kind.value,
        "frozen": suite.manifest.frozen,
        "tasks": len(suite.tasks),
        "languages": languages,
        "difficulties": difficulties,
        "defect_categories": sorted({task.defect for task in suite.tasks}),
        "multi_file_tasks": sum(task.source.changed_files_expected >= 2 for task in suite.tasks),
        "manifest_sha256": suite.manifest_sha256,
        "task_set_sha256": suite.task_set_sha256,
        "fixture_runs_verified": len(fixture_results),
        "network_model_calls": False,
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Valid {suite.manifest.suite_kind.value} suite {suite.manifest.id}: "
            f"{len(suite.tasks)} tasks; fixture audits={len(fixture_results)}; "
            f"manifest SHA-256 {suite.manifest_sha256}"
        ),
    )


@benchmark_app.command("real-ping")
def real_model_ping(
    real_model: Annotated[
        bool,
        typer.Option(
            "--real-model",
            help="Explicitly authorize one paid compatibility request.",
        ),
    ] = False,
    max_total_cost_usd: Annotated[
        float,
        typer.Option("--max-total-cost-usd", min=0.000001),
    ] = 0.02,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        result = asyncio.run(
            ping_real_model(
                settings,
                explicit_real_model=real_model,
                provider=settings.model_provider,
                global_cost_limit=Decimal(str(max_total_cost_usd)),
            )
        )
    except (BenchmarkError, SettingsError, OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "provider": result.provider,
        "requested_model": result.requested_model,
        "actual_model": result.actual_model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "estimated_cost_usd": str(result.estimated_cost_usd),
        "usage_estimated": result.usage_estimated,
        "latency_ms": result.latency_ms,
        "structured_tool_call": True,
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Real model compatibility passed: {result.actual_model}; "
            f"tokens={result.prompt_tokens + result.completion_tokens}; "
            f"cost=${result.estimated_cost_usd}; latency={result.latency_ms}ms"
        ),
    )


@benchmark_app.command("real-run")
def execute_real_benchmark(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", file_okay=False)],
    max_total_cost_usd: Annotated[
        float,
        typer.Option("--max-total-cost-usd", min=0.000001),
    ],
    real_model: Annotated[
        bool,
        typer.Option(
            "--real-model",
            help="Explicitly authorize paid model calls for this command.",
        ),
    ] = False,
    strategy: Annotated[
        list[RunStrategy] | None,
        typer.Option("--strategy", help="Calibration only; repeat to select strategies."),
    ] = None,
    repetitions: Annotated[int | None, typer.Option("--repetitions", min=1, max=3)] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Calibration only; forbidden for formal real-v1."),
    ] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", min=1, max=4)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        result = asyncio.run(
            run_real_benchmark(
                path,
                output,
                settings=settings,
                explicit_real_model=real_model,
                provider=settings.model_provider,
                global_cost_limit=Decimal(str(max_total_cost_usd)),
                strategies=tuple(strategy) if strategy else None,
                repetitions=repetitions,
                limit=limit,
                concurrency=concurrency,
            )
        )
    except RealBenchmarkStopped as exc:
        _fail(str(exc), json_output=json_output)
    except (BenchmarkError, SettingsError, OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "benchmark_id": result.summary.benchmark_id,
        "suite_kind": result.summary.suite_kind.value,
        "persisted_runs": result.summary.counts["persisted_runs"],
        "task_results": result.summary.counts["task_results"],
        "api_errors": result.summary.counts["api_errors"],
        "infrastructure_errors": result.summary.counts["infrastructure_errors"],
        "accounted_cost_usd": result.summary.cost["accounted_cost_usd"],
        "resumed_runs": result.resumed_runs,
        "output_directory": str(result.output_directory),
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Real Benchmark {result.summary.benchmark_id}: "
            f"{result.summary.counts['persisted_runs']} persisted runs; "
            f"accounted cost ${result.summary.cost['accounted_cost_usd']}; "
            f"reports in {result.output_directory}"
        ),
    )


@benchmark_app.command("real-experiment-validate")
def validate_real_experiment(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    profile_path: Annotated[
        Path,
        typer.Option("--profile", exists=True, dir_okay=False, readable=True),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        suite = load_real_benchmark(path)
        loaded = load_real_experiment_profile(profile_path, suite)
        profile = loaded.profile
        if not suite.manifest.frozen:
            raise BenchmarkError("real experiment requires a frozen base benchmark")
        for task in suite.tasks:
            effective = effective_experiment_task_spec(task.spec, loaded)
            if (
                effective.allowed_paths != task.spec.allowed_paths
                or effective.denied_paths != task.spec.denied_paths
                or effective.execution != task.spec.execution
                or effective.acceptance != task.spec.acceptance
                or effective.budget.max_changed_files != task.spec.budget.max_changed_files
                or effective.budget.max_patch_lines != task.spec.budget.max_patch_lines
            ):
                raise BenchmarkError("experiment profile changed a frozen quality constraint")
    except (BenchmarkError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "network_model_calls": False,
        "experiment_id": profile.id,
        "classification": profile.classification,
        "base_benchmark_id": suite.manifest.id,
        "manifest_sha256": suite.manifest_sha256,
        "task_set_sha256": suite.task_set_sha256,
        "experiment_profile_sha256": loaded.sha256,
        "tasks": len(suite.tasks),
        "strategies": [profile.strategy.value],
        "repetitions": profile.repetitions,
        "runs": len(suite.tasks) * profile.repetitions,
        "concurrency": profile.concurrency,
        "global_cost_limit_usd": str(profile.global_cost_limit_usd),
        "budget": profile.budget.model_dump(mode="json"),
        "quality_constraints_unchanged": True,
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Experiment {profile.id} is valid: {payload['runs']} fixed full-strategy Runs; "
            f"profile SHA-256 {loaded.sha256}; no model request was made"
        ),
    )


@benchmark_app.command("real-experiment-run")
def execute_real_experiment(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    profile_path: Annotated[
        Path,
        typer.Option("--profile", exists=True, dir_okay=False, readable=True),
    ],
    output: Annotated[Path, typer.Option("--output", "-o", file_okay=False)],
    max_total_cost_usd: Annotated[
        float,
        typer.Option("--max-total-cost-usd", min=0.000001),
    ],
    real_model: Annotated[
        bool,
        typer.Option(
            "--real-model",
            help="Explicitly authorize paid model calls for this experiment.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        result = asyncio.run(
            run_real_benchmark(
                path,
                output,
                settings=settings,
                explicit_real_model=real_model,
                provider=settings.model_provider,
                global_cost_limit=Decimal(str(max_total_cost_usd)),
                experiment_profile_path=profile_path,
            )
        )
    except RealBenchmarkStopped as exc:
        _fail(str(exc), json_output=json_output)
    except (BenchmarkError, SettingsError, OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    payload = {
        "ok": True,
        "experiment_id": result.summary.benchmark_id,
        "persisted_runs": result.summary.counts["persisted_runs"],
        "expected_runs": result.summary.counts["expected_runs"],
        "accounted_cost_usd": result.summary.cost["accounted_cost_usd"],
        "resumed_runs": result.resumed_runs,
        "output_directory": str(result.output_directory),
    }
    _emit(
        payload,
        json_output=json_output,
        human=(
            f"Real experiment {result.summary.benchmark_id}: "
            f"{payload['persisted_runs']}/{payload['expected_runs']} persisted Runs; "
            f"accounted cost ${payload['accounted_cost_usd']}"
        ),
    )


@benchmark_app.command("real-estimate")
def estimate_real_benchmark(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    calibration_raw: Annotated[
        Path | None,
        typer.Option(
            "--calibration-raw",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Optional real calibration raw.jsonl used for observed estimates.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    settings = _settings()
    try:
        suite = load_real_benchmark(path)
        records: list[RealBenchmarkRunRecord] = []
        if calibration_raw is not None:
            for line in calibration_raw.read_text("utf-8").splitlines():
                if line.strip():
                    records.append(RealBenchmarkRunRecord.model_validate_json(line))
        estimate = estimate_full_matrix(
            suite,
            input_cost_per_million=settings.model_input_cost_per_million_usd,
            output_cost_per_million=settings.model_output_cost_per_million_usd,
            observed_records=records,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(
        {"ok": True, **estimate},
        json_output=json_output,
        human=(
            f"Full matrix: {estimate['runs']} runs; expected usage cost "
            f"${estimate['expected_usage_cost_usd']}; task-budget worst case "
            f"${estimate['worst_case_task_budget_cost_usd']}"
        ),
    )
