from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from patchpilot.domain.task import TaskSpec
from scripts.prepare_demo import prepare_repository

ROOT = Path(__file__).resolve().parents[2]


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        shell=False,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in completed.stdout.split(b"\0") if item]


def test_repository_has_no_tracked_cache_secret_or_large_artifact() -> None:
    forbidden_parts = {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "demo-work",
        "workspaces",
    }
    secret_patterns = (
        re.compile("sk-" + r"[A-Za-z0-9_-]{20,}"),
        re.compile("ghp_" + r"[A-Za-z0-9]{20,}"),
        re.compile("AK" + r"IA[0-9A-Z]{16}"),
        re.compile("BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    )
    violations: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if forbidden_parts.intersection(relative.parts):
            violations.append(f"forbidden generated path: {relative.as_posix()}")
        if relative.name == ".env":
            violations.append("tracked .env file")
        if path.stat().st_size > 1_048_576:
            violations.append(f"file exceeds 1 MiB: {relative.as_posix()}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in secret_patterns:
            if pattern.search(text):
                violations.append(f"possible credential in {relative.as_posix()}")
    assert violations == []


def test_ci_cannot_enable_or_receive_a_real_model_credential() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'PATCHPILOT_ENABLE_REAL_MODEL: "false"' in workflow
    assert 'MODEL_API_KEY: ""' in workflow
    assert "patchpilot benchmark run" in workflow


def test_release_compose_defines_healthy_postgres_and_redis_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"postgres", "redis"}
    for service in compose["services"].values():
        assert "healthcheck" in service
        assert "image" in service


def test_release_demo_can_be_prepared_as_an_isolated_git_repository(tmp_path: Path) -> None:
    source = ROOT / "examples" / "demo-repository"
    destination = tmp_path / "repository"

    prepare_repository(source, destination)

    task_data = yaml.safe_load((ROOT / "examples" / "demo-task.yaml").read_text(encoding="utf-8"))
    task_data["repository"]["path"] = str(destination)
    spec = TaskSpec.model_validate(task_data)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=destination,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    assert spec.id == "release-demo-page-boundary"
    assert status.stdout == ""
    assert (destination / "tests" / "test_cases.py").is_file()
