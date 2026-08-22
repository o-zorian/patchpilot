from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchpilot.agent.prompts import REAL_BENCHMARK_PROMPT_VERSION
from patchpilot.benchmark.models import MANDATORY_STRATEGIES, BenchmarkError
from patchpilot.domain.run import RunStrategy
from patchpilot.domain.task import TaskSpec

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class RealSuiteKind(StrEnum):
    CALIBRATION = "calibration"
    FORMAL = "formal"


class OutcomeClass(StrEnum):
    TASK_RESULT = "task_result"
    API_ERROR = "api_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    INTERRUPTED = "interrupted"


class RealBenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]
    id: str = Field(min_length=1, max_length=128)
    suite_kind: RealSuiteKind
    frozen: bool
    task_glob: Literal["tasks/*.yaml"] = "tasks/*.yaml"
    source_registry: Literal["SOURCES.yaml"] = "SOURCES.yaml"
    strategies: list[RunStrategy]
    prompt_version: str = REAL_BENCHMARK_PROMPT_VERSION
    repetitions: int = Field(default=1, gt=0, le=3)
    concurrency: int = Field(default=1, gt=0, le=4)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=4_096, gt=0)
    scoring_rule: Literal["all_acceptance_must_pass"] = "all_acceptance_must_pass"
    retry_rule: Literal["transport_only_bounded_no_task_reruns"] = (
        "transport_only_bounded_no_task_reruns"
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> RealBenchmarkManifest:
        if not _ID.fullmatch(self.id):
            raise ValueError("benchmark id contains unsupported characters")
        if tuple(self.strategies) != MANDATORY_STRATEGIES:
            raise ValueError("strategies must list the four mandatory strategies in order")
        if self.prompt_version != REAL_BENCHMARK_PROMPT_VERSION:
            raise ValueError(
                f"real benchmark prompt_version must be {REAL_BENCHMARK_PROMPT_VERSION}"
            )
        if self.suite_kind == RealSuiteKind.FORMAL and not self.frozen:
            raise ValueError("formal real benchmark manifests must be frozen")
        if self.suite_kind == RealSuiteKind.CALIBRATION and self.frozen:
            raise ValueError("calibration manifests must not be marked frozen")
        return self


class RealExperimentBudget(BaseModel):
    """Run-safety limits overridden by an explicitly separate experiment profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_steps: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    max_cost_usd: Decimal = Field(gt=0)


class RealExperimentProfile(BaseModel):
    """A frozen ablation matrix layered on top of, but not merged into, real-v1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]
    id: str = Field(min_length=1, max_length=128)
    classification: Literal["budget_ablation"]
    base_benchmark_id: str = Field(min_length=1, max_length=128)
    strategy: RunStrategy
    repetitions: int = Field(gt=0, le=3)
    concurrency: int = Field(gt=0, le=4)
    global_cost_limit_usd: Decimal = Field(gt=0)
    budget: RealExperimentBudget

    @model_validator(mode="after")
    def validate_profile(self) -> RealExperimentProfile:
        if not _ID.fullmatch(self.id) or not _ID.fullmatch(self.base_benchmark_id):
            raise ValueError("experiment or base benchmark id contains unsupported characters")
        if self.strategy != RunStrategy.FULL:
            raise ValueError("budget ablation profile must select only the full strategy")
        return self


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    provenance: Literal["curated_snapshot", "upstream_snapshot"]
    source_name: str = Field(min_length=1, max_length=255)
    source_url: str | None = Field(default=None, max_length=2_000)
    upstream_commit: str | None = Field(default=None, max_length=128)
    license: str = Field(min_length=1, max_length=128)
    license_file: str
    attribution: str = Field(min_length=1, max_length=2_000)
    hidden_target: str
    reference_patch: str
    changed_files_expected: int = Field(gt=0, le=4)


class SourceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]
    notice: str = Field(min_length=1, max_length=5_000)
    tasks: list[SourceRecord]


@dataclass(frozen=True, slots=True)
class RealBenchmarkTask:
    spec: TaskSpec
    task_path: Path
    repository_template: Path
    hidden_test: Path
    hidden_target: str
    reference_patch: Path
    source: SourceRecord
    repository_sha256: str
    hidden_test_sha256: str
    ordinal: int

    @property
    def language(self) -> Literal["python", "go"]:
        return self.spec.repository.language

    @property
    def difficulty(self) -> Literal["easy", "medium", "hard"]:
        value = self.spec.metadata.difficulty
        if value is None:
            raise BenchmarkError(f"Task {self.spec.id} has no difficulty")
        return value

    @property
    def defect(self) -> str:
        for tag in self.spec.metadata.tags:
            if tag.startswith("defect:"):
                return tag.removeprefix("defect:")
        raise BenchmarkError(f"Task {self.spec.id} has no defect:<type> tag")


