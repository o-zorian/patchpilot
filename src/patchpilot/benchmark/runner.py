from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from patchpilot.agent.events import EventEmitter, JsonlEventSink
from patchpilot.agent.loop import AgentLoop
from patchpilot.agent.registry import build_default_registry
from patchpilot.agent.strategies import policy_for
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.benchmark.models import (
    BenchmarkError,
    BenchmarkRunRecord,
    BenchmarkSuite,
    BenchmarkSummary,
    BenchmarkTask,
    load_benchmark,
)
from patchpilot.benchmark.reporting import build_summary, render_html, render_markdown
from patchpilot.domain.run import RunStrategy
from patchpilot.models.base import ModelConfig, ModelResponse, TokenUsage, ToolCall
from patchpilot.models.fake import ScriptedModelClient, ScriptItem
from patchpilot.quality.gate import HiddenTestInjection, QualityGate
from patchpilot.sandbox.local import git_argv, run_argv
from patchpilot.sandbox.trusted_local import TrustedLocalSandbox
from patchpilot.sandbox.workspace import Workspace, WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits


@dataclass(frozen=True, slots=True)
class BenchmarkRunOutput:
    output_directory: Path
    summary: BenchmarkSummary
    records: tuple[BenchmarkRunRecord, ...]


def _remove_tree(path: Path, *, attempts: int = 8) -> None:
    """Remove only a validated benchmark-owned tree, including read-only Git objects."""

    resolved = path.resolve(strict=False)
    for attempt in range(1, attempts + 1):
        if not resolved.exists():
            return
        for directory, directories, files in os.walk(resolved, topdown=False):
            for name in [*directories, *files]:
                entry = Path(directory) / name
                if not entry.is_symlink():
                    entry.chmod(stat.S_IREAD | stat.S_IWRITE)
        try:
            shutil.rmtree(resolved)
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(0.1 * attempt)


def _cleanup_workspace(manager: WorkspaceManager, workspace: Workspace) -> None:
    for attempt in range(1, 9):
        try:
            manager.cleanup(workspace)
            return
        except PermissionError:
            if attempt >= 8:
                raise
            time.sleep(0.1 * attempt)


def _command(argv: list[str], cwd: Path) -> None:
    result = run_argv(argv, cwd=cwd, timeout_seconds=60, output_max_chars=20_000)
    if result.return_code != 0:
        raise BenchmarkError(f"command failed ({argv[0]}): {result.stderr.strip()}")


def _prepare_repository(task: BenchmarkTask, destination: Path) -> None:
    shutil.copytree(task.repository_template, destination)
    _command(
        git_argv(["init", "--initial-branch=main", "--", str(destination)]), destination.parent
    )
    _command(git_argv(["add", "--all"]), destination)
    _command(
        git_argv(
            [
                "-c",
                "user.name=PatchPilot Benchmark",
                "-c",
                "user.email=benchmark@patchpilot.invalid",
                "commit",
                "-m",
                "benchmark baseline",
            ]
        ),
        destination,
    )


def _call(identifier: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=arguments)


def _response(index: int, *calls: ToolCall) -> ModelResponse:
    prompt_tokens = 100 + index * 7
    completion_tokens = 20 + len(calls) * 5
    return ModelResponse(
        content=None,
        tool_calls=list(calls),
        finish_reason="tool_calls",
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model="scripted-benchmark-v1",
        provider_request_id=f"offline-{index}",
        latency_ms=0,
    )


def _finish(identifier: str, tests: list[str]) -> ToolCall:
    return _call(
        identifier,
        "finish",
        {
            "summary": "deterministic benchmark repair attempt complete",
            "tests_run": tests,
            "remaining_risks": ["offline scripted model"],
        },
    )


