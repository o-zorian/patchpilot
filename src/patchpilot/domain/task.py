from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TASK_SPEC_VERSION = "1"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PYTEST_SELECTOR_PATTERN = re.compile(r"^[A-Za-z0-9_./:\[\]\\-]+$")
_GO_PACKAGE_PATTERN = re.compile(r"^\./(?:[A-Za-z0-9_.-]+/)*(?:[A-Za-z0-9_.-]+|\.\.\.)$")


class TaskSpecLoadError(ValueError):
    """Raised for TaskSpec I/O or contextual validation failures."""


class RepositorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=255)
    language: Literal["python", "go"]


class AcceptanceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: list[str] = Field(min_length=2, max_length=32)
    timeout_seconds: int = Field(gt=0)

    @field_validator("argv")
    @classmethod
    def validate_argv_shape(cls, argv: list[str]) -> list[str]:
        if any(not argument or "\x00" in argument for argument in argv):
            raise ValueError("acceptance argv must contain non-empty, NUL-free arguments")
        forbidden = {"bash", "sh", "cmd", "powershell", "pwsh"}
        if Path(argv[0]).name.lower().removesuffix(".exe") in forbidden:
            raise ValueError("shell interpreters are not accepted by language profiles")
        return argv


class AcceptanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commands: list[AcceptanceCommand] = Field(min_length=1)
    required_tests: list[str] = Field(default_factory=list)

    @field_validator("required_tests")
    @classmethod
    def validate_required_tests(cls, names: list[str]) -> list[str]:
        if any(not name.strip() for name in names):
            raise ValueError("required test names cannot be blank")
        if len(set(names)) != len(names):
            raise ValueError("required test names must be unique")
        return names


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cost_usd: Decimal = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    max_changed_files: int = Field(gt=0)
    max_patch_lines: int = Field(gt=0)


class ExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    network: Literal[False] = False
    cpu_limit: int = Field(default=2, gt=0)
    memory_limit_mb: int = Field(default=1_024, gt=0)


class MetadataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    difficulty: Literal["easy", "medium", "hard"] | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        normalized = [tag.strip() for tag in tags]
        if any(not tag for tag in normalized):
            raise ValueError("metadata tags cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("metadata tags must be unique")
        return normalized


def _validate_relative_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in path.parts
    ):
        raise ValueError(f"path pattern must be relative and cannot traverse: {pattern!r}")
    return normalized


def validate_pytest_selector(selector: str) -> str:
    normalized = selector.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith(("-", "/", ":"))
        or re.match(r"^[A-Za-z]:", normalized)
        or not _PYTEST_SELECTOR_PATTERN.fullmatch(normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise ValueError(f"unsafe or unsupported pytest selector: {selector!r}")
    return normalized


def validate_go_package(package: str) -> str:
    normalized = package.replace("\\", "/")
    if not _GO_PACKAGE_PATTERN.fullmatch(normalized) or ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"unsafe or unsupported Go package path: {package!r}")
    return normalized


def _validate_acceptance_profile(spec: TaskSpec) -> None:
    for command in spec.acceptance.commands:
        argv = command.argv
        if spec.repository.language == "python":
            if argv[:3] != ["python", "-m", "pytest"]:
                raise ValueError("the Python profile accepts only: python -m pytest")
            for selector in argv[3:]:
                validate_pytest_selector(selector)
            continue
        if argv[:2] not in (["go", "test"], ["go", "vet"]):
            raise ValueError("the Go profile accepts only go test or go vet")
        if len(argv) < 3:
            raise ValueError("Go test and vet commands require at least one package path")
        for package in argv[2:]:
            validate_go_package(package)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]
    id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    repository: RepositorySpec
    goal: str = Field(min_length=1, max_length=20_000)
    allowed_paths: list[str] = Field(min_length=1)
    denied_paths: list[str] = Field(default_factory=lambda: [".git/**"])
    acceptance: AcceptanceSpec
    budget: BudgetSpec
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    metadata: MetadataSpec = Field(default_factory=MetadataSpec)

    @field_validator("id")
    @classmethod
    def validate_id(cls, task_id: str) -> str:
        if not _ID_PATTERN.fullmatch(task_id):
            raise ValueError("id must use lowercase letters, digits, dots, underscores, or hyphens")
        return task_id

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, goal: str) -> str:
        if not goal.strip():
            raise ValueError("goal cannot be blank")
        return goal

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def validate_path_patterns(cls, patterns: list[str]) -> list[str]:
        normalized = [_validate_relative_pattern(pattern) for pattern in patterns]
        if len(set(normalized)) != len(normalized):
            raise ValueError("path patterns must be unique")
        return normalized

    @model_validator(mode="after")
    def require_git_denial(self) -> TaskSpec:
        if ".git/**" not in self.denied_paths:
            raise ValueError("denied_paths must include the mandatory .git/** pattern")
        _validate_acceptance_profile(self)
        return self


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    owner_id: str
    spec: TaskSpec
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskLimits:
    max_steps: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: Decimal
    max_wall_time_seconds: int
    max_changed_files: int
    max_patch_lines: int
    max_command_timeout_seconds: int
    max_cpu_limit: int
    max_memory_limit_mb: int


