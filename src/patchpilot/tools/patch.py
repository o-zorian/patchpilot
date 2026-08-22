from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.sandbox.local import CommandResult, git_argv, run_argv
from patchpilot.sandbox.workspace import PathPolicyError
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success
from patchpilot.tools.git import collect_diff

_REJECTED_PATCH_PREFIXES = (
    "Binary files ",
    "GIT binary patch",
    "copy from ",
    "copy to ",
    "rename from ",
    "rename to ",
    "similarity index ",
)


class ApplyPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    patch: str = Field(
        min_length=1,
        max_length=1_000_000,
        description=(
            "A complete Git unified diff. Start each file section with "
            "'diff --git a/path b/path', followed by '--- a/path', '+++ b/path', "
            "and at least one '@@' hunk. Do not use '*** Begin Patch', "
            "'*** Update File', or '*** End Patch' markers. Example: "
            "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n"
            "@@ -1 +1 @@\n-old\n+new"
        ),
    )


@dataclass(frozen=True, slots=True)
class ParsedPatch:
    paths: tuple[str, ...]
    added_lines: int
    deleted_lines: int


@dataclass(frozen=True, slots=True)
class _FileBackup:
    path: Path
    existed: bool
    content: bytes | None


class PatchParseError(ValueError):
    """Raised when a patch uses an unsupported or unsafe format."""


def parse_unified_patch(patch: str) -> ParsedPatch:
    if "\x00" in patch:
        raise PatchParseError("binary or NUL-containing patches are not supported")
    paths: list[str] = []
    added_lines = 0
    deleted_lines = 0
    saw_hunk = False
    current_path: str | None = None
    for line in patch.splitlines():
        if line.startswith(_REJECTED_PATCH_PREFIXES):
            raise PatchParseError("binary, rename, and copy patches are not supported")
        if line.startswith(("old mode ", "new mode ")):
            raise PatchParseError("file mode changes are not supported")
        if line.startswith(("new file mode ", "deleted file mode ")) and not line.endswith(
            (" 100644", " 100755")
        ):
            raise PatchParseError("only regular text-file modes are supported")
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line, posix=True)
            except ValueError as exc:
                raise PatchParseError("invalid diff header quoting") from exc
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
                raise PatchParseError("each patch section must use diff --git a/path b/path")
            old_path = fields[2][2:]
            new_path = fields[3][2:]
            if old_path != new_path:
                raise PatchParseError("file renames are not supported")
            paths.append(new_path)
            current_path = new_path
        elif line.startswith(("--- ", "+++ ")):
            if current_path is None:
                raise PatchParseError("file headers must follow a diff --git header")
            try:
                fields = shlex.split(line, posix=True)
            except ValueError as exc:
                raise PatchParseError("invalid file header quoting") from exc
            if len(fields) != 2:
                raise PatchParseError("file headers cannot contain timestamps or extra fields")
            header_path = fields[1]
            if header_path == "/dev/null":
                continue
            expected_prefix = "a/" if line.startswith("--- ") else "b/"
            if not header_path.startswith(expected_prefix) or header_path[2:] != current_path:
                raise PatchParseError("file headers do not match the diff --git path")
        elif line.startswith("@@"):
            saw_hunk = True
        elif saw_hunk and line.startswith("+") and not line.startswith("+++"):
            added_lines += 1
        elif saw_hunk and line.startswith("-") and not line.startswith("---"):
            deleted_lines += 1
    if not paths or not saw_hunk:
        raise PatchParseError("patch must contain at least one unified diff hunk")
    if len(set(paths)) != len(paths):
        raise PatchParseError("a file cannot appear in multiple patch sections")
    return ParsedPatch(
        paths=tuple(paths),
        added_lines=added_lines,
        deleted_lines=deleted_lines,
    )