@dataclass(frozen=True, slots=True)
class RealBenchmarkSuite:
    root: Path
    manifest: RealBenchmarkManifest
    tasks: tuple[RealBenchmarkTask, ...]
    manifest_sha256: str
    task_set_sha256: str


@dataclass(frozen=True, slots=True)
class LoadedRealExperimentProfile:
    path: Path
    profile: RealExperimentProfile
    sha256: str


class RealBenchmarkRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    run_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str
    task_id: str
    strategy: RunStrategy
    repetition: int = Field(gt=0)
    language: Literal["python", "go"]
    difficulty: Literal["easy", "medium", "hard"]
    defect: str
    outcome_class: OutcomeClass
    result: str
    error_code: str | None = None
    passed: bool
    first_gate_passed: bool
    provider: str
    requested_model: str
    actual_model_ids: list[str]
    prompt_version: str
    temperature: float
    started_at: datetime
    completed_at: datetime
    steps: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    model_retries: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    usage_estimated: bool
    estimated_cost_usd: Decimal = Field(ge=0)
    reserved_unknown_cost_usd: Decimal = Field(ge=0)
    model_latency_ms: int = Field(ge=0)
    wall_time_seconds: float = Field(ge=0)
    scope_violation: bool
    regression: bool
    baseline_commit: str
    artifact_directory: str


class RealBenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    benchmark_id: str
    suite_kind: RealSuiteKind
    generated_at: datetime
    manifest_sha256: str
    task_set_sha256: str
    experiment: dict[str, object]
    counts: dict[str, int]
    cost: dict[str, str]
    latency: dict[str, float]
    agent_metrics: dict[str, object]
    event_metrics: dict[str, object] = Field(default_factory=dict)
    paired_comparisons: dict[str, dict[str, object]]
    by_strategy: dict[str, dict[str, object]]
    by_language: dict[str, dict[str, object]]
    by_difficulty: dict[str, dict[str, object]]
    by_defect: dict[str, dict[str, object]]
    failure_reasons: dict[str, int]
    limitations: list[str]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    root = path.resolve(strict=True)
    for entry in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if entry.is_symlink():
            raise BenchmarkError(f"symbolic link is not allowed in real benchmark: {entry}")
        if not entry.is_file():
            continue
        relative = entry.relative_to(root).as_posix()
        if relative.startswith(".git/") or "__pycache__" in entry.parts:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_child(root: Path, relative: str, *, file: bool = False) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if not candidate.is_relative_to(root) or candidate.is_symlink():
        raise BenchmarkError(f"unsafe real benchmark path: {relative}")
    if file and not candidate.is_file():
        raise BenchmarkError(f"required real benchmark file is missing: {relative}")
    return candidate


def _validate_scale(suite_kind: RealSuiteKind, tasks: list[RealBenchmarkTask]) -> None:
    if suite_kind == RealSuiteKind.CALIBRATION:
        if not 5 <= len(tasks) <= 8:
            raise BenchmarkError("calibration suite must contain 5 to 8 tasks")
        return
    if len(tasks) < 20:
        raise BenchmarkError("formal real benchmark must contain at least 20 tasks")
    languages = {
        language: sum(task.language == language for task in tasks) for language in ("python", "go")
    }
    if not all(languages.values()):
        raise BenchmarkError("formal real benchmark must cover Python and Go")
    counts = {
        difficulty: sum(task.difficulty == difficulty for task in tasks)
        for difficulty in ("easy", "medium", "hard")
    }
    total = len(tasks)
    expected = {"easy": 0.20, "medium": 0.50, "hard": 0.30}
    if any(abs(counts[name] / total - ratio) > 0.08 for name, ratio in expected.items()):
        raise BenchmarkError("formal difficulty distribution must be approximately 20/50/30")
    if len({task.defect for task in tasks}) < 8:
        raise BenchmarkError("formal real benchmark requires at least 8 defect categories")
    if sum(task.source.changed_files_expected >= 2 for task in tasks) < 8:
        raise BenchmarkError("formal real benchmark requires at least 8 multi-file tasks")


