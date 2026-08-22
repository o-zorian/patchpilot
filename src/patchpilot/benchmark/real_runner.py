from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from patchpilot.agent.events import EventEmitter, EventType, JsonlEventSink
from patchpilot.agent.loop import AgentLoop
from patchpilot.agent.registry import build_default_registry
from patchpilot.agent.strategies import policy_for
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.benchmark.models import BenchmarkError
from patchpilot.benchmark.real_models import (
    LoadedRealExperimentProfile,
    OutcomeClass,
    RealBenchmarkRunRecord,
    RealBenchmarkSuite,
    RealBenchmarkSummary,
    RealBenchmarkTask,
    RealSuiteKind,
    load_real_benchmark,
    load_real_experiment_profile,
    real_run_key,
)
from patchpilot.benchmark.real_reporting import (
    build_real_summary,
    render_real_html,
    render_real_markdown,
)
from patchpilot.config import AppSettings, SettingsError
from patchpilot.domain.run import RunStrategy
from patchpilot.domain.scorecard import (
    GateChecks,
    GateFailure,
    QualityResult,
    Scorecard,
    ScorecardArtifacts,
    ScorecardMetrics,
)
from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import (
    Message,
    MessageRole,
    ModelClient,
    ModelClientError,
    ModelConfig,
    ModelResponse,
    ToolSchema,
)
from patchpilot.models.openai_compatible import OpenAICompatibleClient
from patchpilot.profiles import profile_for
from patchpilot.quality.gate import HiddenTestInjection, QualityGate
from patchpilot.reporting.render import render_html as render_run_html
from patchpilot.reporting.render import render_markdown as render_run_markdown
from patchpilot.sandbox.docker import DockerSandbox
from patchpilot.sandbox.local import git_argv, run_argv
from patchpilot.sandbox.workspace import Workspace, WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits

_FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
_API_ERROR_CODES = {
    "MODEL_ERROR",
    "MODEL_RATE_LIMIT",
    "MODEL_SERVER_ERROR",
    "MODEL_TIMEOUT",
    "MODEL_PROTOCOL_ERROR",
}


class GlobalCostLimitError(ModelClientError):
    code = "GLOBAL_COST_LIMIT"


class RealBenchmarkStopped(BenchmarkError):
    """Raised after safely persisting progress when the global cap prevents another Run."""


@dataclass(frozen=True, slots=True)
class RealBenchmarkOutput:
    output_directory: Path
    summary: RealBenchmarkSummary
    records: tuple[RealBenchmarkRunRecord, ...]
    resumed_runs: int


@dataclass(frozen=True, slots=True)
class PingResult:
    provider: str
    requested_model: str
    actual_model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal
    usage_estimated: bool
    latency_ms: int


class GlobalCostLedger:
    """Conservatively authorize each API request before it can reach the provider."""

    def __init__(
        self,
        limit: Decimal,
        *,
        consumed: Decimal = Decimal(0),
        unknown: Decimal = Decimal(0),
    ) -> None:
        if limit <= 0:
            raise ValueError("global model cost limit must be positive")
        if consumed < 0 or unknown < 0 or consumed + unknown > limit:
            raise ValueError("persisted cost exceeds the configured global limit")
        self.limit = limit
        self.consumed = consumed
        self.unknown = unknown
        self._reservations: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()

    @property
    def accounted(self) -> Decimal:
        return self.consumed + self.unknown

    async def can_start(self, run_limit: Decimal) -> bool:
        async with self._lock:
            reserved = sum(self._reservations.values(), Decimal(0))
            return self.accounted + reserved + run_limit <= self.limit

    async def reserve(self, maximum: Decimal) -> str:
        token = uuid4().hex
        async with self._lock:
            reserved = sum(self._reservations.values(), Decimal(0))
            if self.accounted + reserved + maximum > self.limit:
                raise GlobalCostLimitError("global model cost hard limit would be exceeded")
            self._reservations[token] = maximum
        return token

    async def settle(self, token: str, actual: Decimal) -> None:
        async with self._lock:
            maximum = self._reservations.pop(token)
            if actual > maximum:
                # The request bound uses a byte-count token upper bound. Reaching this branch
                # indicates provider accounting incompatible with that conservative contract.
                self.unknown += maximum
                raise GlobalCostLimitError("provider usage exceeded the pre-authorized cost")
            self.consumed += actual

    async def release(self, token: str) -> None:
        async with self._lock:
            self._reservations.pop(token, None)

    async def mark_unknown(self, token: str) -> Decimal:
        async with self._lock:
            maximum = self._reservations.pop(token)
            self.unknown += maximum
            return maximum


