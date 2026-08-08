from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.agent.events import EventEmitter, EventType
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.domain.run import RunStatus
from patchpilot.domain.scorecard import (
    GateChecks,
    GateFailure,
    GateOutcome,
    QualityResult,
    Scorecard,
    ScorecardArtifacts,
    ScorecardMetrics,
)
from patchpilot.persistence.repositories import RunRepository
from patchpilot.profiles import profile_for
from patchpilot.reporting.render import render_html, render_markdown
from patchpilot.sandbox.base import SandboxError
from patchpilot.sandbox.workspace import PathPolicyError, normalize_logical_path
from patchpilot.tools.base import ToolContext
from patchpilot.tools.git import DiffSnapshot, collect_diff


class QualityGateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    patch_max_chars: int = Field(default=2_000_000, gt=0)
    test_log_max_chars: int = Field(default=200_000, gt=0)
    feedback_max_chars: int = Field(default=4_000, gt=0, le=4_000)


@dataclass(frozen=True, slots=True)
class HiddenTestInjection:
    """A gate-only test file that is never present in the Agent-visible Workspace."""

    source: Path
    target: str

    def validated(self) -> HiddenTestInjection:
        source = self.source.expanduser().resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise ValueError("hidden test source must be a regular file")
        if source.stat().st_size > 1_000_000:
            raise ValueError("hidden test source exceeds the 1 MB limit")
        return HiddenTestInjection(source=source, target=normalize_logical_path(self.target))


@dataclass(frozen=True, slots=True)
class _TestRun:
    command_id: str
    exit_code: int | None
    timed_out: bool
    budget_limited: bool
    truncated: bool
    duration_ms: int
    stdout: str
    stderr: str
    executed_tests: tuple[str, ...]
    failed_tests: tuple[str, ...]
    skipped_tests: tuple[str, ...]
    system_error: str | None = None


@dataclass(frozen=True, slots=True)
class _TestSuite:
    runs: tuple[_TestRun, ...]
    tests_passed: bool
    required_tests_ran: bool
    failed_tests: tuple[str, ...]
    regression: bool
    timed_out: bool
    budget_exceeded: bool
    sandbox_error: bool
    error_excerpt: str
    log: str


def classify_gate(
    checks: GateChecks,
    *,
    timed_out: bool = False,
    sandbox_error: bool = False,
    regression: bool = False,
) -> QualityResult:
    """Apply the stable M3 result precedence without model input."""

    if not checks.budget_valid:
        return QualityResult.BUDGET_EXCEEDED
    if not checks.has_patch:
        return QualityResult.NO_PATCH
    if not checks.scope_valid:
        return QualityResult.SCOPE_VIOLATION
    if not checks.patch_size_valid:
        return QualityResult.PATCH_TOO_LARGE
    if sandbox_error:
        return QualityResult.SANDBOX_ERROR
    if timed_out:
        return QualityResult.TIMEOUT
    if not checks.tests_passed:
        return QualityResult.REGRESSION if regression else QualityResult.TEST_FAILURE
    if not checks.required_tests_ran:
        return QualityResult.REQUIRED_TEST_NOT_RUN
    return QualityResult.PASSED


