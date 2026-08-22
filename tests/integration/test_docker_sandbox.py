from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from patchpilot.sandbox.docker import DockerSandbox
from patchpilot.sandbox.local import run_argv
from tests.helpers import create_git_repository


def docker_binary() -> str:
    if os.environ.get("PATCHPILOT_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set PATCHPILOT_RUN_DOCKER_TESTS=1 to run real Docker isolation tests")
    value = os.environ.get("SANDBOX_DOCKER_BINARY", "docker")
    if value == "docker" and shutil.which(value) is None:
        pytest.skip("Docker CLI is unavailable")
    if value != "docker" and not Path(value).is_file():
        pytest.skip("Docker CLI is unavailable")
    return value


def make_sandbox(tmp_path: Path, *, memory_mb: int = 128, pids: int = 32) -> DockerSandbox:
    workspace = create_git_repository(
        tmp_path / "workspace",
        {"README.md": "sandbox fixture\n"},
    )
    runner = DockerSandbox(
        workspace=workspace,
        run_id=uuid4(),
        language="python",
        image="patchpilot-python:latest",
        cpu_limit=1,
        memory_limit_mb=memory_mb,
        pids_limit=pids,
        docker_binary=docker_binary(),
    )
    runner.check_available()
    return runner


@pytest.mark.docker
def test_docker_blocks_network_and_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_sandbox(tmp_path)
    monkeypatch.setenv("PATCHPILOT_HOST_SECRET", "must-not-enter-container")
    code = """import os, socket
assert 'PATCHPILOT_HOST_SECRET' not in os.environ
assert 'MODEL_API_KEY' not in os.environ
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(('1.1.1.1', 53))
except OSError:
    print('network-blocked')
else:
    raise SystemExit('network unexpectedly available')
"""

    result = runner.run(
        ["python", "-c", code],
        cwd=runner.workspace,
        timeout_seconds=10,
        output_max_chars=10_000,
    )

    assert result.return_code == 0
    assert "network-blocked" in result.stdout
    assert "must-not-enter-container" not in result.stdout + result.stderr


@pytest.mark.docker
def test_docker_is_non_root_read_only_and_workspace_scoped(tmp_path: Path) -> None:
    runner = make_sandbox(tmp_path)
    code = """import os
from pathlib import Path
assert os.geteuid() == 10001
try:
    Path('/patchpilot-host-write').write_text('forbidden')
except OSError:
    pass
else:
    raise SystemExit('root filesystem was writable')
Path('/workspace/sandbox-output.txt').write_text('workspace-only', encoding='utf-8')
print(os.geteuid())
"""

    result = runner.run(
        ["python", "-c", code],
        cwd=runner.workspace,
        timeout_seconds=10,
        output_max_chars=10_000,
    )

    assert result.return_code == 0
    assert result.stdout.strip() == "10001"
    assert (runner.workspace / "sandbox-output.txt").read_text(encoding="utf-8") == "workspace-only"


@pytest.mark.docker
def test_docker_enforces_cpu_memory_and_pid_limits(tmp_path: Path) -> None:
    runner = make_sandbox(tmp_path, memory_mb=128, pids=32)
    code = """import json
from pathlib import Path
values = {
    'cpu': Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
    'memory': Path('/sys/fs/cgroup/memory.max').read_text().strip(),
    'pids': Path('/sys/fs/cgroup/pids.max').read_text().strip(),
}
print(json.dumps(values, sort_keys=True))
"""

    result = runner.run(
        ["python", "-c", code],
        cwd=runner.workspace,
        timeout_seconds=10,
        output_max_chars=10_000,
    )
    limits = json.loads(result.stdout)
    quota, period = (int(value) for value in limits["cpu"].split())

    assert result.return_code == 0
    assert quota / period == 1
    assert int(limits["memory"]) == 128 * 1_024 * 1_024
    assert int(limits["pids"]) == 32


@pytest.mark.docker
def test_docker_timeout_removes_container(tmp_path: Path) -> None:
    runner = make_sandbox(tmp_path)

    result = runner.run(
        ["python", "-c", "import time; time.sleep(30)"],
        cwd=runner.workspace,
        timeout_seconds=0.5,
        output_max_chars=10_000,
    )
    remaining = run_argv(
        [
            runner.docker_binary,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.patchpilot.run={runner.run_id}",
        ],
        cwd=runner.workspace,
        timeout_seconds=10,
        output_max_chars=2_000,
    )

    assert result.timed_out is True
    assert remaining.return_code == 0
    assert remaining.stdout.strip() == ""


@pytest.mark.docker
def test_python_and_go_images_are_non_root() -> None:
    executable = docker_binary()
    for image, command in (
        ("patchpilot-python:latest", ["python", "--version"]),
        ("patchpilot-go:latest", ["go", "version"]),
    ):
        result = run_argv(
            [
                executable,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                image,
                *command,
            ],
            cwd=Path.cwd(),
            timeout_seconds=30,
            output_max_chars=5_000,
        )
        assert result.return_code == 0