def _script_for(task: BenchmarkTask, strategy: RunStrategy) -> ScriptedModelClient:
    patch = task.scripted_patch.read_text(encoding="utf-8")
    source = "cases.py" if task.language == "python" else "cases.go"
    read = _call("read", "read_file", {"path": source, "start_line": 1, "end_line": 400})
    apply = _call("patch", "apply_patch", {"patch": patch})
    tests = _call("tests", "run_tests", {"profile_command_id": "acceptance:0"})
    finish = _finish("finish", ["acceptance:0"])
    success_limit = {
        RunStrategy.SINGLE_SHOT: 5,
        RunStrategy.AGENT_LOOP: 10,
        RunStrategy.AGENT_LOOP_WITH_TESTS: 16,
        RunStrategy.FULL: 20,
    }[strategy]

    if strategy == RunStrategy.SINGLE_SHOT:
        calls = (apply, finish) if task.ordinal <= success_limit else (finish,)
        return ScriptedModelClient([_response(1, *calls)])
    if strategy == RunStrategy.AGENT_LOOP:
        script: list[ScriptItem] = [_response(1, read)]
        if task.ordinal <= success_limit:
            script.extend([_response(2, apply), _response(3, finish)])
        else:
            script.append(_response(2, finish))
        return ScriptedModelClient(script)
    if strategy == RunStrategy.AGENT_LOOP_WITH_TESTS:
        if task.ordinal <= 10:
            return ScriptedModelClient(
                [
                    _response(1, read),
                    _response(2, apply),
                    _response(3, tests),
                    _response(4, finish),
                ]
            )
        if task.ordinal <= success_limit:
            return ScriptedModelClient(
                [
                    _response(1, read),
                    _response(2, _finish("first-finish", [])),
                    _response(3, apply),
                    _response(4, finish),
                ]
            )
        return ScriptedModelClient(
            [
                _response(1, read),
                _response(2, _finish("first-finish", [])),
                _response(3, _finish("second-finish", [])),
                _response(4, _finish("third-finish", [])),
            ]
        )
    if strategy != RunStrategy.FULL:
        raise BenchmarkError(f"unsupported mandatory benchmark strategy: {strategy.value}")
    return ScriptedModelClient(
        [
            _response(1, _call("list", "list_files", {"path": ".", "max_depth": 3})),
            _response(
                2,
                _call(
                    "search",
                    "search_code",
                    {"query": "case", "path": ".", "max_results": 20},
                ),
            ),
            _response(3, read),
            _response(4, apply),
            _response(5, tests),
            _response(6, _call("diff", "git_diff", {"stat_only": True})),
            _response(7, finish),
        ]
    )


async def _run_one(
    suite: BenchmarkSuite,
    task: BenchmarkTask,
    strategy: RunStrategy,
    repetition: int,
    output: Path,
    work_root: Path,
) -> BenchmarkRunRecord:
    run_id = uuid4()
    prepared = work_root / "sources" / str(run_id)
    workspace_manager = WorkspaceManager(work_root / "workspaces")
    workspace = None
    try:
        await asyncio.to_thread(_prepare_repository, task, prepared)
        rewritten_spec = task.spec.model_copy(
            update={
                "repository": task.spec.repository.model_copy(
                    update={"path": str(prepared), "base_ref": "main"}
                )
            }
        )
        workspace = await asyncio.to_thread(
            workspace_manager.create,
            prepared,
            workspace_id=str(run_id),
            base_ref="main",
        )
        context = ToolContext.create(
            workspace,
            rewritten_spec,
            ToolLimits(),
            command_sandbox=TrustedLocalSandbox(),
        )
        artifacts = ArtifactStore(output / "runs", run_id)
        events = EventEmitter(
            run_id,
            [JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG))],
        )
        gate = QualityGate(
            run_id=run_id,
            context=context,
            events=events,
            artifacts=artifacts,
            hidden_test=HiddenTestInjection(task.hidden_test, task.hidden_target),
            acceptance_environment={"GOCACHE": str(work_root / "go-cache")},
        )
        policy = policy_for(strategy)
        client = _script_for(task, strategy)
        loop = AgentLoop(
            model_client=client,
            model_config=ModelConfig(
                model=suite.manifest.model,
                temperature=suite.manifest.temperature,
                max_tokens=suite.manifest.max_tokens,
                max_retries=0,
                input_cost_per_million_usd=Decimal("0.50"),
                output_cost_per_million_usd=Decimal("1.50"),
            ),
            tool_context=context,
            registry=build_default_registry(context, allowed_tools=policy.allowed_tools),
            events=events,
            quality_gate=gate,
            strategy_policy=policy,
        )
        result = await loop.run(run_id)
        if result.scorecard is None:
            raise RuntimeError("benchmark Run did not produce a Scorecard")
        scorecard = result.scorecard
        failures = {failure.result.value for failure in scorecard.failures}
        return BenchmarkRunRecord(
            run_id=str(run_id),
            task_id=task.spec.id,
            strategy=strategy,
            repetition=repetition,
            language=task.spec.repository.language,
            difficulty=task.difficulty,
            defect=task.defect,
            result=scorecard.result.value,
            passed=scorecard.result.value == "PASSED",
            first_gate_passed=scorecard.result.value == "PASSED" and not scorecard.failures,
            steps=scorecard.metrics.steps,
            model_calls=scorecard.metrics.model_calls,
            tool_calls=scorecard.metrics.tool_calls,
            prompt_tokens=scorecard.metrics.prompt_tokens,
            completion_tokens=scorecard.metrics.completion_tokens,
            estimated_cost_usd=scorecard.metrics.estimated_cost_usd,
            wall_time_seconds=scorecard.metrics.wall_time_seconds,
            scope_violation="SCOPE_VIOLATION" in failures,
            regression="REGRESSION" in failures,
            artifact_directory=(Path("runs") / str(run_id)).as_posix(),
        )
    finally:
        if workspace is not None:
            await asyncio.to_thread(_cleanup_workspace, workspace_manager, workspace)
        if prepared.exists():
            await asyncio.to_thread(_remove_tree, prepared)


