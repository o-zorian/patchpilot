from __future__ import annotations

import json

from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import Message, MessageRole
from patchpilot.profiles import profile_for
from patchpilot.tools.base import ToolContext
from patchpilot.tools.files import ListFilesInput, ListFilesTool

PROMPT_VERSION = "m2-v1"

SYSTEM_PROMPT = """You are PatchPilot, a controlled repository repair agent.
Use only the registered structured tools. Never invent shell commands or request credentials.
All paths are relative to the isolated Workspace. Respect allowed_paths, denied_paths, and
every budget. A finish call only requests later deterministic evaluation and never means the
task passed. Read and search before editing, run configured tests, and report remaining risk."""


def build_initial_messages(context: ToolContext) -> list[Message]:
    spec: TaskSpec = context.task_spec
    listing = ListFilesTool(context).execute(ListFilesInput(path=".", max_depth=3))
    repository_files: list[str] = []
    if listing.ok and listing.data is not None:
        raw_files = listing.data.get("files")
        if isinstance(raw_files, list):
            repository_files = [str(path) for path in raw_files]
    task_payload = {
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
        "prompt_version": PROMPT_VERSION,
    }
    return [
        Message(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
        Message(
            role=MessageRole.USER,
            content=json.dumps(task_payload, ensure_ascii=False, sort_keys=True),
        ),
    ]