class ApplyPatchTool:
    name = "apply_patch"

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def execute(self, arguments: ApplyPatchInput) -> ToolResult:
        started = time.monotonic()
        try:
            parsed = parse_unified_patch(arguments.patch)
        except PatchParseError as exc:
            return failure(self.name, "INVALID_PATCH", str(exc), started=started)

        if len(parsed.paths) > self.context.task_spec.budget.max_changed_files:
            return failure(
                self.name,
                "PATCH_TOO_LARGE",
                "patch exceeds max_changed_files",
                started=started,
            )
        if (
            parsed.added_lines + parsed.deleted_lines
            > self.context.task_spec.budget.max_patch_lines
        ):
            return failure(
                self.name,
                "PATCH_TOO_LARGE",
                "patch exceeds max_patch_lines",
                started=started,
            )

        backups: list[_FileBackup] = []
        try:
            for logical in parsed.paths:
                resolved = self.context.path_policy.resolve(
                    logical,
                    for_write=True,
                    must_exist=False,
                    reject_symlinks=True,
                )
                candidate = resolved.candidate_path
                if candidate.exists() and not candidate.is_file():
                    return failure(
                        self.name,
                        "PATCH_TARGET_INVALID",
                        f"patch target is not a regular file: {logical}",
                        started=started,
                    )
                if (
                    candidate.exists()
                    and candidate.stat().st_size > self.context.limits.max_file_bytes
                ):
                    return failure(
                        self.name,
                        "PATCH_TARGET_TOO_LARGE",
                        f"patch target exceeds the atomic backup limit: {logical}",
                        started=started,
                    )
                backups.append(
                    _FileBackup(
                        path=candidate,
                        existed=candidate.exists(),
                        content=candidate.read_bytes() if candidate.exists() else None,
                    )
                )
        except PathPolicyError as exc:
            return path_failure(self.name, exc, started=started)
        except OSError as exc:
            return failure(self.name, "PATCH_BACKUP_ERROR", str(exc), started=started)

        check = self._git_apply(arguments.patch, check=True)
        if check.return_code != 0:
            return failure(
                self.name,
                "PATCH_CHECK_FAILED",
                check.stderr.strip() or "git apply --check rejected the patch",
                started=started,
            )
        applied = self._git_apply(arguments.patch, check=False)
        if applied.return_code != 0:
            self._restore(backups)
            return failure(
                self.name,
                "PATCH_APPLY_FAILED",
                applied.stderr.strip() or "git apply failed",
                started=started,
            )

        try:
            snapshot = collect_diff(self.context)
            if (
                snapshot.changed_files > self.context.task_spec.budget.max_changed_files
                or snapshot.added_lines + snapshot.deleted_lines
                > self.context.task_spec.budget.max_patch_lines
            ):
                self._restore(backups)
                return failure(
                    self.name,
                    "PATCH_TOO_LARGE",
                    "resulting Workspace diff exceeds the remaining TaskSpec patch budget",
                    started=started,
                )
        except OSError as exc:
            self._restore(backups)
            return failure(self.name, "PATCH_VERIFY_ERROR", str(exc), started=started)

        return success(
            self.name,
            f"applied patch to {len(parsed.paths)} files",
            {
                "changed_paths": list(parsed.paths),
                "patch_added_lines": parsed.added_lines,
                "patch_deleted_lines": parsed.deleted_lines,
                "workspace_changed_files": snapshot.changed_files,
                "workspace_added_lines": snapshot.added_lines,
                "workspace_deleted_lines": snapshot.deleted_lines,
            },
            started=started,
        )

    def _git_apply(self, patch: str, *, check: bool) -> CommandResult:
        arguments = ["apply", "--recount", "--whitespace=nowarn"]
        if check:
            arguments.append("--check")
        arguments.append("-")
        return run_argv(
            git_argv(arguments),
            cwd=self.context.workspace.path,
            timeout_seconds=30,
            output_max_chars=self.context.limits.output_max_chars,
            input_text=patch,
        )

    def _restore(self, backups: list[_FileBackup]) -> None:
        for backup in backups:
            if backup.existed:
                backup.path.parent.mkdir(parents=True, exist_ok=True)
                if backup.content is None:
                    raise RuntimeError("existing backup is missing content")
                backup.path.write_bytes(backup.content)
            elif backup.path.exists():
                backup.path.unlink()
                parent = backup.path.parent
                while parent != self.context.path_policy.root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
