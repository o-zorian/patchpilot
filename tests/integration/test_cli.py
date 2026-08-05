from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from patchpilot.cli.app import app

runner = CliRunner()


def write_task(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_cli_validates_and_creates_persistent_pending_run(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    monkeypatch: Any,
) -> None:
    task_path = write_task(tmp_path / "task.yaml", valid_task_data)
    database_path = tmp_path / "data" / "patchpilot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))

    validate_result = runner.invoke(app, ["task", "validate", str(task_path), "--json"])
    create_result = runner.invoke(
        app,
        [
            "run",
            "create",
            str(task_path),
            "--model",
            "scripted-test",
            "--strategy",
            "full",
            "--json",
        ],
    )

    assert validate_result.exit_code == 0
    assert json.loads(validate_result.stdout)["ok"] is True
    assert create_result.exit_code == 0
    created = json.loads(create_result.stdout)
    assert created["ok"] is True
    assert created["status"] == "pending"
    assert database_path.is_file()

    show_result = runner.invoke(app, ["run", "show", created["id"], "--json"])

    assert show_result.exit_code == 0
    assert json.loads(show_result.stdout)["id"] == created["id"]


def test_cli_invalid_task_returns_nonzero_exit(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    valid_task_data["denied_paths"] = [".github/**"]
    task_path = write_task(tmp_path / "task.yaml", valid_task_data)

    result = runner.invoke(app, ["task", "validate", str(task_path), "--json"])

    assert result.exit_code == 1
    error = json.loads(result.stdout)
    assert error["ok"] is False
    assert error["error"]["code"] == "VALIDATION_ERROR"