@dataclass(frozen=True, slots=True)
class LoadedTaskSpec:
    spec: TaskSpec
    source_path: Path
    repository_path: Path


def _load_raw_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskSpecLoadError(f"cannot read TaskSpec {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        if path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise TaskSpecLoadError(f"invalid {path.suffix.lower()} document: {exc}") from exc
    raise TaskSpecLoadError("TaskSpec filename must end with .json, .yaml, or .yml")


def _validate_context(spec: TaskSpec, source_path: Path, limits: TaskLimits) -> Path:
    repository = (source_path.parent / spec.repository.path).resolve()
    if not repository.is_dir():
        raise TaskSpecLoadError(f"repository does not exist or is not a directory: {repository}")
    if not (repository / ".git").exists():
        raise TaskSpecLoadError(f"repository does not contain Git metadata: {repository}")

    budget_checks: dict[str, tuple[int | Decimal, int | Decimal]] = {
        "max_steps": (spec.budget.max_steps, limits.max_steps),
        "max_input_tokens": (spec.budget.max_input_tokens, limits.max_input_tokens),
        "max_output_tokens": (spec.budget.max_output_tokens, limits.max_output_tokens),
        "max_cost_usd": (spec.budget.max_cost_usd, limits.max_cost_usd),
        "max_wall_time_seconds": (
            spec.budget.max_wall_time_seconds,
            limits.max_wall_time_seconds,
        ),
        "max_changed_files": (spec.budget.max_changed_files, limits.max_changed_files),
        "max_patch_lines": (spec.budget.max_patch_lines, limits.max_patch_lines),
    }
    exceeded = [name for name, (value, maximum) in budget_checks.items() if value > maximum]
    if exceeded:
        raise TaskSpecLoadError(f"TaskSpec exceeds system hard limits: {', '.join(exceeded)}")

    if any(
        command.timeout_seconds > limits.max_command_timeout_seconds
        for command in spec.acceptance.commands
    ):
        raise TaskSpecLoadError("acceptance command timeout exceeds the system hard limit")
    if spec.execution.cpu_limit > limits.max_cpu_limit:
        raise TaskSpecLoadError("execution cpu_limit exceeds the system hard limit")
    if spec.execution.memory_limit_mb > limits.max_memory_limit_mb:
        raise TaskSpecLoadError("execution memory_limit_mb exceeds the system hard limit")
    return repository


def load_task_spec(path: Path, limits: TaskLimits) -> LoadedTaskSpec:
    source_path = path.expanduser().resolve()
    raw = _load_raw_document(source_path)
    try:
        spec = TaskSpec.model_validate(raw)
    except ValidationError:
        raise
    repository_path = _validate_context(spec, source_path, limits)
    return LoadedTaskSpec(
        spec=spec,
        source_path=source_path,
        repository_path=repository_path,
    )


def validate_task_spec_context(
    spec: TaskSpec,
    *,
    base_directory: Path,
    limits: TaskLimits,
) -> TaskSpec:
    """Validate an API-provided TaskSpec and freeze its repository as an absolute path."""

    synthetic_source = base_directory.expanduser().resolve() / "api-task.json"
    repository = _validate_context(spec, synthetic_source, limits)
    return spec.model_copy(
        update={"repository": spec.repository.model_copy(update={"path": str(repository)})}
    )


def task_spec_json_schema() -> dict[str, Any]:
    return TaskSpec.model_json_schema()
