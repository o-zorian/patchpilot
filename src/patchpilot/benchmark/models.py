from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchpilot.domain.run import RunStrategy
from patchpilot.domain.task import TaskSpec

MANDATORY_STRATEGIES = (
    RunStrategy.SINGLE_SHOT,
    RunStrategy.AGENT_LOOP,
    RunStrategy.AGENT_LOOP_WITH_TESTS,
    RunStrategy.FULL,
)
_BENCHMARK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class BenchmarkError(ValueError):
    """Raised when a benchmark suite is incomplete or unsafe."""


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]
    id: str = Field(min_length=1, max_length=128)
    task_glob: str = "tasks/*.yaml"
    strategies: list[RunStrategy]
    model: str = Field(min_length=1, max_length=255)
    prompt_version: str = Field(min_length=1, max_length=64)
    repetitions: int = Field(default=1, gt=0, le=10)
    concurrency: int = Field(default=1, gt=0, le=16)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=4096, gt=0)

    @model_validator(mode="after")
    def validate_manifest(self) -> BenchmarkManifest:
        if not _BENCHMARK_ID.fullmatch(self.id):
            raise ValueError("benchmark id contains unsupported characters")
        if tuple(self.strategies) != MANDATORY_STRATEGIES:
            raise ValueError("strategies must list the four mandatory strategies in order")
        if self.task_glob != "tasks/*.yaml":
            raise ValueError("task_glob must be tasks/*.yaml")
        return self


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    spec: TaskSpec
    task_path: Path
    repository_template: Path
    hidden_test: Path
    hidden_target: str
    scripted_patch: Path
    ordinal: int

    @property
    def language(self) -> str:
        return self.spec.repository.language

    @property
    def difficulty(self) -> str:
        return self.spec.metadata.difficulty or "unknown"

    @property
    def defect(self) -> str:
        for tag in self.spec.metadata.tags:
            if tag.startswith("defect:"):
                return tag.removeprefix("defect:")
        return "unknown"


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    root: Path
    manifest: BenchmarkManifest
    tasks: tuple[BenchmarkTask, ...]
    task_set_sha256: str


class BenchmarkRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    run_id: str
    task_id: str
    strategy: RunStrategy
    repetition: int = Field(gt=0)
    language: Literal["python", "go"]
    difficulty: Literal["easy", "medium", "hard"]
    defect: str
    result: str
    passed: bool
    first_gate_passed: bool
    steps: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    wall_time_seconds: float = Field(ge=0)
    scope_violation: bool
    regression: bool
    artifact_directory: str


class MetricGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: int
    passed: int
    pass_rate: float
    first_gate_pass_rate: float
    average_steps: float
    average_model_calls: float
    average_tool_calls: float
    average_prompt_tokens: float
    average_completion_tokens: float
    average_cost_usd: float
    average_wall_time_seconds: float
    scope_violation_rate: float
    regression_rate: float


class BenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    benchmark_id: str
    generated_at: str
    task_set_sha256: str
    experiment: dict[str, object]
    overall: MetricGroup
    by_strategy: dict[str, MetricGroup]
    by_language: dict[str, MetricGroup]
    by_difficulty: dict[str, MetricGroup]
    by_defect: dict[str, MetricGroup]
    successes: list[BenchmarkRunRecord]
    failures: list[BenchmarkRunRecord]
    failure_reasons: dict[str, int]
    limitations: list[str]


def _safe_child(root: Path, relative: str, *, must_be_file: bool = False) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise BenchmarkError(f"path escapes benchmark root: {relative}")
    if candidate.is_symlink():
        raise BenchmarkError(f"symbolic links are not allowed in benchmark inputs: {relative}")
    if must_be_file and not candidate.is_file():
        raise BenchmarkError(f"required benchmark file is missing: {relative}")
    return candidate


def load_benchmark(path: Path, *, enforce_scale: bool = True) -> BenchmarkSuite:
    root = path.expanduser().resolve(strict=True)
    manifest_path = _safe_child(root, "benchmark.yaml", must_be_file=True)
    try:
        manifest = BenchmarkManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise BenchmarkError(f"invalid benchmark manifest: {exc}") from exc

    task_paths = sorted(
        root.glob(manifest.task_glob),
        key=lambda task_path: (task_path.name.startswith("go-"), task_path.name),
    )
    tasks: list[BenchmarkTask] = []
    seen_ids: set[str] = set()
    digest = hashlib.sha256()
    for ordinal, task_path in enumerate(task_paths, start=1):
        resolved_task = task_path.resolve(strict=True)
        if not resolved_task.is_relative_to(root / "tasks") or resolved_task.is_symlink():
            raise BenchmarkError("task documents must be regular files under tasks/")
        raw_bytes = resolved_task.read_bytes()
        digest.update(resolved_task.name.encode("utf-8"))
        digest.update(raw_bytes)
        try:
            spec = TaskSpec.model_validate(yaml.safe_load(raw_bytes.decode("utf-8")))
        except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            raise BenchmarkError(f"invalid TaskSpec {resolved_task.name}: {exc}") from exc
        if spec.id in seen_ids:
            raise BenchmarkError(f"duplicate benchmark Task ID: {spec.id}")
        seen_ids.add(spec.id)
        repository = _safe_child(root, spec.repository.path)
        if not repository.is_dir() or not repository.is_relative_to(root / "repos"):
            raise BenchmarkError(f"Task {spec.id} repository must be under repos/")
        extension = "py" if spec.repository.language == "python" else "go"
        hidden = _safe_child(root, f"hidden_tests/{spec.id}.{extension}", must_be_file=True)
        scripted_patch = _safe_child(root, f"scripted_model/{spec.id}.patch", must_be_file=True)
        hidden_target = (
            "test_patchpilot_hidden.py"
            if spec.repository.language == "python"
            else "patchpilot_hidden_test.go"
        )
        task = BenchmarkTask(
            spec=spec,
            task_path=resolved_task,
            repository_template=repository,
            hidden_test=hidden,
            hidden_target=hidden_target,
            scripted_patch=scripted_patch,
            ordinal=ordinal,
        )
        if task.difficulty == "unknown" or task.defect == "unknown":
            raise BenchmarkError(f"Task {spec.id} requires difficulty and defect:<type> metadata")
        tasks.append(task)

    if not tasks:
        raise BenchmarkError("benchmark contains no tasks")
    if enforce_scale:
        _validate_scale(tasks)
    digest.update(json.dumps(manifest.model_dump(mode="json"), sort_keys=True).encode("utf-8"))
    return BenchmarkSuite(root, manifest, tuple(tasks), digest.hexdigest())


def _validate_scale(tasks: list[BenchmarkTask]) -> None:
    python_count = sum(task.language == "python" for task in tasks)
    go_count = sum(task.language == "go" for task in tasks)
    easy_count = sum(task.difficulty == "easy" for task in tasks)
    medium_count = sum(task.difficulty == "medium" for task in tasks)
    defects = {task.defect for task in tasks}
    problems: list[str] = []
    if len(tasks) < 20:
        problems.append("at least 20 tasks")
    if python_count < 12:
        problems.append("at least 12 Python tasks")
    if go_count < 8:
        problems.append("at least 8 Go tasks")
    if easy_count < 5 or medium_count < 5:
        problems.append("at least 5 easy and 5 medium tasks")
    if len(defects) < 6:
        problems.append("at least 6 defect categories")
    if problems:
        raise BenchmarkError("benchmark scale validation failed: " + "; ".join(problems))