class AuditedRealModelClient:
    """Keep model telemetry in memory while never retaining messages or credentials."""

    def __init__(
        self,
        delegate: ModelClient,
        ledger: GlobalCostLedger,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.actual_models: set[str] = set()
        self.total_latency_ms = 0
        self.usage_estimated = False
        self.unknown_cost = Decimal(0)

    @staticmethod
    def _maximum_request_cost(
        messages: list[Message], tools: list[ToolSchema], config: ModelConfig
    ) -> Decimal:
        # UTF-8 byte length is a conservative upper bound for token count for the
        # byte-level BPE APIs targeted here. The fixed margin covers message framing.
        material = json.dumps(
            {
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [tool.model_dump(mode="json") for tool in tools],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        maximum_input_tokens = len(material) + 2_048
        million = Decimal(1_000_000)
        return (
            Decimal(maximum_input_tokens) * config.input_cost_per_million_usd
            + Decimal(config.max_tokens) * config.output_cost_per_million_usd
        ) / million

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse:
        reservation = await self.ledger.reserve(self._maximum_request_cost(messages, tools, config))
        try:
            response = await self.delegate.complete(messages, tools, config)
        except (asyncio.CancelledError, ModelClientError):
            # Once dispatch may have happened, an absent usage response cannot prove
            # that the provider did not bill the request. Retain the full reservation.
            self.unknown_cost += await self.ledger.mark_unknown(reservation)
            raise
        except BaseException:
            await self.ledger.release(reservation)
            raise
        actual = config.cost_for(response.usage)
        await self.ledger.settle(reservation, actual)
        self.actual_models.add(response.model)
        self.total_latency_ms += response.latency_ms
        self.usage_estimated = self.usage_estimated or response.usage.estimated
        return response


def require_real_model(settings: AppSettings, *, explicit_real_model: bool) -> ModelConfig:
    if not explicit_real_model:
        raise SettingsError("the explicit --real-model CLI flag is required")
    config = settings.real_model_config()
    if config.api_key is None or not config.api_key.get_secret_value():
        raise SettingsError("MODEL_API_KEY is required")
    if config.input_cost_per_million_usd <= 0 or config.output_cost_per_million_usd <= 0:
        raise SettingsError(
            "positive MODEL_INPUT_COST_PER_MILLION_USD and "
            "MODEL_OUTPUT_COST_PER_MILLION_USD are required for audited real runs"
        )
    return config


def _remove_tree(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        return
    for directory, directories, files in os.walk(resolved, topdown=False):
        for name in [*directories, *files]:
            entry = Path(directory) / name
            if not entry.is_symlink():
                with suppress(OSError):
                    entry.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(resolved)


def _command(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout: float = 60,
    environment: Mapping[str, str] | None = None,
) -> str:
    result = run_argv(
        list(argv),
        cwd=cwd,
        timeout_seconds=timeout,
        output_max_chars=20_000,
        environment=environment,
    )
    if result.return_code != 0:
        raise BenchmarkError(f"command failed safely: {Path(argv[0]).name}")
    return (result.stdout or result.stderr).strip()


def _prepare_repository(task: RealBenchmarkTask, destination: Path) -> str:
    before = task.repository_sha256
    shutil.copytree(task.repository_template, destination)
    _command(
        git_argv(["init", "--initial-branch=main", "--", str(destination)]), destination.parent
    )
    _command(git_argv(["add", "--all"]), destination)
    _command(
        git_argv(
            [
                "-c",
                "user.name=PatchPilot Real Benchmark",
                "-c",
                "user.email=real-benchmark@patchpilot.invalid",
                "commit",
                "-m",
                "frozen benchmark baseline",
            ]
        ),
        destination,
        environment=_FIXED_GIT_ENVIRONMENT,
    )
    baseline = _command(git_argv(["rev-parse", "HEAD"]), destination)
    from patchpilot.benchmark.real_models import sha256_tree

    if sha256_tree(task.repository_template) != before:
        raise BenchmarkError("source repository changed during preparation")
    return baseline


def _docker_sandbox(
    settings: AppSettings,
    workspace: Workspace,
    task: RealBenchmarkTask,
    run_id: UUID,
) -> DockerSandbox:
    image = (
        settings.sandbox_image_python if task.language == "python" else settings.sandbox_image_go
    )
    return DockerSandbox(
        workspace=workspace.path,
        run_id=run_id,
        language=task.language,
        image=image,
        cpu_limit=task.spec.execution.cpu_limit,
        memory_limit_mb=task.spec.execution.memory_limit_mb,
        pids_limit=settings.sandbox_pids_limit,
        docker_binary=settings.sandbox_docker_binary,
    )


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def _write_infrastructure_artifacts(
    artifacts: ArtifactStore,
    events: EventEmitter,
    task_id: str,
    *,
    error_code: str,
) -> Scorecard:
    await events.emit(
        EventType.RUN_FAILED,
        {
            "status": "failed",
            "result_code": QualityResult.SYSTEM_ERROR.value,
            "error_code": error_code,
        },
    )
    scorecard = Scorecard(
        run_id=artifacts.run_id,
        task_id=task_id,
        result=QualityResult.SYSTEM_ERROR,
        checks=GateChecks(
            has_patch=False,
            scope_valid=False,
            patch_size_valid=False,
            tests_passed=False,
            required_tests_ran=False,
            budget_valid=True,
        ),
        metrics=ScorecardMetrics(
            steps=0,
            model_calls=0,
            tool_calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            estimated_cost_usd=Decimal(0),
            wall_time_seconds=0,
        ),
        failures=[
            GateFailure(
                result=QualityResult.SYSTEM_ERROR,
                summary=f"Real Benchmark infrastructure failed safely: {error_code}",
            )
        ],
        artifacts=ScorecardArtifacts(
            patch=artifacts.relative_path_for(ArtifactKind.PATCH),
            events=artifacts.relative_path_for(ArtifactKind.EVENT_LOG),
            test_log=artifacts.relative_path_for(ArtifactKind.TEST_LOG),
            scorecard=artifacts.relative_path_for(ArtifactKind.SCORECARD),
            report_markdown=artifacts.relative_path_for(ArtifactKind.REPORT_MARKDOWN),
            report_html=artifacts.relative_path_for(ArtifactKind.REPORT_HTML),
        ),
    )
    await artifacts.write_text(ArtifactKind.PATCH, "")
    await artifacts.write_text(
        ArtifactKind.TEST_LOG,
        f"Acceptance was not completed because of infrastructure error {error_code}.\n",
    )
    await artifacts.write_text(ArtifactKind.REPORT_MARKDOWN, render_run_markdown(scorecard))
    await artifacts.write_text(
        ArtifactKind.REPORT_HTML,
        render_run_html(
            scorecard,
            patch="",
            test_log="Acceptance was not completed.",
            events_jsonl=artifacts.path_for(ArtifactKind.EVENT_LOG).read_text("utf-8"),
        ),
    )
    await artifacts.write_text(ArtifactKind.SCORECARD, scorecard.model_dump_json(indent=2) + "\n")
    await artifacts.record_existing(ArtifactKind.EVENT_LOG)
    return scorecard


def _outcome_class(result: str, error_code: str | None) -> OutcomeClass:
    if error_code in _API_ERROR_CODES or result == QualityResult.MODEL_ERROR.value:
        return OutcomeClass.API_ERROR
    if error_code == GlobalCostLimitError.code:
        return OutcomeClass.INFRASTRUCTURE_ERROR
    if result in {QualityResult.SANDBOX_ERROR.value, QualityResult.SYSTEM_ERROR.value}:
        return OutcomeClass.INFRASTRUCTURE_ERROR
    return OutcomeClass.TASK_RESULT


def _event_audit(events: list[dict[str, object]]) -> tuple[int, int, bool, bool]:
    model_attempts = 0
    retries = 0
    usage_estimated = False
    gate_started = 0
    gate_passed = False
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if event_type == EventType.MODEL_REQUESTED.value:
            model_attempts += 1
        elif event_type == EventType.MODEL_RETRYING.value:
            retries += 1
        elif event_type == EventType.MODEL_RESPONDED.value:
            usage_estimated = usage_estimated or bool(payload.get("usage_estimated", False))
        elif event_type == EventType.QUALITY_GATE_STARTED.value:
            gate_started += 1
        elif event_type == EventType.QUALITY_GATE_PASSED.value:
            gate_passed = True
    return model_attempts, retries, usage_estimated, gate_started == 1 and gate_passed


def summarize_event_artifacts(
    output: Path, records: list[RealBenchmarkRunRecord]
) -> dict[str, object]:
    apply_patch = {"successful": 0, "failed": 0}
    run_tests = {"passed": 0, "tests_failed": 0, "tool_failed": 0}
    context_compactions = 0
    continued_runs = 0
    post_pass_model_requests = 0
    for record in records:
        events = _read_events(output / record.artifact_directory / "events.jsonl")
        first_public_pass: int | None = None
        for index, event in enumerate(events):
            event_type = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            tool = payload.get("tool")
            if event_type == EventType.CONTEXT_COMPACTED.value:
                context_compactions += 1
            if tool == "apply_patch" and event_type in {
                EventType.TOOL_COMPLETED.value,
                EventType.TOOL_FAILED.value,
            }:
                bucket = "successful" if event_type == EventType.TOOL_COMPLETED.value else "failed"
                apply_patch[bucket] += 1
            if tool == "run_tests" and event_type in {
                EventType.TOOL_COMPLETED.value,
                EventType.TOOL_FAILED.value,
            }:
                if event_type == EventType.TOOL_FAILED.value:
                    run_tests["tool_failed"] += 1
                elif payload.get("output_summary") == "tests passed":
                    run_tests["passed"] += 1
                    if first_public_pass is None:
                        first_public_pass = index
                else:
                    run_tests["tests_failed"] += 1
        if first_public_pass is not None:
            later_requests = sum(
                event.get("type") == EventType.MODEL_REQUESTED.value
                for event in events[first_public_pass + 1 :]
            )
            if later_requests:
                continued_runs += 1
                post_pass_model_requests += later_requests
    limit_names = ("max_input_tokens", "max_output_tokens", "max_steps", "max_wall_time_seconds")
    return {
        "context_compactions": context_compactions,
        "runs_continued_after_public_tests_passed": continued_runs,
        "model_requests_after_public_tests_passed": post_pass_model_requests,
        "apply_patch": apply_patch,
        "run_tests": run_tests,
        "limit_hits": {
            name: sum(record.error_code == name for record in records) for name in limit_names
        },
    }


def effective_experiment_task_spec(
    spec: TaskSpec, profile: LoadedRealExperimentProfile | None
) -> TaskSpec:
    if profile is None:
        return spec
    override = profile.profile.budget
    budget = spec.budget.model_copy(
        update={
            "max_input_tokens": override.max_input_tokens,
            "max_output_tokens": override.max_output_tokens,
            "max_steps": override.max_steps,
            "max_wall_time_seconds": override.max_wall_time_seconds,
            "max_cost_usd": override.max_cost_usd,
        }
    )
    return spec.model_copy(update={"budget": budget})


async def _run_one(
    suite: RealBenchmarkSuite,
    task: RealBenchmarkTask,
    strategy: RunStrategy,
    repetition: int,
    *,
    run_key: str,
    run_id: UUID,
    output: Path,
    work_root: Path,
    settings: AppSettings,
    base_model_config: ModelConfig,
    ledger: GlobalCostLedger,
    provider: str,
    experiment_profile: LoadedRealExperimentProfile | None,
) -> RealBenchmarkRunRecord:
    started = datetime.now(UTC)
    started_clock = time.monotonic()
    prepared = work_root / "sources" / str(run_id)
    workspace_manager = WorkspaceManager(work_root / "workspaces")
    workspace: Workspace | None = None
    baseline_commit = "unavailable"
    artifacts = ArtifactStore(output / "runs", run_id)
    events = EventEmitter(run_id, [JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG))])
    client_delegate = OpenAICompatibleClient()
    client = AuditedRealModelClient(client_delegate, ledger)
    scorecard: Scorecard | None = None
    result_code = QualityResult.SYSTEM_ERROR.value
    error_code: str | None = None
    try:
        baseline_commit = await asyncio.to_thread(_prepare_repository, task, prepared)
        effective_spec = effective_experiment_task_spec(task.spec, experiment_profile)
        rewritten_spec = effective_spec.model_copy(
            update={
                "repository": effective_spec.repository.model_copy(
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
        sandbox = _docker_sandbox(settings, workspace, task, run_id)
        await asyncio.to_thread(sandbox.check_available)
        context = ToolContext.create(
            workspace,
            rewritten_spec,
            ToolLimits(
                output_max_chars=settings.tool_output_max_chars,
                list_max_files=settings.tool_list_max_files,
                search_max_results=settings.tool_search_max_results,
                read_max_lines=settings.tool_read_max_lines,
                max_file_bytes=settings.tool_max_file_bytes,
            ),
            command_sandbox=sandbox,
        )
        gate = QualityGate(
            run_id=run_id,
            context=context,
            events=events,
            artifacts=artifacts,
            hidden_test=HiddenTestInjection(task.hidden_test, task.hidden_target),
        )
        policy = policy_for(strategy)
        model_config = base_model_config.model_copy(
            update={
                "temperature": suite.manifest.temperature,
                "max_tokens": suite.manifest.max_tokens,
            }
        )
        loop = AgentLoop(
            model_client=client,
            model_config=model_config,
            tool_context=context,
            registry=build_default_registry(context, allowed_tools=policy.allowed_tools),
            events=events,
            quality_gate=gate,
            strategy_policy=policy,
            prompt_version=suite.manifest.prompt_version,
            include_repository_snapshot=True,
        )
        loop_result = await loop.run(run_id)
        scorecard = loop_result.scorecard
        result_code = loop_result.result_code
        error_code = loop_result.error_code
        if scorecard is None:
            scorecard = await _write_infrastructure_artifacts(
                artifacts, events, task.spec.id, error_code="MISSING_SCORECARD"
            )
            result_code = QualityResult.SYSTEM_ERROR.value
            error_code = "MISSING_SCORECARD"
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        error_code = (
            exc.code
            if isinstance(exc, ModelClientError)
            else f"INFRASTRUCTURE_{type(exc).__name__.upper()}"
        )
        scorecard = await _write_infrastructure_artifacts(
            artifacts,
            events,
            task.spec.id,
            error_code=error_code,
        )
        result_code = scorecard.result.value
    finally:
        await client_delegate.close()
        if workspace is not None:
            with suppress(OSError):
                await asyncio.to_thread(workspace_manager.cleanup, workspace)
        if prepared.exists():
            with suppress(OSError):
                await asyncio.to_thread(_remove_tree, prepared)

    if scorecard is None:
        raise RuntimeError("real benchmark failed to produce a genuine Scorecard")
    event_path = artifacts.path_for(ArtifactKind.EVENT_LOG)
    event_rows = _read_events(event_path)
    model_attempts, retries, event_usage_estimated, first_gate_passed = _event_audit(event_rows)
    failures = {failure.result.value for failure in scorecard.failures}
    outcome = _outcome_class(result_code, error_code)
    return RealBenchmarkRunRecord(
        run_key=run_key,
        run_id=str(run_id),
        task_id=task.spec.id,
        strategy=strategy,
        repetition=repetition,
        language=task.language,
        difficulty=task.difficulty,
        defect=task.defect,
        outcome_class=outcome,
        result=result_code,
        error_code=error_code,
        passed=outcome == OutcomeClass.TASK_RESULT and result_code == QualityResult.PASSED.value,
        first_gate_passed=first_gate_passed,
        provider=provider,
        requested_model=base_model_config.model,
        actual_model_ids=sorted(client.actual_models),
        prompt_version=suite.manifest.prompt_version,
        temperature=suite.manifest.temperature,
        started_at=started,
        completed_at=datetime.now(UTC),
        steps=scorecard.metrics.steps,
        model_calls=scorecard.metrics.model_calls,
        model_attempts=model_attempts,
        model_retries=retries,
        tool_calls=scorecard.metrics.tool_calls,
        prompt_tokens=scorecard.metrics.prompt_tokens,
        completion_tokens=scorecard.metrics.completion_tokens,
        usage_estimated=client.usage_estimated or event_usage_estimated,
        estimated_cost_usd=scorecard.metrics.estimated_cost_usd,
        reserved_unknown_cost_usd=client.unknown_cost,
        model_latency_ms=client.total_latency_ms,
        wall_time_seconds=max(
            scorecard.metrics.wall_time_seconds, time.monotonic() - started_clock
        ),
        scope_violation=QualityResult.SCOPE_VIOLATION.value in failures,
        regression=QualityResult.REGRESSION.value in failures,
        baseline_commit=baseline_commit,
        artifact_directory=(Path("runs") / str(run_id)).as_posix(),
    )


def _version(argv: Sequence[str], cwd: Path) -> str:
    try:
        return _command(argv, cwd, timeout=30)[:500]
    except (BenchmarkError, OSError):
        return "unavailable"


def _git_identity(root: Path) -> tuple[str, bool, str]:
    commit = _version(git_argv(["rev-parse", "HEAD"]), root)
    status_result = run_argv(
        git_argv(["status", "--porcelain=v1", "--untracked-files=all"]),
        cwd=root,
        timeout_seconds=30,
        output_max_chars=1_000_000,
    )
    if status_result.return_code != 0:
        raise BenchmarkError("cannot inspect PatchPilot Git state")
    status = status_result.stdout
    return commit, bool(status), hashlib.sha256(status.encode("utf-8")).hexdigest()


def _docker_image_identity(settings: AppSettings, image: str) -> dict[str, str]:
    output = _command(
        [
            settings.sandbox_docker_binary,
            "image",
            "inspect",
            image,
            "--format",
            '{{.Id}}|{{join .RepoDigests ","}}',
        ],
        Path.cwd(),
        timeout=30,
    )
    image_id, _, digests = output.partition("|")
    return {"image": image, "image_id": image_id, "repo_digests": digests}


def collect_reproducibility(
    suite: RealBenchmarkSuite,
    settings: AppSettings,
    config: ModelConfig,
    *,
    provider: str,
    global_cost_limit: Decimal,
    selected_strategies: tuple[RunStrategy, ...],
    repetitions: int,
    concurrency: int,
    task_limit: int | None,
    experiment_profile: LoadedRealExperimentProfile | None = None,
) -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[3]
    commit, dirty, diff_hash = _git_identity(project_root)
    python_image = _docker_image_identity(settings, settings.sandbox_image_python)
    go_image = _docker_image_identity(settings, settings.sandbox_image_go)
    environment: dict[str, object] = {
        "schema_version": "1",
        "benchmark_id": suite.manifest.id,
        "suite_kind": suite.manifest.suite_kind.value,
        "manifest_sha256": suite.manifest_sha256,
        "task_set_sha256": suite.task_set_sha256,
        "task_spec_versions": sorted({task.spec.version for task in suite.tasks}),
        "prompt_version": suite.manifest.prompt_version,
        "strategies": [strategy.value for strategy in selected_strategies],
        "repetitions": repetitions,
        "concurrency": concurrency,
        "task_limit": task_limit,
        "scoring_rule": suite.manifest.scoring_rule,
        "retry_rule": suite.manifest.retry_rule,
        "patchpilot_git_commit": commit,
        "patchpilot_git_dirty": dirty,
        "patchpilot_git_status_sha256": diff_hash,
        "manifest_frozen": suite.manifest.frozen,
        "provider": provider,
        "requested_model": config.model,
        "model_configuration": {
            "temperature": suite.manifest.temperature,
            "max_tokens": suite.manifest.max_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "max_retries": config.max_retries,
            "thinking_mode": config.thinking_mode or "provider_default",
            "input_cost_per_million_usd": str(config.input_cost_per_million_usd),
            "output_cost_per_million_usd": str(config.output_cost_per_million_usd),
            "credentials_recorded": False,
            "base_url_recorded": False,
        },
        "global_cost_limit_usd": str(global_cost_limit),
        "baseline_repositories": {
            task.spec.id: {
                "tree_sha256": task.repository_sha256,
                "source_name": task.source.source_name,
                "provenance": task.source.provenance,
                "license": task.source.license,
            }
            for task in suite.tasks
        },
        "hidden_test_sha256": {task.spec.id: task.hidden_test_sha256 for task in suite.tasks},
        "docker": {
            "client_server_version": _version(
                [
                    settings.sandbox_docker_binary,
                    "version",
                    "--format",
                    "{{.Client.Version}}|{{.Server.Version}}",
                ],
                project_root,
            ),
            "python_image": python_image,
            "go_image": go_image,
            "python_runtime": _version(
                [
                    settings.sandbox_docker_binary,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    settings.sandbox_image_python,
                    "python",
                    "--version",
                ],
                project_root,
            ),
            "go_runtime": _version(
                [
                    settings.sandbox_docker_binary,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    settings.sandbox_image_go,
                    "go",
                    "version",
                ],
                project_root,
            ),
        },
        "host_environment": {
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "go": _version(["go", "version"], project_root),
            "git": _version(["git", "--version"], project_root),
        },
    }
    if experiment_profile is not None:
        profile = experiment_profile.profile
        environment.update(
            {
                "experiment_id": profile.id,
                "experiment_classification": profile.classification,
                "base_benchmark_id": profile.base_benchmark_id,
                "experiment_profile_sha256": experiment_profile.sha256,
                "budget_profile": profile.budget.model_dump(mode="json"),
                "quality_constraints_unchanged": True,
                "equal_budget_strategy_comparison": False,
            }
        )
    environment["experiment_fingerprint"] = reproducibility_fingerprint(environment)
    environment["generated_at"] = datetime.now(UTC).isoformat()
    return environment


def reproducibility_fingerprint(environment: dict[str, object]) -> str:
    material = json.dumps(environment, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_resume_experiment(
    existing: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    if existing.get("experiment_fingerprint") != current.get("experiment_fingerprint"):
        raise BenchmarkError("resume configuration does not match experiment.json")
    return existing


def _load_records(path: Path) -> list[RealBenchmarkRunRecord]:
    records: list[RealBenchmarkRunRecord] = []
    if not path.is_file():
        return records
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(RealBenchmarkRunRecord.model_validate_json(line))
        except ValueError as exc:
            raise BenchmarkError(f"invalid raw.jsonl line {line_number}: {exc}") from exc
    keys = [record.run_key for record in records]
    if len(keys) != len(set(keys)):
        raise BenchmarkError("raw.jsonl contains duplicate Run keys")
    run_ids = [record.run_id for record in records]
    if len(run_ids) != len(set(run_ids)):
        raise BenchmarkError("raw.jsonl contains duplicate Run IDs")
    return records


def _attempt_state(path: Path) -> tuple[dict[str, dict[str, object]], set[str]]:
    started: dict[str, dict[str, object]] = {}
    completed: set[str] = set()
    if not path.is_file():
        return started, completed
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"invalid attempts.jsonl line {line_number}") from exc
        if not isinstance(item, dict) or not isinstance(item.get("run_key"), str):
            raise BenchmarkError(f"invalid attempts.jsonl record at line {line_number}")
        key = str(item["run_key"])
        if item.get("state") == "started":
            started[key] = item
        elif item.get("state") == "completed":
            completed.add(key)
    return started, completed


def _persist_summary(
    suite: RealBenchmarkSuite,
    output: Path,
    records: list[RealBenchmarkRunRecord],
    reproducibility: dict[str, object],
    global_cost_limit: Decimal,
) -> RealBenchmarkSummary:
    unknown = sum((record.reserved_unknown_cost_usd for record in records), Decimal(0))
    summary = build_real_summary(
        suite,
        records,
        experiment=reproducibility,
        global_cost_limit=global_cost_limit,
        reserved_unknown_cost=unknown,
        event_metrics=summarize_event_artifacts(output, records),
    )
    _atomic_text(output / "summary.json", summary.model_dump_json(indent=2) + "\n")
    _atomic_text(output / "report.md", render_real_markdown(summary))
    _atomic_text(output / "report.html", render_real_html(summary))
    return summary


async def _interrupted_record(
    suite: RealBenchmarkSuite,
    task: RealBenchmarkTask,
    item: dict[str, object],
    *,
    output: Path,
    provider: str,
    config: ModelConfig,
) -> RealBenchmarkRunRecord:
    run_id = UUID(str(item["run_id"]))
    artifacts = ArtifactStore(output / "runs", run_id)
    events = EventEmitter(run_id, [JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG))])
    await _write_infrastructure_artifacts(
        artifacts,
        events,
        task.spec.id,
        error_code="INTERRUPTED_UNKNOWN_BILLING",
    )
    started_text = str(item.get("started_at", datetime.now(UTC).isoformat()))
    try:
        started = datetime.fromisoformat(started_text)
    except ValueError:
        started = datetime.now(UTC)
    return RealBenchmarkRunRecord(
        run_key=str(item["run_key"]),
        run_id=str(run_id),
        task_id=task.spec.id,
        strategy=RunStrategy(str(item["strategy"])),
        repetition=int(str(item["repetition"])),
        language=task.language,
        difficulty=task.difficulty,
        defect=task.defect,
        outcome_class=OutcomeClass.INTERRUPTED,
        result="INTERRUPTED",
        error_code="INTERRUPTED_UNKNOWN_BILLING",
        passed=False,
        first_gate_passed=False,
        provider=provider,
        requested_model=config.model,
        actual_model_ids=[],
        prompt_version=suite.manifest.prompt_version,
        temperature=suite.manifest.temperature,
        started_at=started,
        completed_at=datetime.now(UTC),
        steps=0,
        model_calls=0,
        model_attempts=0,
        model_retries=0,
        tool_calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        usage_estimated=True,
        estimated_cost_usd=Decimal(0),
        reserved_unknown_cost_usd=Decimal(str(item["unknown_cost_reserve_usd"])),
        model_latency_ms=0,
        wall_time_seconds=0,
        scope_violation=False,
        regression=False,
        baseline_commit="unknown-after-interruption",
        artifact_directory=(Path("runs") / str(run_id)).as_posix(),
    )


async def run_real_benchmark(
    suite_path: Path,
    output_directory: Path,
    *,
    settings: AppSettings,
    explicit_real_model: bool,
    provider: str,
    global_cost_limit: Decimal,
    strategies: tuple[RunStrategy, ...] | None = None,
    repetitions: int | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    experiment_profile_path: Path | None = None,
) -> RealBenchmarkOutput:
    config = require_real_model(settings, explicit_real_model=explicit_real_model)
    suite = load_real_benchmark(suite_path)
    experiment_profile = (
        load_real_experiment_profile(experiment_profile_path, suite)
        if experiment_profile_path is not None
        else None
    )
    if experiment_profile is None:
        selected = strategies or tuple(suite.manifest.strategies)
        repeat_count = repetitions or suite.manifest.repetitions
        parallelism = concurrency or suite.manifest.concurrency
    else:
        if any(value is not None for value in (strategies, repetitions, limit, concurrency)):
            raise BenchmarkError("experiment profile forbids matrix overrides")
        profile = experiment_profile.profile
        if global_cost_limit != profile.global_cost_limit_usd:
            raise BenchmarkError("CLI global cost limit must exactly match experiment profile")
        selected = (profile.strategy,)
        repeat_count = profile.repetitions
        parallelism = profile.concurrency
    if not provider.strip() or len(provider) > 128:
        raise BenchmarkError("provider label must contain 1 to 128 characters")
    if not selected or any(strategy not in suite.manifest.strategies for strategy in selected):
        raise BenchmarkError("selected strategies must come from the real benchmark manifest")
    if not 1 <= repeat_count <= 3:
        raise BenchmarkError("real benchmark repetitions must be between 1 and 3")
    if not 1 <= parallelism <= 4:
        raise BenchmarkError("real benchmark concurrency must be between 1 and 4")
    if suite.manifest.suite_kind == RealSuiteKind.FORMAL:
        if experiment_profile is None and (
            strategies is not None
            or repetitions is not None
            or limit is not None
            or concurrency is not None
        ):
            raise BenchmarkError(
                "formal real-v1 runs use the frozen full "
                "strategy/task/repetition/concurrency matrix"
            )
        _, dirty, _ = _git_identity(Path(__file__).resolve().parents[3])
        if dirty:
            raise BenchmarkError(
                "formal real-v1 requires a clean PatchPilot Git commit; calibration permits "
                "recorded local changes"
            )
    selected_tasks = suite.tasks[:limit] if limit is not None else suite.tasks
    if not selected_tasks:
        raise BenchmarkError("task limit selected no calibration tasks")

    output = output_directory.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    allowed_existing = {
        "experiment.json",
        "attempts.jsonl",
        "raw.jsonl",
        "summary.json",
        "report.md",
        "report.html",
        "runs",
        ".work",
    }
    unexpected = {item.name for item in output.iterdir()} - allowed_existing
    if unexpected:
        raise BenchmarkError(f"output directory contains unrelated files: {sorted(unexpected)}")
    (output / "runs").mkdir(exist_ok=True)
    work_root = output / ".work"
    if work_root.exists():
        _remove_tree(work_root)
    (work_root / "sources").mkdir(parents=True)
    (work_root / "workspaces").mkdir(parents=True)

    reproducibility = await asyncio.to_thread(
        collect_reproducibility,
        suite,
        settings,
        config,
        provider=provider,
        global_cost_limit=global_cost_limit,
        selected_strategies=selected,
        repetitions=repeat_count,
        concurrency=parallelism,
        task_limit=limit,
        experiment_profile=experiment_profile,
    )
    experiment_path = output / "experiment.json"
    if experiment_path.is_file():
        try:
            existing_experiment = json.loads(experiment_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkError("existing experiment.json is invalid") from exc
        reproducibility = validate_resume_experiment(existing_experiment, reproducibility)
    else:
        _atomic_text(
            experiment_path,
            json.dumps(reproducibility, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    raw_path = output / "raw.jsonl"
    attempts_path = output / "attempts.jsonl"
    records = _load_records(raw_path)
    records_by_key = {record.run_key: record for record in records}
    started, completed_attempts = _attempt_state(attempts_path)
    task_by_id = {task.spec.id: task for task in selected_tasks}
    resumed_runs = len(records)
    for key, item in sorted(started.items()):
        if key in records_by_key or key in completed_attempts:
            continue
        task_id = str(item.get("task_id"))
        task = task_by_id.get(task_id)
        if task is None:
            raise BenchmarkError("interrupted attempt does not belong to the selected suite")
        record = await _interrupted_record(
            suite,
            task,
            item,
            output=output,
            provider=provider,
            config=config,
        )
        _append_jsonl(raw_path, record.model_dump(mode="json"))
        _append_jsonl(
            attempts_path,
            {
                "state": "completed",
                "run_key": key,
                "completed_at": datetime.now(UTC).isoformat(),
                "outcome_class": record.outcome_class.value,
            },
        )
        records.append(record)
        records_by_key[key] = record

    consumed = sum((record.estimated_cost_usd for record in records), Decimal(0))
    unknown = sum((record.reserved_unknown_cost_usd for record in records), Decimal(0))
    ledger = GlobalCostLedger(global_cost_limit, consumed=consumed, unknown=unknown)
    experiment_fingerprint = str(reproducibility["experiment_fingerprint"])
    matrix = [
        (task, strategy, repetition)
        for strategy in selected
        for task in selected_tasks
        for repetition in range(1, repeat_count + 1)
    ]
    keyed_matrix = [
        (
            task,
            strategy,
            repetition,
            real_run_key(suite, task, strategy, repetition, experiment_fingerprint),
        )
        for task, strategy, repetition in matrix
    ]
    expected_by_key = {
        key: (task.spec.id, strategy, repetition)
        for task, strategy, repetition, key in keyed_matrix
    }
    for record in records:
        expected = expected_by_key.get(record.run_key)
        if expected is None or expected != (record.task_id, record.strategy, record.repetition):
            raise BenchmarkError("raw.jsonl contains a Run outside the fixed experiment matrix")
    pending = [item for item in keyed_matrix if item[3] not in records_by_key]
    semaphore = asyncio.Semaphore(parallelism)
    persistence_lock = asyncio.Lock()

    async def execute_pending(
        task: RealBenchmarkTask,
        strategy: RunStrategy,
        repetition: int,
        key: str,
    ) -> RealBenchmarkRunRecord:
        async with semaphore:
            run_cost_limit = effective_experiment_task_spec(
                task.spec, experiment_profile
            ).budget.max_cost_usd
            if not await ledger.can_start(run_cost_limit):
                raise RealBenchmarkStopped(
                    "global cost limit cannot reserve the next fixed-matrix Run"
                )
            run_id = uuid4()
            async with persistence_lock:
                _append_jsonl(
                    attempts_path,
                    {
                        "state": "started",
                        "run_key": key,
                        "run_id": str(run_id),
                        "task_id": task.spec.id,
                        "strategy": strategy.value,
                        "repetition": repetition,
                        "started_at": datetime.now(UTC).isoformat(),
                        "unknown_cost_reserve_usd": str(run_cost_limit),
                    },
                )
            return await _run_one(
                suite,
                task,
                strategy,
                repetition,
                run_key=key,
                run_id=run_id,
                output=output,
                work_root=work_root,
                settings=settings,
                base_model_config=config,
                ledger=ledger,
                provider=provider,
                experiment_profile=experiment_profile,
            )

    jobs = [
        asyncio.create_task(execute_pending(task, strategy, repetition, key))
        for task, strategy, repetition, key in pending
    ]
    stopped: RealBenchmarkStopped | None = None
    try:
        for future in asyncio.as_completed(jobs):
            try:
                record = await future
            except RealBenchmarkStopped as exc:
                stopped = exc
                break
            async with persistence_lock:
                _append_jsonl(raw_path, record.model_dump(mode="json"))
                _append_jsonl(
                    attempts_path,
                    {
                        "state": "completed",
                        "run_key": record.run_key,
                        "completed_at": record.completed_at.isoformat(),
                        "outcome_class": record.outcome_class.value,
                    },
                )
                records.append(record)
                records_by_key[record.run_key] = record
                _persist_summary(suite, output, records, reproducibility, global_cost_limit)
        if stopped is not None:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
    except BaseException:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    finally:
        if work_root.exists():
            await asyncio.to_thread(_remove_tree, work_root)

    records.sort(key=lambda item: (item.strategy.value, item.task_id, item.repetition))
    summary = _persist_summary(suite, output, records, reproducibility, global_cost_limit)
    if stopped is not None:
        raise RealBenchmarkStopped(f"{stopped}; progress and reports are persisted in {output}")
    return RealBenchmarkOutput(output, summary, tuple(records), resumed_runs)


async def ping_real_model(
    settings: AppSettings,
    *,
    explicit_real_model: bool,
    provider: str,
    global_cost_limit: Decimal,
) -> PingResult:
    config = require_real_model(settings, explicit_real_model=explicit_real_model).model_copy(
        update={"max_tokens": min(settings.model_max_tokens, 512)}
    )
    ledger = GlobalCostLedger(global_cost_limit)
    delegate = OpenAICompatibleClient()
    client = AuditedRealModelClient(delegate, ledger)
    schema = ToolSchema(
        name="benchmark_ping",
        description="Return the exact supplied nonce to verify structured tool-call support.",
        parameters={
            "type": "object",
            "properties": {"nonce": {"type": "string"}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
    )
    nonce = uuid4().hex
    try:
        response = await client.complete(
            [
                Message(
                    role=MessageRole.SYSTEM,
                    content="Call benchmark_ping exactly once. Do not return credentials or prose.",
                ),
                Message(
                    role=MessageRole.USER,
                    content=f"Call benchmark_ping with nonce {nonce}.",
                ),
            ],
            [schema],
            config,
        )
    finally:
        await delegate.close()
    if len(response.tool_calls) != 1 or response.tool_calls[0].name != "benchmark_ping":
        raise BenchmarkError("real model did not return the required structured tool_call")
    arguments = response.tool_calls[0].arguments
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("real model returned invalid tool_call JSON") from exc
    if not isinstance(arguments, dict) or arguments.get("nonce") != nonce:
        raise BenchmarkError("real model tool_call did not preserve the compatibility nonce")
    return PingResult(
        provider=provider,
        requested_model=config.model,
        actual_model=response.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        estimated_cost_usd=config.cost_for(response.usage),
        usage_estimated=response.usage.estimated,
        latency_ms=response.latency_ms,
    )


def _run_fixture_commands(
    task: RealBenchmarkTask,
    workspace: Workspace,
    sandbox: DockerSandbox,
) -> tuple[bool, list[int]]:
    destination = (workspace.path / task.hidden_target).resolve(strict=False)
    root = workspace.path.resolve(strict=True)
    if not destination.is_relative_to(root) or destination.exists():
        raise BenchmarkError("hidden test injection target is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(task.hidden_test, destination)
    profile = profile_for(task.spec)
    exit_codes: list[int] = []
    try:
        for command_id in profile.command_ids:
            resolved = profile.resolve(command_id)
            result = sandbox.run(
                resolved.argv,
                cwd=workspace.path,
                timeout_seconds=resolved.timeout_seconds,
                output_max_chars=50_000,
                environment=profile.environment,
            )
            exit_codes.append(result.return_code)
    finally:
        if destination.exists():
            destination.unlink()
    return bool(exit_codes) and all(code == 0 for code in exit_codes), exit_codes


async def verify_real_fixtures(
    suite_path: Path,
    *,
    settings: AppSettings,
) -> list[dict[str, object]]:
    """Prove every baseline fails and its human reference patch passes in Docker."""

    suite = load_real_benchmark(suite_path)
    audit_root = settings.workspace_root.expanduser().resolve() / ".real-fixture-audit"
    if audit_root.exists():
        _remove_tree(audit_root)
    (audit_root / "sources").mkdir(parents=True)
    (audit_root / "workspaces").mkdir(parents=True)
    manager = WorkspaceManager(audit_root / "workspaces")
    results: list[dict[str, object]] = []
    try:
        for task in suite.tasks:
            run_id = uuid4()
            prepared = audit_root / "sources" / str(run_id)
            workspace: Workspace | None = None
            before_hash = task.repository_sha256
            try:
                baseline_commit = await asyncio.to_thread(_prepare_repository, task, prepared)
                workspace = await asyncio.to_thread(
                    manager.create,
                    prepared,
                    workspace_id=str(run_id),
                    base_ref="main",
                )
                sandbox = _docker_sandbox(settings, workspace, task, run_id)
                await asyncio.to_thread(sandbox.check_available)
                baseline_passed, baseline_codes = await asyncio.to_thread(
                    _run_fixture_commands, task, workspace, sandbox
                )
                if baseline_passed:
                    raise BenchmarkError(
                        f"Task {task.spec.id} baseline unexpectedly passes public+hidden tests"
                    )
                apply_result = run_argv(
                    git_argv(
                        [
                            "apply",
                            "--recount",
                            "--whitespace=nowarn",
                            "--ignore-space-change",
                            "--",
                            str(task.reference_patch),
                        ]
                    ),
                    cwd=workspace.path,
                    timeout_seconds=30,
                    output_max_chars=20_000,
                )
                if apply_result.return_code != 0:
                    raise BenchmarkError(
                        f"Task {task.spec.id} reference patch cannot be applied: "
                        f"{apply_result.stderr.strip()[:500]}"
                    )
                diff_names = _command(
                    git_argv(["diff", "--name-only", "--no-ext-diff"]),
                    workspace.path,
                ).splitlines()
                if len(diff_names) != task.source.changed_files_expected:
                    raise BenchmarkError(
                        f"Task {task.spec.id} reference patch changed {len(diff_names)} files; "
                        f"expected {task.source.changed_files_expected}"
                    )
                rewritten_spec = task.spec.model_copy(
                    update={
                        "repository": task.spec.repository.model_copy(
                            update={"path": str(prepared), "base_ref": "main"}
                        )
                    }
                )
                context = ToolContext.create(
                    workspace,
                    rewritten_spec,
                    command_sandbox=sandbox,
                )
                for path in diff_names:
                    context.path_policy.resolve(
                        path,
                        for_write=True,
                        must_exist=False,
                        reject_symlinks=True,
                    )
                reference_passed, reference_codes = await asyncio.to_thread(
                    _run_fixture_commands, task, workspace, sandbox
                )
                if not reference_passed:
                    raise BenchmarkError(
                        f"Task {task.spec.id} reference patch fails public+hidden tests"
                    )
                from patchpilot.benchmark.real_models import sha256_tree

                if sha256_tree(task.repository_template) != before_hash:
                    raise BenchmarkError(f"Task {task.spec.id} modified its source repository")
                results.append(
                    {
                        "task_id": task.spec.id,
                        "baseline_commit": baseline_commit,
                        "repository_sha256": before_hash,
                        "hidden_test_sha256": task.hidden_test_sha256,
                        "baseline_exit_codes": baseline_codes,
                        "reference_exit_codes": reference_codes,
                        "reference_changed_files": diff_names,
                    }
                )
            finally:
                if workspace is not None:
                    with suppress(OSError):
                        await asyncio.to_thread(manager.cleanup, workspace)
                if prepared.exists():
                    with suppress(OSError):
                        await asyncio.to_thread(_remove_tree, prepared)
    finally:
        if audit_root.exists():
            await asyncio.to_thread(_remove_tree, audit_root)
    return results


def estimate_full_matrix(
    suite: RealBenchmarkSuite,
    *,
    input_cost_per_million: Decimal,
    output_cost_per_million: Decimal,
    observed_records: Sequence[RealBenchmarkRunRecord],
) -> dict[str, object]:
    runs = len(suite.tasks) * len(suite.manifest.strategies) * 3
    per_run_hard = max(task.spec.budget.max_cost_usd for task in suite.tasks)
    worst = sum(
        task.spec.budget.max_cost_usd * len(suite.manifest.strategies) * 3 for task in suite.tasks
    )
    if observed_records:
        average_prompt = Decimal(
            sum(record.prompt_tokens for record in observed_records)
        ) / Decimal(len(observed_records))
        average_completion = Decimal(
            sum(record.completion_tokens for record in observed_records)
        ) / Decimal(len(observed_records))
        average_wall = sum(record.wall_time_seconds for record in observed_records) / len(
            observed_records
        )
        expected_cost = (
            (average_prompt * input_cost_per_million + average_completion * output_cost_per_million)
            / Decimal(1_000_000)
            * runs
        )
        expected_seconds = average_wall * runs
    else:
        average_prompt = Decimal(0)
        average_completion = Decimal(0)
        average_wall = 0.0
        expected_cost = Decimal(0)
        expected_seconds = 0.0
    return {
        "tasks": len(suite.tasks),
        "strategies": len(suite.manifest.strategies),
        "repetitions": 3,
        "runs": runs,
        "observed_calibration_runs": len(observed_records),
        "observed_average_prompt_tokens": str(average_prompt),
        "observed_average_completion_tokens": str(average_completion),
        "observed_average_wall_seconds": average_wall,
        "expected_usage_cost_usd": str(expected_cost),
        "worst_case_task_budget_cost_usd": str(worst),
        "maximum_single_run_budget_usd": str(per_run_hard),
        "expected_serial_wall_seconds": expected_seconds,
        "rate_limit_risk": ("unknown without provider quota; concurrency=1 minimizes burst rate"),
    }
