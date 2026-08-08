from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run(argv: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(argv, cwd=cwd, check=False, shell=False)
    if completed.returncode != 0:
        raise SystemExit(f"command failed with exit code {completed.returncode}: {argv[0]}")


def prepare_repository(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SystemExit(
            f"demo repository already exists: {destination}\n"
            "Remove demo-work after inspecting any prior run, then prepare it again."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    run(["git", "init", "--initial-branch=main"], cwd=destination)
    run(["git", "add", "--all"], cwd=destination)
    run(
        [
            "git",
            "-c",
            "user.name=PatchPilot Demo",
            "-c",
            "user.email=demo@patchpilot.invalid",
            "commit",
            "-m",
            "demo baseline",
        ],
        cwd=destination,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "examples" / "demo-repository"
    destination = root / "demo-work" / "repository"
    prepare_repository(source, destination)
    print(f"Prepared isolated demo repository: {destination}")
    print(f"TaskSpec: {root / 'examples' / 'demo-task.yaml'}")


if __name__ == "__main__":
    main()
