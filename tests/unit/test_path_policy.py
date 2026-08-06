from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.sandbox.workspace import (
    PathPolicyError,
    Workspace,
    WorkspacePathPolicy,
    glob_matches,
)


def make_policy(tmp_path: Path) -> tuple[WorkspacePathPolicy, Path]:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = Workspace(
        id="unit-workspace",
        path=root,
        source_path=tmp_path / "source",
        baseline_commit="a" * 40,
        initial_git_status="",
    )
    return (
        WorkspacePathPolicy(
            workspace,
            allowed_paths=["src/**"],
            denied_paths=[".git/**", "src/denied.py"],
        ),
        root,
    )


def test_path_policy_allows_in_workspace_read_and_scoped_write(tmp_path: Path) -> None:
    policy, root = make_policy(tmp_path)

    read = policy.resolve("src/service.py")
    write = policy.resolve("src/new.py", for_write=True, must_exist=False)

    assert read.candidate_path == root / "src" / "service.py"
    assert write.logical_path == "src/new.py"


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/outside.py", "C:/outside.py", ":/git-magic"],
)
def test_path_policy_rejects_escape_syntax(tmp_path: Path, path: str) -> None:
    policy, _ = make_policy(tmp_path)

    with pytest.raises(PathPolicyError, match="inside the Workspace"):
        policy.resolve(path, must_exist=False)


def test_path_policy_rejects_disallowed_and_denied_writes(tmp_path: Path) -> None:
    policy, _ = make_policy(tmp_path)

    with pytest.raises(PathPolicyError) as outside:
        policy.resolve("README.md", for_write=True, must_exist=False)
    with pytest.raises(PathPolicyError) as denied:
        policy.resolve("src/denied.py", for_write=True, must_exist=False)

    assert outside.value.code == "PATH_NOT_ALLOWED"
    assert denied.value.code == "PATH_DENIED"


def test_path_policy_rejects_git_and_secret_reads(tmp_path: Path) -> None:
    policy, root = make_policy(tmp_path)
    (root / ".git").mkdir()
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    with pytest.raises(PathPolicyError) as git_error:
        policy.resolve(".git/config", must_exist=False)
    with pytest.raises(PathPolicyError) as secret_error:
        policy.resolve(".env")

    assert git_error.value.code == "PATH_DENIED"
    assert secret_error.value.code == "SENSITIVE_PATH"


def test_path_policy_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, root = make_policy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_target = outside / "secret.py"
    escaped_target.write_text("SECRET = True\n", encoding="utf-8")
    candidate = root / "src" / "outside-link" / "secret.py"
    original_resolve = Path.resolve

    def resolve_as_external(self: Path, strict: bool = False) -> Path:
        if self == candidate:
            return escaped_target
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_as_external)

    with pytest.raises(PathPolicyError) as captured:
        policy.resolve("src/outside-link/secret.py")

    assert captured.value.code == "SYMLINK_ESCAPE"


def test_patch_writes_reject_even_internal_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, root = make_policy(tmp_path)
    link = root / "src" / "service-link.py"
    original_is_symlink = Path.is_symlink

    def report_internal_link(self: Path) -> bool:
        return self == link or original_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", report_internal_link)

    with pytest.raises(PathPolicyError) as captured:
        policy.resolve(
            "src/service-link.py",
            for_write=True,
            must_exist=False,
            reject_symlinks=True,
        )

    assert captured.value.code == "SYMLINK_WRITE_DENIED"


def test_glob_matching_uses_logical_forward_slash_paths() -> None:
    assert glob_matches("src/package/service.py", "src/**")
    assert glob_matches("tests/unit/test_service.py", "tests/**/*_service.py")
    assert not glob_matches("README.md", "src/**")
