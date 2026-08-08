from __future__ import annotations

import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchpilot.profiles.go import GO_IGNORED_DIRECTORIES
from patchpilot.profiles.python import PYTHON_IGNORED_DIRECTORIES
from patchpilot.sandbox.workspace import PathPolicyError, is_sensitive_logical_path
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success

_IGNORED_DIRECTORIES = PYTHON_IGNORED_DIRECTORIES | GO_IGNORED_DIRECTORIES


class ListFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = "."
    max_depth: int = Field(default=3, ge=1, le=5)


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> ReadFileInput:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


def _logical_child(base: str, name: str) -> str:
    return name if base == "." else f"{base}/{name}"


def iter_workspace_files(
    context: ToolContext,
    *,
    start_path: Path,
    start_logical: str,
    max_depth: int | None = None,
    file_limit: int | None = None,
) -> list[str]:
    files: list[str] = []
    stack: list[tuple[Path, str, int]] = [(start_path, start_logical, 0)]
    while stack:
        directory, logical_directory, depth = stack.pop()
        try:
            entries = sorted(
                os.scandir(directory), key=lambda entry: entry.name.lower(), reverse=True
            )
        except OSError:
            continue
        for entry in entries:
            logical = _logical_child(logical_directory, entry.name)
            if entry.name in _IGNORED_DIRECTORIES or is_sensitive_logical_path(logical):
                continue
            try:
                resolved = context.path_policy.resolve(
                    logical,
                    must_exist=True,
                    reject_symlinks=False,
                )
            except PathPolicyError:
                continue
            if entry.is_symlink() and not resolved.resolved_path.is_relative_to(
                context.path_policy.root
            ):
                continue
            if entry.is_file(follow_symlinks=False) or (
                entry.is_symlink() and resolved.resolved_path.is_file()
            ):
                files.append(logical)
                if file_limit is not None and len(files) >= file_limit:
                    return sorted(files)
            elif entry.is_dir(follow_symlinks=False):
                child_depth = depth + 1
                if max_depth is None or child_depth < max_depth:
                    stack.append((Path(entry.path), logical, child_depth))
    return sorted(files)


class ListFilesTool:
    name = "list_files"

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def execute(self, arguments: ListFilesInput) -> ToolResult:
        started = time.monotonic()
        try:
            resolved = self.context.path_policy.resolve(
                arguments.path,
                must_exist=True,
                allow_root=True,
            )
        except PathPolicyError as exc:
            return path_failure(self.name, exc, started=started)
        if not resolved.candidate_path.is_dir():
            return failure(
                self.name,
                "NOT_A_DIRECTORY",
                f"Workspace path is not a directory: {resolved.logical_path}",
                started=started,
            )

        all_files = iter_workspace_files(
            self.context,
            start_path=resolved.candidate_path,
            start_logical=resolved.logical_path,
            max_depth=arguments.max_depth,
            file_limit=self.context.limits.list_max_files + 1,
        )
        maximum = self.context.limits.list_max_files
        visible = all_files[:maximum]
        truncated = len(all_files) > maximum
        return success(
            self.name,
            f"listed {len(visible)} files under {resolved.logical_path}",
            {"path": resolved.logical_path, "files": visible, "count": len(visible)},
            started=started,
            truncated=truncated,
        )


class ReadFileTool:
    name = "read_file"

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def execute(self, arguments: ReadFileInput) -> ToolResult:
        started = time.monotonic()
        try:
            resolved = self.context.path_policy.resolve(arguments.path, must_exist=True)
        except PathPolicyError as exc:
            return path_failure(self.name, exc, started=started)
        if not resolved.candidate_path.is_file():
            return failure(
                self.name,
                "NOT_A_FILE",
                f"Workspace path is not a file: {resolved.logical_path}",
                started=started,
            )
        try:
            with resolved.candidate_path.open("rb") as binary:
                if b"\x00" in binary.read(8_192):
                    return failure(
                        self.name,
                        "BINARY_FILE",
                        f"binary files cannot be read: {resolved.logical_path}",
                        started=started,
                    )
            requested_end = arguments.end_line or (
                arguments.start_line + self.context.limits.read_max_lines - 1
            )
            effective_end = min(
                requested_end,
                arguments.start_line + self.context.limits.read_max_lines - 1,
            )
            rendered: list[str] = []
            output_chars = 0
            last_line = arguments.start_line - 1
            truncated = effective_end < requested_end
            with resolved.candidate_path.open("r", encoding="utf-8", errors="strict") as text_file:
                for line_number, line in enumerate(text_file, start=1):
                    if line_number < arguments.start_line:
                        continue
                    if line_number > effective_end:
                        truncated = True
                        break
                    rendered_line = f"{line_number:>6}: {line.rstrip(chr(10)).rstrip(chr(13))}\n"
                    if output_chars + len(rendered_line) > self.context.limits.output_max_chars:
                        truncated = True
                        break
                    rendered.append(rendered_line)
                    output_chars += len(rendered_line)
                    last_line = line_number
        except UnicodeDecodeError:
            return failure(
                self.name,
                "UNSUPPORTED_ENCODING",
                f"file is not valid UTF-8 text: {resolved.logical_path}",
                started=started,
            )
        except OSError as exc:
            return failure(self.name, "FILE_READ_ERROR", str(exc), started=started)

        return success(
            self.name,
            f"read {resolved.logical_path} lines {arguments.start_line}-{last_line}",
            {
                "path": resolved.logical_path,
                "start_line": arguments.start_line,
                "end_line": last_line,
                "content": "".join(rendered),
            },
            started=started,
            truncated=truncated,
        )
