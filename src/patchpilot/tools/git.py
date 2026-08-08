from __future__ import annotations

import difflib
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from patchpilot.sandbox.local import CommandResult, git_argv, run_argv
from patchpilot.sandbox.workspace import PathPolicyError
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success


class GitDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str | None = None
    stat_only: bool = False


@dataclass(frozen=True, slots=True)
class DiffSnapshot:
    diff: str
    changed_paths: tuple[str, ...]
    binary_files: tuple[str, ...]
    changed_files: int
    added_lines: int
    deleted_lines: int
    truncated: bool


def _git(
    context: ToolContext,
    arguments: list[str],
    *,
    output_max_chars: int | None = None,
) -> CommandResult:
    return run_argv(
        git_argv(arguments),
        cwd=context.workspace.path,
        timeout_seconds=30,
        output_max_chars=output_max_chars or context.limits.output_max_chars,
    )


def collect_diff(
    context: ToolContext,
    path: str | None = None,
    *,
    output_max_chars: int | None = None,
) -> DiffSnapshot:
    path_arguments = [] if path is None else ["--", path]
    maximum_output = output_max_chars or context.limits.output_max_chars
    tracked = _git(
        context,
        [
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            context.workspace.baseline_commit,
            *path_arguments,
        ],
        output_max_chars=maximum_output,
    )
    if tracked.return_code != 0:
        raise OSError(tracked.stderr or "git diff failed")
    numstat = _git(
        context,
        [
            "diff",
            "--no-renames",
            "--numstat",
            context.workspace.baseline_commit,
            *path_arguments,
        ],
        output_max_chars=maximum_output,
    )
    if numstat.return_code != 0:
        raise OSError(numstat.stderr or "git diff --numstat failed")

    changed: set[str] = set()
    binary_files: set[str] = set()
    added_lines = 0
    deleted_lines = 0
    for row in numstat.stdout.splitlines():
        fields = row.split("\t", 2)
        if len(fields) != 3:
            continue
        added, deleted, logical = fields
        changed.add(logical)
        if added.isdigit():
            added_lines += int(added)
        else:
            binary_files.add(logical)
        if deleted.isdigit():
            deleted_lines += int(deleted)
        else:
            binary_files.add(logical)

    untracked_arguments = ["ls-files", "--others", "--exclude-standard", "-z"]
    if path is not None:
        untracked_arguments.extend(["--", path])
    untracked_result = _git(
        context,
        untracked_arguments,
        output_max_chars=max(context.limits.output_max_chars, 100_000),
    )
    if untracked_result.return_code != 0:
        raise OSError(untracked_result.stderr or "git ls-files failed")

    diff_parts = [tracked.stdout]
    truncated = tracked.truncated or untracked_result.truncated
    for logical in filter(None, untracked_result.stdout.split("\x00")):
        try:
            resolved = context.path_policy.resolve(logical, must_exist=True)
        except PathPolicyError:
            continue
        if not resolved.candidate_path.is_file():
            continue
        changed.add(logical)
        try:
            content = resolved.candidate_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            binary_files.add(logical)
            diff_parts.append(f"diff --git a/{logical} b/{logical}\nBinary files differ\n")
            continue
        lines = content.splitlines(keepends=True)
        added_lines += len(lines)
        header = f"diff --git a/{logical} b/{logical}\nnew file mode 100644\n"
        generated = "".join(
            difflib.unified_diff(
                [],
                lines,
                fromfile="/dev/null",
                tofile=f"b/{logical}",
                lineterm="\n",
            )
        )
        diff_parts.append(header + generated)

    combined = "".join(diff_parts)
    if len(combined) > maximum_output:
        combined = combined[:maximum_output]
        truncated = True
    return DiffSnapshot(
        diff=combined,
        changed_paths=tuple(sorted(changed)),
        binary_files=tuple(sorted(binary_files)),
        changed_files=len(changed),
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        truncated=truncated,
    )


class GitDiffTool:
    name = "git_diff"

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def execute(self, arguments: GitDiffInput) -> ToolResult:
        started = time.monotonic()
        logical_path: str | None = None
        if arguments.path is not None:
            try:
                resolved = self.context.path_policy.resolve(
                    arguments.path,
                    must_exist=False,
                    allow_root=True,
                )
                logical_path = resolved.logical_path
            except PathPolicyError as exc:
                return path_failure(self.name, exc, started=started)
        try:
            snapshot = collect_diff(self.context, logical_path)
        except OSError as exc:
            return failure(self.name, "GIT_DIFF_ERROR", str(exc), started=started)

        data: dict[str, object] = {
            "changed_files": snapshot.changed_files,
            "added_lines": snapshot.added_lines,
            "deleted_lines": snapshot.deleted_lines,
        }
        if not arguments.stat_only:
            data["diff"] = snapshot.diff
        return success(
            self.name,
            (
                f"{snapshot.changed_files} changed files, "
                f"+{snapshot.added_lines}/-{snapshot.deleted_lines} lines"
            ),
            data,
            started=started,
            truncated=snapshot.truncated,
        )
