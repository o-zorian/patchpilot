from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from patchpilot.sandbox.base import SandboxError
from patchpilot.sandbox.local import CommandResult, run_argv

_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
_ENVIRONMENT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_ENVIRONMENT = frozenset(
    {
        "GOCACHE",
        "GOMODCACHE",
        "GONOSUMDB",
        "GOPROXY",
        "GOSUMDB",
        "GOTOOLCHAIN",
        "HOME",
        "PYTEST_ADDOPTS",
        "PYTHONDONTWRITEBYTECODE",
        "TMPDIR",
    }
)


class DockerSandbox:
    """Run one command in a disposable, resource-limited, networkless container."""

    def __init__(
        self,
        *,
        workspace: Path,
        run_id: UUID,
        language: str,
        image: str,
        cpu_limit: int,
        memory_limit_mb: int,
        pids_limit: int,
        docker_binary: str = "docker",
    ) -> None:
        resolved = workspace.expanduser().resolve(strict=True)
        if not resolved.is_dir() or not (resolved / ".git").is_dir():
            raise SandboxError("Docker Sandbox requires a Git Workspace")
        if "," in str(resolved):
            raise SandboxError("Workspace paths containing commas cannot be mounted safely")
        if language not in {"python", "go"}:
            raise SandboxError(f"unsupported Docker Sandbox language: {language}")
        if not _IMAGE_PATTERN.fullmatch(image):
            raise SandboxError("sandbox image reference contains unsupported characters")
        if cpu_limit <= 0 or memory_limit_mb <= 0 or pids_limit <= 0:
            raise SandboxError("sandbox resource limits must be positive")
        self.workspace = resolved
        self.run_id = run_id
        self.language = language
        self._image = image
        self.cpu_limit = cpu_limit
        self.memory_limit_mb = memory_limit_mb
        self.pids_limit = pids_limit
        self.docker_binary = docker_binary

    @property
    def isolation(self) -> str:
        return "docker"

    @property
    def image(self) -> str:
        return self._image

    def check_available(self) -> None:
        result = run_argv(
            [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
            cwd=self.workspace,
            timeout_seconds=10,
            output_max_chars=2_000,
        )
        if result.return_code != 0 or not result.stdout.strip():
            raise SandboxError("Docker daemon is unavailable")

    def build_run_argv(
        self,
        argv: Sequence[str],
        *,
        container_name: str,
        environment: Mapping[str, str] | None = None,
    ) -> list[str]:
        inner = self._container_argv(argv)
        safe_environment = self._safe_environment(environment)
        mount = f"type=bind,source={self.workspace},target=/workspace"
        memory = f"{self.memory_limit_mb}m"
        temporary_size = max(64, min(256, self.memory_limit_mb // 4))
        temporary_options = "rw,nosuid,nodev"
        if self.language == "go":
            # The Go toolchain executes its compiled test binary from TMPDIR.
            temporary_options += ",exec"
        else:
            temporary_options += ",noexec"
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--label",
            "com.patchpilot.managed=true",
            "--label",
            f"com.patchpilot.run={self.run_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--cpus",
            str(self.cpu_limit),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--user",
            "10001:10001",
            "--workdir",
            "/workspace",
            "--mount",
            mount,
            "--tmpfs",
            f"/tmp:{temporary_options},size={temporary_size}m",
        ]
        for key, value in sorted(safe_environment.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend([self._image, *inner])
        return command

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_max_chars: int,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        if cwd.expanduser().resolve(strict=True) != self.workspace:
            raise SandboxError("Docker commands must run at the Workspace root")
        container_name = f"patchpilot-{self.run_id.hex[:12]}-{uuid4().hex[:10]}"
        docker_argv = self.build_run_argv(
            argv,
            container_name=container_name,
            environment=environment,
        )
        try:
            result = run_argv(
                docker_argv,
                cwd=self.workspace,
                timeout_seconds=timeout_seconds,
                output_max_chars=output_max_chars,
                cancel_event=cancel_event,
            )
        except OSError as exc:
            raise SandboxError("failed to start the Docker CLI") from exc
        finally:
            self._remove_container(container_name)
        if result.return_code == 125:
            raise SandboxError("Docker rejected or failed to start the sandbox container")
        return replace(result, argv=tuple(argv))

    def _remove_container(self, container_name: str) -> None:
        try:
            result = run_argv(
                [self.docker_binary, "rm", "--force", container_name],
                cwd=self.workspace,
                timeout_seconds=10,
                output_max_chars=2_000,
            )
        except OSError as exc:
            raise SandboxError("could not verify sandbox container cleanup") from exc
        if result.return_code != 0 and "no such container" not in result.stderr.lower():
            raise SandboxError("could not verify sandbox container cleanup")

    def _container_argv(self, argv: Sequence[str]) -> list[str]:
        if not argv or any(not value or "\x00" in value for value in argv):
            raise SandboxError("container argv must contain non-empty, NUL-free arguments")
        inner = list(argv)
        executable = Path(inner[0]).name.lower()
        if self.language == "python" and executable in {
            "python",
            "python.exe",
            "python3",
            "python3.exe",
        }:
            inner[0] = "python"
        return inner

    @staticmethod
    def _safe_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
        defaults = {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GOCACHE": "/tmp/go-cache",
            "GOMODCACHE": "/tmp/go-mod-cache",
            "GONOSUMDB": "*",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
        }
        for key, value in (environment or {}).items():
            if key not in _SAFE_ENVIRONMENT or not _ENVIRONMENT_PATTERN.fullmatch(key):
                raise SandboxError(f"environment variable is not allowed in sandbox: {key}")
            if "\x00" in value or len(value) > 2_000:
                raise SandboxError(f"invalid sandbox environment value: {key}")
            defaults[key] = value
        return defaults
