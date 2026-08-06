from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from patchpilot.sandbox.local import CommandResult, git_argv, run_argv

_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        "credentials",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".pem"})


class WorkspaceError(ValueError):
    """Raised when Workspace isolation cannot be guaranteed."""


class PathPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    path: Path
    source_path: Path
    baseline_commit: str
    initial_git_status: str


@dataclass(frozen=True, slots=True)
class ResolvedWorkspacePath:
    logical_path: str
    candidate_path: Path
    resolved_path: Path


def normalize_logical_path(value: str, *, allow_root: bool = False) -> str:
    if "\x00" in value:
        raise PathPolicyError("INVALID_PATH", "path cannot contain NUL bytes")
    normalized = value.replace("\\", "/").strip()
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise PathPolicyError("INVALID_PATH", "path must identify a Workspace entry")
    if (
        normalized.startswith(("/", ":"))
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise PathPolicyError("PATH_OUTSIDE_WORKSPACE", "path must remain inside the Workspace")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts:
        if allow_root:
            return "."
        raise PathPolicyError("INVALID_PATH", "path must identify a Workspace entry")
    return PurePosixPath(*parts).as_posix()


def is_sensitive_logical_path(logical_path: str) -> bool:
    for part in PurePosixPath(logical_path).parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_NAMES:
            return True
        if lowered.startswith(".env.") and lowered != ".env.example":
            return True
        if any(lowered.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
            return True
    return False


def glob_matches(logical_path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    return fnmatch.fnmatchcase(logical_path, normalized_pattern)


class WorkspacePathPolicy:
    def __init__(
        self,
        workspace: Workspace,
        *,
        allowed_paths: list[str],
        denied_paths: list[str],
    ) -> None:
        self.workspace = workspace
        self.root = workspace.path.resolve(strict=True)
        self.allowed_paths = tuple(pattern.replace("\\", "/") for pattern in allowed_paths)
        self.denied_paths = tuple(pattern.replace("\\", "/") for pattern in denied_paths)

    def resolve(
        self,
        path: str,
        *,
        for_write: bool = False,
        must_exist: bool = True,
        allow_root: bool = False,
        reject_symlinks: bool = False,
    ) -> ResolvedWorkspacePath:
        logical = normalize_logical_path(path, allow_root=allow_root)
        if logical == ".":
            candidate = self.root
        else:
            candidate = self.root.joinpath(*PurePosixPath(logical).parts)

        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PathPolicyError(
                "SYMLINK_ESCAPE",
                f"path resolves outside the Workspace: {logical}",
            )
        if logical == ".git" or logical.startswith(".git/"):
            raise PathPolicyError("PATH_DENIED", "Git metadata is not accessible through tools")
        if is_sensitive_logical_path(logical):
            raise PathPolicyError(
                "SENSITIVE_PATH", "sensitive files are not accessible through tools"
            )
        if reject_symlinks:
            self._reject_symlink_components(candidate)
        if must_exist and not candidate.exists():
            raise PathPolicyError("PATH_NOT_FOUND", f"Workspace path does not exist: {logical}")
        if for_write:
            if not any(glob_matches(logical, pattern) for pattern in self.allowed_paths):
                raise PathPolicyError(
                    "PATH_NOT_ALLOWED",
                    f"path is outside allowed_paths: {logical}",
                )
            if any(glob_matches(logical, pattern) for pattern in self.denied_paths):
                raise PathPolicyError("PATH_DENIED", f"path matches denied_paths: {logical}")
        return ResolvedWorkspacePath(
            logical_path=logical,
            candidate_path=candidate,
            resolved_path=resolved,
        )

    def _reject_symlink_components(self, candidate: Path) -> None:
        current = self.root
        try:
            relative_parts = candidate.relative_to(self.root).parts
        except ValueError as exc:
            raise PathPolicyError(
                "PATH_OUTSIDE_WORKSPACE", "path is outside the Workspace"
            ) from exc
        for part in relative_parts:
            current /= part
            if current.is_symlink():
                raise PathPolicyError(
                    "SYMLINK_WRITE_DENIED",
                    "patch targets cannot contain symbolic links",
                )


class WorkspaceManager:
    def __init__(self, workspace_root: Path, *, command_timeout_seconds: int = 60) -> None:
        self.root = workspace_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_timeout_seconds = command_timeout_seconds

    def create(self, source_repository: Path, *, workspace_id: str, base_ref: str) -> Workspace:
        if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise WorkspaceError("workspace_id contains unsupported characters")
        source = source_repository.expanduser().resolve(strict=True)
        if not source.is_dir() or not (source / ".git").exists():
            raise WorkspaceError("source repository must contain Git metadata")

        destination = (self.root / workspace_id).resolve(strict=False)
        if destination.parent != self.root or not destination.is_relative_to(self.root):
            raise WorkspaceError("Workspace destination is outside WORKSPACE_ROOT")
        if destination.exists():
            raise WorkspaceError(f"Workspace already exists: {workspace_id}")

        status = self._git(source, ["status", "--porcelain=v1", "--untracked-files=all"])
        if status.return_code != 0:
            raise WorkspaceError(f"cannot inspect source repository: {status.stderr.strip()}")
        if status.stdout:
            raise WorkspaceError(
                f"source repository must be clean before Workspace creation: {status.stdout!r}"
            )
        revision = self._git(
            source,
            ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
        )
        if revision.return_code != 0:
            raise WorkspaceError(f"base_ref does not resolve to a commit: {base_ref}")
        baseline_commit = revision.stdout.strip()

        try:
            clone = run_argv(
                git_argv(
                    [
                        "clone",
                        "--no-local",
                        "--no-hardlinks",
                        "--no-checkout",
                        "--",
                        str(source),
                        str(destination),
                    ]
                ),
                cwd=self.root,
                timeout_seconds=self.command_timeout_seconds,
                output_max_chars=20_000,
            )
            if clone.return_code != 0:
                raise WorkspaceError(f"failed to clone Workspace: {clone.stderr.strip()}")
            checkout = self._git(destination, ["checkout", "--detach", baseline_commit])
            if checkout.return_code != 0:
                raise WorkspaceError(
                    f"failed to checkout Workspace baseline: {checkout.stderr.strip()}"
                )
            workspace_status = self._git(
                destination,
                ["status", "--porcelain=v1", "--untracked-files=all"],
            )
            if workspace_status.return_code != 0 or workspace_status.stdout:
                raise WorkspaceError("new Workspace is not clean")
            workspace_revision = self._git(destination, ["rev-parse", "HEAD"])
            if workspace_revision.stdout.strip() != baseline_commit:
                raise WorkspaceError("Workspace baseline does not match the requested commit")
            source_after = self._git(
                source,
                ["status", "--porcelain=v1", "--untracked-files=all"],
            )
            if source_after.return_code != 0 or source_after.stdout:
                raise WorkspaceError("source repository changed during Workspace creation")
        except Exception:
            if destination.exists():
                self._remove_tree(destination)
            raise

        return Workspace(
            id=workspace_id,
            path=destination,
            source_path=source,
            baseline_commit=baseline_commit,
            initial_git_status=workspace_status.stdout,
        )

    def cleanup(self, workspace: Workspace) -> None:
        target = workspace.path.resolve(strict=False)
        if target.parent != self.root or not target.is_relative_to(self.root):
            raise WorkspaceError("refusing to clean a path outside WORKSPACE_ROOT")
        if target.exists():
            self._remove_tree(target)

    def _git(self, cwd: Path, arguments: list[str]) -> CommandResult:
        return run_argv(
            git_argv(arguments),
            cwd=cwd,
            timeout_seconds=self.command_timeout_seconds,
            output_max_chars=20_000,
        )

    @staticmethod
    def _remove_tree(target: Path) -> None:
        if target.is_symlink():
            target.unlink()
        else:
            for directory, directories, files in os.walk(target, topdown=False):
                for name in [*directories, *files]:
                    entry = Path(directory) / name
                    if not entry.is_symlink():
                        entry.chmod(stat.S_IREAD | stat.S_IWRITE)
            shutil.rmtree(target)
