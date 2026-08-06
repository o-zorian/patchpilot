from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.sandbox.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import create_git_repository, run_command


def test_workspace_is_independent_and_source_remains_unchanged(tmp_path: Path) -> None:
    source = create_git_repository(
        tmp_path / "source",
        {"service.py": "VALUE = 1\n", "tests/test_service.py": "def test_value(): pass\n"},
    )
    source_before = (source / "service.py").read_bytes()
    manager = WorkspaceManager(tmp_path / "workspaces")

    workspace = manager.create(source, workspace_id="run-001", base_ref="main")
    (workspace.path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert workspace.path.parent == (tmp_path / "workspaces").resolve()
    assert (source / "service.py").read_bytes() == source_before
    assert run_command(["git", "status", "--porcelain"], cwd=source).stdout == ""
    assert run_command(["git", "status", "--porcelain"], cwd=workspace.path).stdout

    manager.cleanup(workspace)
    assert not workspace.path.exists()
    assert source.is_dir()


def test_workspace_rejects_dirty_source_repository(tmp_path: Path) -> None:
    source = create_git_repository(tmp_path / "source", {"service.py": "VALUE = 1\n"})
    (source / "service.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="must be clean"):
        WorkspaceManager(tmp_path / "workspaces").create(
            source,
            workspace_id="run-002",
            base_ref="main",
        )
