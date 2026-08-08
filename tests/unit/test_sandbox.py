from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from patchpilot.config import AppSettings, SettingsError
from patchpilot.sandbox.base import SandboxError
from patchpilot.sandbox.docker import DockerSandbox
from patchpilot.sandbox.factory import ensure_sandbox_runtime


def sandbox(tmp_path: Path) -> DockerSandbox:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    return DockerSandbox(
        workspace=workspace,
        run_id=uuid4(),
        language="python",
        image="patchpilot-python:latest",
        cpu_limit=2,
        memory_limit_mb=256,
        pids_limit=64,
        docker_binary="docker",
    )


def test_docker_argv_enforces_isolation_and_forwards_only_safe_environment(
    tmp_path: Path,
) -> None:
    runner = sandbox(tmp_path)

    argv = runner.build_run_argv(
        ["python", "-m", "pytest"],
        container_name="patchpilot-test",
        environment={"PYTEST_ADDOPTS": "--color=no"},
    )

    assert argv[:2] == ["docker", "run"]
    assert ["--network", "none"] == argv[argv.index("--network") : argv.index("--network") + 2]
    assert "--read-only" in argv
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "no-new-privileges" in argv
    assert ["--pids-limit", "64"] == argv[
        argv.index("--pids-limit") : argv.index("--pids-limit") + 2
    ]
    assert ["--cpus", "2"] == argv[argv.index("--cpus") : argv.index("--cpus") + 2]
    assert ["--memory", "256m"] == argv[argv.index("--memory") : argv.index("--memory") + 2]
    assert ["--user", "10001:10001"] == argv[argv.index("--user") : argv.index("--user") + 2]
    assert sum("target=/workspace" in argument for argument in argv) == 1
    assert not any("docker.sock" in argument.lower() for argument in argv)
    assert not any("MODEL_API_KEY" in argument for argument in argv)
    assert "PYTEST_ADDOPTS=--color=no" in argv
    assert any(value.startswith("/tmp:rw,nosuid,nodev,noexec,") for value in argv)
    assert argv[-4:] == ["patchpilot-python:latest", "python", "-m", "pytest"]


def test_go_sandbox_allows_only_its_temporary_test_binary_to_execute(tmp_path: Path) -> None:
    runner = sandbox(tmp_path)
    runner.language = "go"

    argv = runner.build_run_argv(
        ["go", "test", "./..."],
        container_name="patchpilot-go-test",
    )

    assert any(value.startswith("/tmp:rw,nosuid,nodev,exec,") for value in argv)


def test_docker_sandbox_rejects_environment_and_workspace_escape(tmp_path: Path) -> None:
    runner = sandbox(tmp_path)

    with pytest.raises(SandboxError, match="not allowed"):
        runner.build_run_argv(
            ["python", "--version"],
            container_name="patchpilot-test",
            environment={"MODEL_API_KEY": "must-not-enter"},
        )
    with pytest.raises(SandboxError, match="Workspace root"):
        runner.run(
            ["python", "--version"],
            cwd=tmp_path,
            timeout_seconds=1,
            output_max_chars=1_000,
        )


def test_trusted_local_requires_explicit_authorization() -> None:
    settings = AppSettings(
        _env_file=None,
        sandbox_mode="local",
        allow_trusted_local_execution=False,
    )

    with pytest.raises(SettingsError, match="unknown repositories require Docker"):
        ensure_sandbox_runtime(settings)
