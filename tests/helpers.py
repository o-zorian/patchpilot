from __future__ import annotations

import subprocess
from pathlib import Path


def run_command(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def create_git_repository(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    run_command(["git", "init", "--initial-branch=main"], cwd=path)
    run_command(["git", "config", "user.name", "PatchPilot Tests"], cwd=path)
    run_command(["git", "config", "user.email", "patchpilot-tests@example.invalid"], cwd=path)
    for logical, content in files.items():
        target = path.joinpath(*logical.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_command(["git", "add", "--all"], cwd=path)
    run_command(["git", "commit", "-m", "fixture baseline"], cwd=path)
    return path
