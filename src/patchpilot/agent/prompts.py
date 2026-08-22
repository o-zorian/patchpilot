from __future__ import annotations

import json
from pathlib import Path

from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import Message, MessageRole
from patchpilot.profiles import profile_for
from patchpilot.tools.base import ToolContext
from patchpilot.tools.files import ListFilesInput, ListFilesTool

PROMPT_VERSION = "m2-v1"
REAL_BENCHMARK_PROMPT_VERSION = "real-v1"

_SNAPSHOT_MAX_FILES = 80
_SNAPSHOT_MAX_FILE_CHARS = 12_000
_SNAPSHOT_MAX_TOTAL_CHARS = 60_000

SYSTEM_PROMPT = """You are PatchPilot, a controlled repository repair agent.
Use only the registered structured tools. Never invent shell commands or request credentials.
All paths are relative to the isolated Workspace. Respect allowed_paths, denied_paths, and
every budget. A finish call only requests later deterministic evaluation and never means the
task passed. Read and search before editing, run configured tests, and report remaining risk."""


def _repository_snapshot(context: ToolContext, files: list[str]) -> list[dict[str, object]]:
    """Build a bounded, identical source view for fair cross-strategy evaluation."""

    snapshot: list[dict[str, object]] = []
    remaining = _SNAPSHOT_MAX_TOTAL_CHARS
    for logical in files[:_SNAPSHOT_MAX_FILES]:
        if remaining <= 0:
            break
        path = (context.workspace.path / Path(logical)).resolve(strict=False)
        try:
            if not path.is_file() or path.stat().st_size > context.limits.max_file_bytes:
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        bounded = text[: min(_SNAPSHOT_MAX_FILE_CHARS, remaining)]
        snapshot.append(
            {
                "path": logical,
                "content": bounded,
                "truncated": len(bounded) < len(text),
            }
        )
        remaining -= len(bounded)
    return snapshot


def build_initial_messages(
    context: ToolContext,
    *,
    prompt_version: str = PROMPT_VERSION,
    include_repository_snapshot: bool = False,
) -> list[Message]:
    spec: TaskSpec = context.task_spec
    listing = ListFilesTool(context).execute(ListFilesInput(path=".", max_depth=3))
    repository_files: list[str] = []
    if listing.ok and listing.data is not None:
        raw_files = listing.data.get("files")
        if isinstance(raw_files, list):
            repository_files = [str(path) for path in raw_files]
    task_payload: dict[str, object] = {
        "protocol_version": spec.version,
        "task_id": spec.id,
        "title": spec.title,
        "goal": spec.goal,
        "language": spec.repository.language,
        "base_ref": spec.repository.base_ref,
        "allowed_paths": spec.allowed_paths,
        "denied_paths": spec.denied_paths,
        "required_tests": spec.acceptance.required_tests,
        "test_command_ids": list(profile_for(spec).command_ids),
        "budget": spec.budget.model_dump(mode="json"),
        "workspace_baseline_commit": context.workspace.baseline_commit,
        "repository_files": repository_files,
        "repository_files_truncated": listing.truncated,
        "prompt_version": prompt_version,
    }
    if include_repository_snapshot:
        task_payload["repository_snapshot"] = _repository_snapshot(context, repository_files)
        task_payload["repository_snapshot_policy"] = {
            "same_for_all_strategies": True,
            "max_files": _SNAPSHOT_MAX_FILES,
            "max_file_chars": _SNAPSHOT_MAX_FILE_CHARS,
            "max_total_chars": _SNAPSHOT_MAX_TOTAL_CHARS,
        }
    return [
        Message(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
        Message(
            role=MessageRole.USER,
            content=json.dumps(task_payload, ensure_ascii=False, sort_keys=True),
        ),
    ]