class QualityGate:
    """Deterministically inspect a Workspace, run acceptance, and write M3 artifacts."""

    _RECOVERABLE = frozenset(
        {
            QualityResult.NO_PATCH,
            QualityResult.TEST_FAILURE,
            QualityResult.REGRESSION,
            QualityResult.REQUIRED_TEST_NOT_RUN,
        }
    )

    def __init__(
        self,
        *,
        run_id: UUID,
        context: ToolContext,
        events: EventEmitter,
        artifacts: ArtifactStore,
        limits: QualityGateLimits | None = None,
        hidden_test: HiddenTestInjection | None = None,
        acceptance_environment: Mapping[str, str] | None = None,
    ) -> None:
        if run_id != events.run_id or run_id != artifacts.run_id:
            raise ValueError("QualityGate Run IDs must match")
        event_path = artifacts.path_for(ArtifactKind.EVENT_LOG)
        if not events.writes_jsonl_to(event_path):
            raise ValueError("QualityGate requires events.jsonl as an EventEmitter sink")
        self.run_id = run_id
        self.context = context
        self.events = events
        self.artifacts = artifacts
        self.limits = limits or QualityGateLimits()
        self.hidden_test = hidden_test.validated() if hidden_test is not None else None
        self.acceptance_environment = dict(acceptance_environment or {})
        self._failure_history: list[GateFailure] = []

    async def evaluate(
        self,
        metrics: ScorecardMetrics,
        *,
        attempt: int,
        remaining_wall_time_seconds: float,
    ) -> GateOutcome:
        started = time.monotonic()
        await self.events.emit(EventType.QUALITY_GATE_STARTED, {"attempt": attempt})
        snapshot: DiffSnapshot | None = None
        test_suite = self._empty_test_suite("acceptance was not run")
        sandbox_error = False
        sandbox_message = ""
        try:
            snapshot = collect_diff(
                self.context,
                output_max_chars=self.limits.patch_max_chars,
            )
        except OSError as exc:
            sandbox_error = True
            sandbox_message = f"failed to inspect Workspace: {type(exc).__name__}"

        if snapshot is None:
            snapshot = DiffSnapshot(
                diff="",
                changed_paths=(),
                binary_files=(),
                changed_files=0,
                added_lines=0,
                deleted_lines=0,
                truncated=False,
            )

        has_patch = snapshot.changed_files > 0
        scope_valid = has_patch and not snapshot.binary_files and self._scope_is_valid(snapshot)
        patch_lines = snapshot.added_lines + snapshot.deleted_lines
        patch_size_valid = (
            has_patch
            and not snapshot.truncated
            and snapshot.changed_files <= self.context.task_spec.budget.max_changed_files
            and patch_lines <= self.context.task_spec.budget.max_patch_lines
        )
        initial_budget_valid = self._budget_is_valid(metrics, extra_wall_time=0)

        if (
            has_patch
            and scope_valid
            and patch_size_valid
            and initial_budget_valid
            and not sandbox_error
        ):
            test_suite = await asyncio.to_thread(
                self._run_acceptance,
                remaining_wall_time_seconds,
            )

        elapsed = time.monotonic() - started
        final_metrics = metrics.model_copy(
            update={
                "changed_files": snapshot.changed_files,
                "added_lines": snapshot.added_lines,
                "deleted_lines": snapshot.deleted_lines,
                "wall_time_seconds": metrics.wall_time_seconds + elapsed,
            }
        )
        budget_valid = self._budget_is_valid(final_metrics, extra_wall_time=0)
        if test_suite.budget_exceeded:
            budget_valid = False
        checks = GateChecks(
            has_patch=has_patch,
            scope_valid=scope_valid,
            patch_size_valid=patch_size_valid,
            tests_passed=test_suite.tests_passed,
            required_tests_ran=test_suite.required_tests_ran,
            budget_valid=budget_valid,
        )
        result = classify_gate(
            checks,
            timed_out=test_suite.timed_out,
            sandbox_error=sandbox_error or test_suite.sandbox_error,
            regression=test_suite.regression,
        )
        if self.context.cancellation_token.is_cancelled:
            result = QualityResult.CANCELLED
        failure: GateFailure | None = None
        if result != QualityResult.PASSED:
            failure = GateFailure(
                result=result,
                summary=self._failure_summary(result, snapshot, test_suite, sandbox_message),
                failed_tests=list(test_suite.failed_tests),
                error_excerpt=test_suite.error_excerpt[: self.limits.feedback_max_chars],
            )
            self._failure_history.append(failure)

        scorecard = await self._write_outputs(
            result=result,
            checks=checks,
            metrics=final_metrics,
            patch=snapshot.diff,
            test_log=test_suite.log,
        )
        event_type = (
            EventType.QUALITY_GATE_PASSED
            if result == QualityResult.PASSED
            else EventType.QUALITY_GATE_FAILED
        )
        await self.events.emit(
            event_type,
            {
                "attempt": attempt,
                "result": result.value,
                "changed_files": snapshot.changed_files,
                "added_lines": snapshot.added_lines,
                "deleted_lines": snapshot.deleted_lines,
                "failed_tests": list(test_suite.failed_tests)[:50],
            },
            duration_ms=round(elapsed * 1_000),
        )
        recoverable = result in self._RECOVERABLE and budget_valid
        feedback = None
        if failure is not None and recoverable:
            feedback = {
                "type": "quality_gate_feedback",
                "result": result.value,
                "summary": failure.summary,
                "failed_tests": failure.failed_tests,
                "error_excerpt": failure.error_excerpt,
                "diff_summary": {
                    "changed_files": snapshot.changed_files,
                    "added_lines": snapshot.added_lines,
                    "deleted_lines": snapshot.deleted_lines,
                },
                "remaining_budget": {
                    "wall_time_seconds": max(
                        0.0,
                        self.context.task_spec.budget.max_wall_time_seconds
                        - final_metrics.wall_time_seconds,
                    ),
                    "steps": max(
                        0,
                        self.context.task_spec.budget.max_steps - final_metrics.steps,
                    ),
                },
            }
        return GateOutcome(
            result=result,
            scorecard=scorecard,
            recoverable=recoverable,
            feedback=feedback,
        )

    async def finalize_terminal(
        self,
        result: QualityResult,
        metrics: ScorecardMetrics,
        *,
        summary: str,
    ) -> Scorecard:
        try:
            snapshot = collect_diff(
                self.context,
                output_max_chars=self.limits.patch_max_chars,
            )
        except OSError:
            snapshot = DiffSnapshot("", (), (), 0, 0, 0, False)
        failure = GateFailure(result=result, summary=summary)
        self._failure_history.append(failure)
        final_metrics = metrics.model_copy(
            update={
                "changed_files": snapshot.changed_files,
                "added_lines": snapshot.added_lines,
                "deleted_lines": snapshot.deleted_lines,
            }
        )
        checks = GateChecks(
            has_patch=snapshot.changed_files > 0,
            scope_valid=False,
            patch_size_valid=False,
            tests_passed=False,
            required_tests_ran=False,
            budget_valid=result != QualityResult.BUDGET_EXCEEDED,
        )
        return await self._write_outputs(
            result=result,
            checks=checks,
            metrics=final_metrics,
            patch=snapshot.diff,
            test_log=f"Acceptance not run: {summary}\n",
        )

    async def finalize_event_log(self) -> None:
        path = self.artifacts.path_for(ArtifactKind.EVENT_LOG)
        if not path.is_file():
            raise RuntimeError("events.jsonl was not created")
        await self.artifacts.record_existing(ArtifactKind.EVENT_LOG)

    async def finalize_run(
        self,
        status: RunStatus,
        scorecard: Scorecard,
        *,
        error_code: str | None = None,
    ) -> None:
        if self.artifacts.database is None:
            return
        async with self.artifacts.database.session() as session:
            await RunRepository(session).finalize(
                self.run_id,
                status=status,
                workspace_id=self.context.workspace.id,
                metrics=scorecard.metrics,
                result_code=scorecard.result.value,
                error_code=error_code,
            )

    def _run_acceptance(self, remaining_wall_time_seconds: float) -> _TestSuite:
        profile = profile_for(self.context.task_spec)
        runs: list[_TestRun] = []
        gate_started = time.monotonic()
        hidden_path = self._inject_hidden_test()
        go_cache: Path | None = None
        environment = dict(profile.environment)
        environment.update(self.acceptance_environment)
        if self.context.task_spec.repository.language == "go":
            if "GOCACHE" not in environment:
                go_cache = self.context.workspace.path / ".patchpilot-go-cache"
                environment["GOCACHE"] = str(go_cache)
        try:
            for index, command_id in enumerate(profile.command_ids):
                remaining = remaining_wall_time_seconds - (time.monotonic() - gate_started)
                if remaining <= 0:
                    runs.append(
                        _TestRun(
                            command_id=command_id,
                            exit_code=None,
                            timed_out=False,
                            budget_limited=True,
                            truncated=False,
                            duration_ms=0,
                            stdout="",
                            stderr="",
                            executed_tests=(),
                            failed_tests=(),
                            skipped_tests=(),
                            system_error="Quality Gate wall-time budget exhausted",
                        )
                    )
                    break
                resolved = profile.resolve(command_id)
                report_path: Path | None = None
                argv = list(resolved.argv)
                if self.context.task_spec.repository.language == "python":
                    report_name = f".patchpilot-junit-{index}-{uuid4().hex}.xml"
                    report_path = self.context.workspace.path / report_name
                    argv.extend([f"--junitxml={report_name}", "-o", "junit_family=xunit2"])
                timeout = min(float(resolved.timeout_seconds), remaining)
                budget_limited = timeout < resolved.timeout_seconds
                try:
                    command_result = self.context.command_sandbox.run(
                        argv,
                        cwd=self.context.workspace.path,
                        timeout_seconds=max(0.001, timeout),
                        output_max_chars=self.context.limits.output_max_chars,
                        environment=environment,
                        cancel_event=self.context.cancellation_token.event,
                    )
                    if report_path is None:
                        executed, failed, skipped = self._parse_go_test_output(
                            f"{command_result.stdout}\n{command_result.stderr}"
                        )
                    else:
                        executed, failed, skipped = self._parse_junit(report_path)
                    runs.append(
                        _TestRun(
                            command_id=command_id,
                            exit_code=command_result.return_code,
                            timed_out=command_result.timed_out,
                            budget_limited=budget_limited and command_result.timed_out,
                            truncated=command_result.truncated,
                            duration_ms=command_result.duration_ms,
                            stdout=command_result.stdout,
                            stderr=command_result.stderr,
                            executed_tests=executed,
                            failed_tests=failed,
                            skipped_tests=skipped,
                        )
                    )
                    if command_result.cancelled:
                        break
                except (OSError, SandboxError) as exc:
                    runs.append(
                        _TestRun(
                            command_id=command_id,
                            exit_code=None,
                            timed_out=False,
                            budget_limited=False,
                            truncated=False,
                            duration_ms=0,
                            stdout="",
                            stderr="",
                            executed_tests=(),
                            failed_tests=(),
                            skipped_tests=(),
                            system_error=f"sandbox command failed: {type(exc).__name__}",
                        )
                    )
                    break
                finally:
                    if report_path is not None and report_path.exists():
                        report_path.unlink()
        finally:
            if hidden_path is not None and hidden_path.exists():
                hidden_path.unlink()
            if go_cache is not None and go_cache.exists():
                shutil.rmtree(go_cache)
        return self._summarize_tests(runs)

    def _inject_hidden_test(self) -> Path | None:
        if self.hidden_test is None:
            return None
        destination = (self.context.workspace.path / self.hidden_test.target).resolve(strict=False)
        root = self.context.workspace.path.resolve(strict=True)
        if not destination.is_relative_to(root) or destination.exists():
            raise OSError("hidden test destination is unsafe or already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.hidden_test.source, destination)
        return destination

    def _summarize_tests(self, runs: list[_TestRun]) -> _TestSuite:
        executed = {name for run in runs for name in run.executed_tests}
        skipped = {name for run in runs for name in run.skipped_tests}
        failed = tuple(sorted({name for run in runs for name in run.failed_tests}))
        required = self.context.task_spec.acceptance.required_tests
        required_ran = all(self._test_name_matches(name, executed - skipped) for name in required)
        failed_required = any(self._test_name_matches(name, set(failed)) for name in required)
        timed_out = any(run.timed_out and not run.budget_limited for run in runs)
        budget_exceeded = any(run.budget_limited for run in runs)
        sandbox_error = any(run.system_error is not None for run in runs)
        tests_passed = bool(runs) and all(
            run.exit_code == 0 and not run.timed_out and run.system_error is None for run in runs
        )
        regression = bool(failed) and required_ran and not failed_required
        excerpts = [
            text
            for run in runs
            for text in (run.system_error or "", run.stderr, run.stdout)
            if text
        ]
        error_excerpt = "\n".join(excerpts)[: self.limits.feedback_max_chars]
        if self.hidden_test is not None and failed:
            error_excerpt = "Hidden acceptance test failed; source and assertions are redacted."
        log = self._render_test_log(runs)
        return _TestSuite(
            runs=tuple(runs),
            tests_passed=tests_passed,
            required_tests_ran=required_ran,
            failed_tests=failed,
            regression=regression,
            timed_out=timed_out,
            budget_exceeded=budget_exceeded,
            sandbox_error=sandbox_error,
            error_excerpt=error_excerpt,
            log=log,
        )

    @staticmethod
    def _parse_junit(path: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        if not path.is_file():
            return (), (), ()
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            return (), (), ()
        executed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        for testcase in root.iter("testcase"):
            name = testcase.attrib.get("name", "").strip()
            classname = testcase.attrib.get("classname", "").strip()
            names = {name} if name else set()
            if name and classname:
                names.add(f"{classname}::{name}")
            executed.update(names)
            if testcase.find("failure") is not None or testcase.find("error") is not None:
                failed.update(names)
            if testcase.find("skipped") is not None:
                skipped.update(names)
        return tuple(sorted(executed)), tuple(sorted(failed)), tuple(sorted(skipped))

    @staticmethod
    def _parse_go_test_output(
        output: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        executed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        for line in output.splitlines():
            run_match = re.match(r"^=== RUN\s+([^\s]+)$", line.strip())
            if run_match is not None:
                name = run_match.group(1)
                executed.update({name, name.split("/", 1)[0]})
                continue
            result_match = re.match(r"^--- (PASS|FAIL|SKIP):\s+([^\s]+)", line.strip())
            if result_match is None:
                continue
            outcome, name = result_match.groups()
            names = {name, name.split("/", 1)[0]}
            executed.update(names)
            if outcome == "FAIL":
                failed.update(names)
            elif outcome == "SKIP":
                skipped.update(names)
        return tuple(sorted(executed)), tuple(sorted(failed)), tuple(sorted(skipped))

    @staticmethod
    def _test_name_matches(required: str, actual: set[str]) -> bool:
        return any(name == required or name.endswith(f"::{required}") for name in actual)

    def _render_test_log(self, runs: list[_TestRun]) -> str:
        sections: list[str] = []
        for run in runs:
            sections.append(
                "\n".join(
                    [
                        f"command_id: {run.command_id}",
                        f"exit_code: {run.exit_code}",
                        f"timed_out: {str(run.timed_out).lower()}",
                        f"duration_ms: {run.duration_ms}",
                        f"truncated: {str(run.truncated).lower()}",
                        f"executed_tests: {json.dumps(run.executed_tests)}",
                        f"failed_tests: {json.dumps(run.failed_tests)}",
                        f"system_error: {run.system_error or ''}",
                        "stdout:",
                        run.stdout,
                        "stderr:",
                        run.stderr,
                    ]
                )
            )
        rendered = "\n\n".join(sections) or "No acceptance commands were run.\n"
        if len(rendered) > self.limits.test_log_max_chars:
            suffix = "\n[PatchPilot test log truncated]\n"
            return rendered[: self.limits.test_log_max_chars - len(suffix)] + suffix
        return rendered

    @staticmethod
    def _empty_test_suite(message: str) -> _TestSuite:
        return _TestSuite((), False, False, (), False, False, False, False, "", message + "\n")

    def _scope_is_valid(self, snapshot: DiffSnapshot) -> bool:
        for logical in snapshot.changed_paths:
            try:
                self.context.path_policy.resolve(
                    logical,
                    for_write=True,
                    must_exist=False,
                    reject_symlinks=True,
                )
            except PathPolicyError:
                return False
        return True

    def _budget_is_valid(
        self,
        metrics: ScorecardMetrics,
        *,
        extra_wall_time: float,
    ) -> bool:
        budget = self.context.task_spec.budget
        return (
            metrics.steps <= budget.max_steps
            and metrics.prompt_tokens <= budget.max_input_tokens
            and metrics.completion_tokens <= budget.max_output_tokens
            and metrics.estimated_cost_usd <= budget.max_cost_usd
            and metrics.wall_time_seconds + extra_wall_time <= budget.max_wall_time_seconds
        )

    async def _write_outputs(
        self,
        *,
        result: QualityResult,
        checks: GateChecks,
        metrics: ScorecardMetrics,
        patch: str,
        test_log: str,
    ) -> Scorecard:
        await self.artifacts.write_text(ArtifactKind.PATCH, patch)
        await self.artifacts.write_text(ArtifactKind.TEST_LOG, test_log)
        artifact_refs = ScorecardArtifacts(
            patch=self.artifacts.relative_path_for(ArtifactKind.PATCH),
            events=self.artifacts.relative_path_for(ArtifactKind.EVENT_LOG),
            test_log=self.artifacts.relative_path_for(ArtifactKind.TEST_LOG),
            scorecard=self.artifacts.relative_path_for(ArtifactKind.SCORECARD),
            report_markdown=self.artifacts.relative_path_for(ArtifactKind.REPORT_MARKDOWN),
            report_html=self.artifacts.relative_path_for(ArtifactKind.REPORT_HTML),
        )
        scorecard = Scorecard(
            run_id=self.run_id,
            task_id=self.context.task_spec.id,
            result=result,
            checks=checks,
            metrics=metrics,
            failures=list(self._failure_history),
            artifacts=artifact_refs,
        )
        await self.artifacts.write_text(ArtifactKind.REPORT_MARKDOWN, render_markdown(scorecard))
        event_path = self.artifacts.path_for(ArtifactKind.EVENT_LOG)
        events_jsonl = event_path.read_text(encoding="utf-8") if event_path.is_file() else ""
        await self.artifacts.write_text(
            ArtifactKind.REPORT_HTML,
            render_html(
                scorecard,
                patch=patch,
                test_log=test_log,
                events_jsonl=events_jsonl,
            ),
        )
        await self.artifacts.write_text(
            ArtifactKind.SCORECARD,
            scorecard.model_dump_json(indent=2) + "\n",
        )
        return scorecard

    @staticmethod
    def _failure_summary(
        result: QualityResult,
        snapshot: DiffSnapshot,
        suite: _TestSuite,
        sandbox_message: str,
    ) -> str:
        summaries = {
            QualityResult.NO_PATCH: "Workspace contains no patch",
            QualityResult.SCOPE_VIOLATION: "Workspace changes violate path or file-type policy",
            QualityResult.PATCH_TOO_LARGE: "Patch exceeds file, line, or artifact limits",
            QualityResult.TEST_FAILURE: "Acceptance tests failed",
            QualityResult.REGRESSION: "Non-required acceptance tests regressed",
            QualityResult.REQUIRED_TEST_NOT_RUN: "One or more required tests did not run",
            QualityResult.TIMEOUT: "Acceptance command timed out",
            QualityResult.BUDGET_EXCEEDED: "A hard execution budget was exhausted",
            QualityResult.SANDBOX_ERROR: sandbox_message or "Acceptance execution failed",
        }
        detail = summaries.get(result, result.value)
        if suite.failed_tests:
            detail += f": {', '.join(suite.failed_tests[:20])}"
        if result == QualityResult.PATCH_TOO_LARGE:
            detail += (
                f" ({snapshot.changed_files} files, "
                f"{snapshot.added_lines + snapshot.deleted_lines} lines)"
            )
        return detail[:2_000]
