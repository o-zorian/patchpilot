from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from patchpilot.domain.task import TaskLimits, TaskSpec, TaskSpecLoadError, load_task_spec


def write_task(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_loads_valid_yaml_relative_to_task_file(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    task_limits: TaskLimits,
) -> None:
    path = write_task(tmp_path / "task.yaml", valid_task_data)

    loaded = load_task_spec(path, task_limits)

    assert loaded.spec.id == "py-boundary-001"
    assert loaded.repository_path == (tmp_path / "repo").resolve()


def test_loads_valid_json(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    task_limits: TaskLimits,
) -> None:
    path = tmp_path / "task.json"
    path.write_text(json.dumps(valid_task_data), encoding="utf-8")

    assert load_task_spec(path, task_limits).spec.version == "1"


def test_accepts_bounded_go_test_and_vet_commands(valid_task_data: dict[str, Any]) -> None:
    valid_task_data["repository"]["language"] = "go"
    valid_task_data["acceptance"] = {
        "commands": [
            {"argv": ["go", "test", "./..."], "timeout_seconds": 120},
            {"argv": ["go", "vet", "./internal/service"], "timeout_seconds": 120},
        ],
        "required_tests": ["TestListItemsPageZero"],
    }

    spec = TaskSpec.model_validate(valid_task_data)

    assert spec.repository.language == "go"
    assert spec.acceptance.commands[0].argv == ["go", "test", "./..."]


def test_accepts_go_race_detector_without_general_flag_passthrough(
    valid_task_data: dict[str, Any],
) -> None:
    valid_task_data["repository"]["language"] = "go"
    valid_task_data["acceptance"] = {
        "commands": [{"argv": ["go", "test", "-race", "./registry"], "timeout_seconds": 60}],
        "required_tests": ["TestConcurrentIncrement"],
    }

    spec = TaskSpec.model_validate(valid_task_data)

    assert spec.acceptance.commands[0].argv == ["go", "test", "-race", "./registry"]


@pytest.mark.parametrize(
    "argv",
    [
        ["go", "test", "-run", "TestOnly"],
        ["go", "vet", "-race", "./..."],
        ["go", "test", "./...", "-race"],
        ["go", "test", "../outside"],
        ["go", "get", "example.com/dependency"],
        ["sh", "-c", "go test ./..."],
    ],
)
def test_rejects_unsafe_go_commands(valid_task_data: dict[str, Any], argv: list[str]) -> None:
    valid_task_data["repository"]["language"] = "go"
    valid_task_data["acceptance"]["commands"][0]["argv"] = argv

    with pytest.raises(ValidationError):
        TaskSpec.model_validate(valid_task_data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(version="2"), "Input should be '1'"),
        (lambda data: data.update(extra_field=True), "Extra inputs are not permitted"),
        (lambda data: data.update(allowed_paths=["../outside/**"]), "cannot traverse"),
        (lambda data: data.update(denied_paths=[".github/**"]), "must include"),
        (
            lambda data: data["acceptance"]["commands"][0].update(
                argv=["python", "-m", "pytest", "-c", "outside.ini"]
            ),
            "unsafe or unsupported",
        ),
        (lambda data: data["budget"].update(max_steps=0), "greater than 0"),
        (lambda data: data["repository"].update(language="go"), "Go profile accepts"),
        (lambda data: data["execution"].update(network=True), "Input should be False"),
    ],
)
def test_rejects_invalid_protocol(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    task_limits: TaskLimits,
    mutate: Any,
    message: str,
) -> None:
    mutate(valid_task_data)
    path = write_task(tmp_path / "task.yaml", valid_task_data)

    with pytest.raises(ValidationError, match=message):
        load_task_spec(path, task_limits)


def test_rejects_missing_git_metadata(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    task_limits: TaskLimits,
) -> None:
    (tmp_path / "repo" / ".git").rmdir()
    path = write_task(tmp_path / "task.yaml", valid_task_data)

    with pytest.raises(TaskSpecLoadError, match="Git metadata"):
        load_task_spec(path, task_limits)


def test_rejects_budget_above_hard_limit(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    task_limits: TaskLimits,
) -> None:
    valid_task_data["budget"]["max_steps"] = task_limits.max_steps + 1
    path = write_task(tmp_path / "task.yaml", valid_task_data)

    with pytest.raises(TaskSpecLoadError, match="max_steps"):
        load_task_spec(path, task_limits)
