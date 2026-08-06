from __future__ import annotations

import fnmatch
import shutil
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.sandbox.local import run_argv
from patchpilot.sandbox.workspace import (
    PathPolicyError,
    normalize_logical_path,
)
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success
from patchpilot.tools.files import iter_workspace_files


class SearchCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=1_000)
    path: str = "."
    glob: str | None = Field(default=None, max_length=500)
    max_results: int = Field(default=50, ge=1, le=100)


class SearchCodeTool:
    name = "search_code"

    def __init__(self, context: ToolContext, *, rg_executable: str | None = None) -> None:
        self.context = context
        self.rg_executable = rg_executable if rg_executable is not None else shutil.which("rg")

    def execute(self, arguments: SearchCodeInput) -> ToolResult:
        started = time.monotonic()
        try:
            resolved = self.context.path_policy.resolve(
                arguments.path,
                must_exist=True,
                allow_root=True,
            )
            glob_pattern = self._validate_glob(arguments.glob)
        except PathPolicyError as exc:
            return path_failure(self.name, exc, started=started)
        if not resolved.candidate_path.is_dir():
            return failure(
                self.name,
                "NOT_A_DIRECTORY",
                f"Workspace path is not a directory: {resolved.logical_path}",
                started=started,
            )

        maximum = min(arguments.max_results, self.context.limits.search_max_results)
        if self.rg_executable:
            try:
                matches = self._search_with_ripgrep(
                    arguments.query,
                    resolved.candidate_path,
                    resolved.logical_path,
                    glob_pattern,
                    maximum,
                )
                return success(
                    self.name,
                    f"found {len(matches)} matches",
                    {"matches": matches, "count": len(matches), "backend": "ripgrep"},
                    started=started,
                    truncated=len(matches) >= maximum,
                )
            except (OSError, ValueError):
                pass

        matches, truncated = self._search_with_python(
            arguments.query,
            resolved.candidate_path,
            resolved.logical_path,
            glob_pattern,
            maximum,
        )
        return success(
            self.name,
            f"found {len(matches)} matches",
            {"matches": matches, "count": len(matches), "backend": "python"},
            started=started,
            truncated=truncated,
        )

    @staticmethod
    def _validate_glob(pattern: str | None) -> str | None:
        if pattern is None:
            return None
        normalized = normalize_logical_path(pattern, allow_root=False)
        if normalized.startswith(":"):
            raise PathPolicyError("INVALID_GLOB", "glob cannot use Git pathspec magic")
        return normalized

    def _search_with_ripgrep(
        self,
        query: str,
        directory: Path,
        base_logical: str,
        glob_pattern: str | None,
        maximum: int,
    ) -> list[dict[str, object]]:
        if self.rg_executable is None:
            raise OSError("ripgrep is unavailable")
        argv = [
            self.rg_executable,
            "--line-number",
            "--column",
            "--with-filename",
            "--color",
            "never",
            "--fixed-strings",
            "--max-columns",
            "500",
        ]
        if glob_pattern is not None:
            argv.extend(["--glob", glob_pattern])
        argv.extend(["--", query, "."])
        result = run_argv(
            argv,
            cwd=directory,
            timeout_seconds=30,
            output_max_chars=self.context.limits.output_max_chars,
        )
        if result.return_code not in {0, 1}:
            raise OSError(result.stderr or "ripgrep failed")
        matches: list[dict[str, object]] = []
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split(":", 3)
            if len(parts) != 4:
                continue
            relative, line_text, column_text, preview = parts
            local_logical = normalize_logical_path(relative.removeprefix("./"))
            logical = local_logical if base_logical == "." else f"{base_logical}/{local_logical}"
            try:
                self.context.path_policy.resolve(logical, must_exist=True)
            except PathPolicyError:
                continue
            matches.append(
                {
                    "path": logical,
                    "line": int(line_text),
                    "column": int(column_text),
                    "preview": preview[:500],
                }
            )
            if len(matches) >= maximum:
                break
        return matches

    def _search_with_python(
        self,
        query: str,
        directory: Path,
        base_logical: str,
        glob_pattern: str | None,
        maximum: int,
    ) -> tuple[list[dict[str, object]], bool]:
        logical_files = iter_workspace_files(
            self.context,
            start_path=directory,
            start_logical=base_logical,
            max_depth=None,
        )
        matches: list[dict[str, object]] = []
        for logical in logical_files:
            local_for_glob = (
                logical if base_logical == "." else logical.removeprefix(f"{base_logical}/")
            )
            if glob_pattern is not None and not fnmatch.fnmatchcase(local_for_glob, glob_pattern):
                continue
            try:
                resolved = self.context.path_policy.resolve(logical, must_exist=True)
                if resolved.candidate_path.stat().st_size > self.context.limits.max_file_bytes:
                    continue
                with resolved.candidate_path.open(
                    "r", encoding="utf-8", errors="strict"
                ) as text_file:
                    for line_number, line in enumerate(text_file, start=1):
                        column = line.find(query)
                        if column < 0:
                            continue
                        matches.append(
                            {
                                "path": logical,
                                "line": line_number,
                                "column": column + 1,
                                "preview": line.rstrip()[:500],
                            }
                        )
                        if len(matches) >= maximum:
                            return matches, True
            except (OSError, UnicodeDecodeError, PathPolicyError):
                continue
        return matches, False