def load_real_benchmark(path: Path) -> RealBenchmarkSuite:
    root = path.expanduser().resolve(strict=True)
    manifest_path = _safe_child(root, "benchmark.yaml", file=True)
    try:
        manifest = RealBenchmarkManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        registry = SourceRegistry.model_validate(
            yaml.safe_load(
                _safe_child(root, manifest.source_registry, file=True).read_text("utf-8")
            )
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise BenchmarkError(f"invalid real benchmark metadata: {exc}") from exc
    source_by_id = {record.task_id: record for record in registry.tasks}
    if len(source_by_id) != len(registry.tasks):
        raise BenchmarkError("duplicate task IDs in SOURCES.yaml")

    digest = hashlib.sha256()
    tasks: list[RealBenchmarkTask] = []
    seen: set[str] = set()
    task_paths = sorted(root.glob(manifest.task_glob))
    for ordinal, task_path in enumerate(task_paths, start=1):
        resolved_task = task_path.resolve(strict=True)
        if not resolved_task.is_relative_to(root / "tasks") or resolved_task.is_symlink():
            raise BenchmarkError("TaskSpecs must be regular files under tasks/")
        raw = resolved_task.read_bytes()
        try:
            spec = TaskSpec.model_validate(yaml.safe_load(raw.decode("utf-8")))
        except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            raise BenchmarkError(f"invalid TaskSpec {task_path.name}: {exc}") from exc
        if spec.id in seen:
            raise BenchmarkError(f"duplicate TaskSpec ID: {spec.id}")
        seen.add(spec.id)
        source = source_by_id.get(spec.id)
        if source is None:
            raise BenchmarkError(f"Task {spec.id} has no source record")
        repository = _safe_child(root, spec.repository.path)
        if not repository.is_dir() or not repository.is_relative_to(root / "repos"):
            raise BenchmarkError(f"Task {spec.id} repository must be under repos/")
        suffix = "py" if spec.repository.language == "python" else "go"
        hidden = _safe_child(root, f"hidden_tests/{spec.id}.{suffix}", file=True)
        reference = _safe_child(root, source.reference_patch, file=True)
        license_path = _safe_child(root, source.license_file, file=True)
        if not license_path.is_relative_to(repository):
            raise BenchmarkError(f"Task {spec.id} license file must be inside its repository")
        task = RealBenchmarkTask(
            spec=spec,
            task_path=resolved_task,
            repository_template=repository,
            hidden_test=hidden,
            hidden_target=source.hidden_target,
            reference_patch=reference,
            source=source,
            repository_sha256=sha256_tree(repository),
            hidden_test_sha256=sha256_file(hidden),
            ordinal=ordinal,
        )
        _ = task.difficulty
        _ = task.defect
        tasks.append(task)
        for relative, content in (
            (f"tasks/{resolved_task.name}", raw),
            (f"hidden_tests/{hidden.name}", hidden.read_bytes()),
            (source.reference_patch, reference.read_bytes()),
        ):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        digest.update(task.repository_sha256.encode("ascii"))
    if set(source_by_id) != seen:
        extras = sorted(set(source_by_id) - seen)
        raise BenchmarkError(f"source records without TaskSpecs: {', '.join(extras)}")
    if not tasks:
        raise BenchmarkError("real benchmark contains no tasks")
    _validate_scale(manifest.suite_kind, tasks)
    manifest_sha256 = sha256_file(manifest_path)
    digest.update(manifest_path.read_bytes())
    digest.update(_safe_child(root, manifest.source_registry, file=True).read_bytes())
    return RealBenchmarkSuite(
        root=root,
        manifest=manifest,
        tasks=tuple(tasks),
        manifest_sha256=manifest_sha256,
        task_set_sha256=digest.hexdigest(),
    )


def load_real_experiment_profile(
    path: Path, suite: RealBenchmarkSuite
) -> LoadedRealExperimentProfile:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise BenchmarkError("real experiment profile must be a regular file")
    try:
        raw = resolved.read_bytes()
        profile = RealExperimentProfile.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise BenchmarkError(f"invalid real experiment profile: {exc}") from exc
    if profile.base_benchmark_id != suite.manifest.id:
        raise BenchmarkError("experiment profile does not target this benchmark")
    return LoadedRealExperimentProfile(
        path=resolved,
        profile=profile,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def real_run_key(
    suite: RealBenchmarkSuite,
    task: RealBenchmarkTask,
    strategy: RunStrategy,
    repetition: int,
    experiment_fingerprint: str,
) -> str:
    material = json.dumps(
        {
            "benchmark": suite.manifest.id,
            "task_set": suite.task_set_sha256,
            "task": task.spec.id,
            "strategy": strategy.value,
            "repetition": repetition,
            "experiment": experiment_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
