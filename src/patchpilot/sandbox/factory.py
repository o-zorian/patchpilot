from __future__ import annotations

from pathlib import Path
from uuid import UUID

from patchpilot.config import AppSettings, SettingsError
from patchpilot.domain.task import TaskSpec
from patchpilot.sandbox.base import CommandSandbox
from patchpilot.sandbox.docker import DockerSandbox
from patchpilot.sandbox.local import run_argv
from patchpilot.sandbox.trusted_local import TrustedLocalSandbox
from patchpilot.sandbox.workspace import Workspace


def ensure_sandbox_runtime(settings: AppSettings) -> None:
    if settings.sandbox_mode == "local":
        if not settings.allow_trusted_local_execution:
            raise SettingsError(
                "SANDBOX_MODE=local requires ALLOW_TRUSTED_LOCAL_EXECUTION=true; "
                "unknown repositories require Docker"
            )
        return
    try:
        result = run_argv(
            [settings.sandbox_docker_binary, "version", "--format", "{{.Server.Version}}"],
            cwd=Path.cwd(),
            timeout_seconds=10,
            output_max_chars=2_000,
        )
    except OSError as exc:
        raise SettingsError("Docker CLI is unavailable for SANDBOX_MODE=docker") from exc
    if result.return_code != 0 or not result.stdout.strip():
        raise SettingsError("Docker daemon is unavailable for SANDBOX_MODE=docker")


def command_sandbox_for(
    settings: AppSettings,
    workspace: Workspace,
    spec: TaskSpec,
    run_id: UUID,
) -> CommandSandbox:
    if settings.sandbox_mode == "local":
        if not settings.allow_trusted_local_execution:
            raise SettingsError("trusted-local execution is not explicitly enabled")
        return TrustedLocalSandbox()
    image = (
        settings.sandbox_image_python
        if spec.repository.language == "python"
        else settings.sandbox_image_go
    )
    return DockerSandbox(
        workspace=workspace.path,
        run_id=run_id,
        language=spec.repository.language,
        image=image,
        cpu_limit=spec.execution.cpu_limit,
        memory_limit_mb=spec.execution.memory_limit_mb,
        pids_limit=settings.sandbox_pids_limit,
        docker_binary=settings.sandbox_docker_binary,
    )