async def run_benchmark(
    suite_path: Path,
    output_directory: Path,
    *,
    strategies: tuple[RunStrategy, ...] | None = None,
    repetitions: int | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
) -> BenchmarkRunOutput:
    suite = load_benchmark(suite_path)
    selected = strategies or tuple(suite.manifest.strategies)
    if not selected or any(strategy not in suite.manifest.strategies for strategy in selected):
        raise BenchmarkError("selected strategies must come from benchmark.yaml")
    repeat_count = repetitions or suite.manifest.repetitions
    if repeat_count <= 0 or repeat_count > 10:
        raise BenchmarkError("repetitions must be between 1 and 10")
    selected_tasks = suite.tasks[:limit] if limit is not None else suite.tasks
    if not selected_tasks:
        raise BenchmarkError("task limit selected no tasks")
    parallelism = concurrency or suite.manifest.concurrency
    if parallelism <= 0 or parallelism > 16:
        raise BenchmarkError("concurrency must be between 1 and 16")

    output = output_directory.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    work_root = output / ".work"
    (work_root / "sources").mkdir(parents=True)
    semaphore = asyncio.Semaphore(parallelism)

    async def guarded(
        task: BenchmarkTask, strategy: RunStrategy, repetition: int
    ) -> BenchmarkRunRecord:
        async with semaphore:
            return await _run_one(suite, task, strategy, repetition, output, work_root)

    jobs = [
        asyncio.create_task(guarded(task, strategy, repetition))
        for strategy in selected
        for task in selected_tasks
        for repetition in range(1, repeat_count + 1)
    ]
    try:
        records = list(await asyncio.gather(*jobs))
    except BaseException:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    finally:
        if work_root.exists():
            _remove_tree(work_root)
    records.sort(key=lambda record: (record.strategy.value, record.task_id, record.repetition))
    raw = "".join(record.model_dump_json() + "\n" for record in records)
    (output / "raw.jsonl").write_text(raw, encoding="utf-8", newline="\n")
    summary = build_summary(
        suite,
        records,
        selected_strategies=[strategy.value for strategy in selected],
        repetitions=repeat_count,
    )
    (output / "summary.json").write_text(
        summary.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (output / "report.md").write_text(render_markdown(summary), encoding="utf-8", newline="\n")
    (output / "report.html").write_text(render_html(summary), encoding="utf-8", newline="\n")
    return BenchmarkRunOutput(output, summary, tuple(records))


def compare_summaries(paths: list[Path]) -> str:
    if len(paths) < 2:
        raise BenchmarkError("compare requires at least two summary.json files")
    rows = ["result\truns\tpass_rate\tavg_cost_usd\tavg_wall_seconds"]
    for path in paths:
        try:
            summary = BenchmarkSummary.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchmarkError(f"invalid summary {path}: {exc}") from exc
        rows.append(
            f"{path}\t{summary.overall.runs}\t{summary.overall.pass_rate:.6f}\t"
            f"{summary.overall.average_cost_usd:.6f}\t"
            f"{summary.overall.average_wall_time_seconds:.6f}"
        )
    return "\n".join(rows) + "\n"
