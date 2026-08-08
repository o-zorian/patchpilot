from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from patchpilot.domain.cancellation import CancellationToken
from patchpilot.domain.task import TaskSpec
from patchpilot.sandbox.trusted_local import TrustedLocalSandbox
from patchpilot.sandbox.workspace import WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits
from patchpilot.tools.files import ListFilesInput, ListFilesTool, ReadFileInput, ReadFileTool
from patchpilot.tools.git import GitDiffInput, GitDiffTool
from patchpilot.tools.patch import ApplyPatchInput, ApplyPatchTool
from patchpilot.tools.search import SearchCodeInput, SearchCodeTool
from patchpilot.tools.tests import RunTestsInput, RunTestsTool
from tests.helpers import create_git_repository


def make_context(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    *,
    files: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> tuple[ToolContext, Path]:
    repository = create_git_repository(
        tmp_path / "source",
        files
        or {
            "calculator.py": "def add(left: int, right: int) -> int:\n    return left - right\n",
            "tests/test_calculator.py": (
                "from calculator import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    valid_task_data["repository"]["path"] = str(repository)
    valid_task_data["allowed_paths"] = ["calculator.py", "tests/**"]
    valid_task_data["denied_paths"] = [".git/**", "README.md"]
    valid_task_data["acceptance"]["commands"] = [
        {
            "argv": ["python", "-m", "pytest"],
            "timeout_seconds": timeout_seconds,
        }
    ]
    spec = TaskSpec.model_validate(valid_task_data)
    workspace = WorkspaceManager(tmp_path / "workspaces").create(
        repository,
        workspace_id="tool-run",
        base_ref="main",
    )
    context = ToolContext.create(
        workspace,
        spec,
        ToolLimits(output_max_chars=20_000, max_file_bytes=1_000_000),
        command_sandbox=TrustedLocalSandbox(),
    )
    return context, repository


def test_controlled_tools_modify_only_workspace_and_run_pytest(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, source = make_context(tmp_path, valid_task_data)
    original = (source / "calculator.py").read_text(encoding="utf-8")

    listed = ListFilesTool(context).execute(ListFilesInput(path=".", max_depth=3))
    searched = SearchCodeTool(context, rg_executable="").execute(
        SearchCodeInput(query="left - right", path=".")
    )
    read = ReadFileTool(context).execute(ReadFileInput(path="calculator.py"))
    patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
"""
    applied = ApplyPatchTool(context).execute(ApplyPatchInput(patch=patch))
    tests = RunTestsTool(context).execute(RunTestsInput(profile_command_id="acceptance:0"))
    diff = GitDiffTool(context).execute(GitDiffInput())

    assert listed.data is not None
    assert searched.data is not None
    assert read.data is not None
    assert tests.data is not None
    assert diff.data is not None
    assert listed.ok and "calculator.py" in listed.data["files"]
    assert searched.ok and searched.data["backend"] == "python"
    assert searched.data["matches"][0]["path"] == "calculator.py"
    assert read.ok and "return left - right" in read.data["content"]
    assert applied.ok
    assert tests.ok and tests.data["passed"] is True
    assert diff.ok and "return left + right" in diff.data["diff"]
    assert (source / "calculator.py").read_text(encoding="utf-8") == original


def test_out_of_scope_and_over_budget_patches_are_atomic(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    calculator_before = (context.workspace.path / "calculator.py").read_bytes()
    out_of_scope_patch = """diff --git a/README.md b/README.md
--- /dev/null
+++ b/README.md
@@ -0,0 +1 @@
+not allowed
"""
    denied = ApplyPatchTool(context).execute(ApplyPatchInput(patch=out_of_scope_patch))

    budget_data = valid_task_data.copy()
    budget_data["budget"] = dict(valid_task_data["budget"])
    budget_data["budget"]["max_patch_lines"] = 1
    limited_context = ToolContext.create(
        context.workspace,
        TaskSpec.model_validate(budget_data),
        context.limits,
        command_sandbox=context.command_sandbox,
    )
    oversized_patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
-def add(left: int, right: int) -> int:
+def add(left: int, right: int) -> int:
     return left - right
"""
    oversized = ApplyPatchTool(limited_context).execute(ApplyPatchInput(patch=oversized_patch))

    assert denied.error is not None
    assert oversized.error is not None
    assert denied.ok is False
    assert denied.error.code == "PATH_NOT_ALLOWED"
    assert oversized.ok is False
    assert oversized.error.code == "PATCH_TOO_LARGE"
    assert (context.workspace.path / "calculator.py").read_bytes() == calculator_before


def test_patch_supports_text_creation_and_deletion(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, source = make_context(tmp_path, valid_task_data)
    patch = """diff --git a/tests/test_regression.py b/tests/test_regression.py
new file mode 100644
--- /dev/null
+++ b/tests/test_regression.py
@@ -0,0 +1,2 @@
+def test_regression() -> None:
+    assert True
diff --git a/calculator.py b/calculator.py
deleted file mode 100644
--- a/calculator.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def add(left: int, right: int) -> int:
-    return left - right
"""

    applied = ApplyPatchTool(context).execute(ApplyPatchInput(patch=patch))
    diff = GitDiffTool(context).execute(GitDiffInput())

    assert applied.ok
    assert diff.data is not None
    assert not (context.workspace.path / "calculator.py").exists()
    assert (context.workspace.path / "tests" / "test_regression.py").is_file()
    assert diff.data["changed_files"] == 2
    assert (source / "calculator.py").is_file()


def test_multi_file_patch_check_failure_leaves_no_partial_changes(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    calculator = context.workspace.path / "calculator.py"
    test_file = context.workspace.path / "tests" / "test_calculator.py"
    before = (calculator.read_bytes(), test_file.read_bytes())
    patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
diff --git a/tests/test_calculator.py b/tests/test_calculator.py
--- a/tests/test_calculator.py
+++ b/tests/test_calculator.py
@@ -99,1 +99,1 @@
-this context does not exist
+therefore the complete patch must fail
"""

    result = ApplyPatchTool(context).execute(ApplyPatchInput(patch=patch))

    assert result.error is not None
    assert result.ok is False
    assert result.error.code == "PATCH_CHECK_FAILED"
    assert (calculator.read_bytes(), test_file.read_bytes()) == before


def test_unknown_command_is_rejected(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)

    result = RunTestsTool(context).execute(RunTestsInput(profile_command_id="shell:0"))

    assert result.error is not None
    assert result.ok is False
    assert result.error.code == "COMMAND_NOT_ALLOWED"


def test_test_process_does_not_inherit_model_secrets(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "must-not-enter-workspace-process")
    context, _ = make_context(
        tmp_path,
        valid_task_data,
        files={
            "calculator.py": "VALUE = 1\n",
            "tests/test_environment.py": (
                "import os\n\n"
                "def test_model_key_is_absent() -> None:\n"
                "    assert 'MODEL_API_KEY' not in os.environ\n"
            ),
        },
    )

    result = RunTestsTool(context).execute(RunTestsInput(profile_command_id="acceptance:0"))

    assert result.data is not None
    assert result.ok
    assert result.data["passed"] is True


def test_test_timeout_terminates_process_tree(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    marker = tmp_path / "escaped-child.json"
    child_code = (
        "import json,time; "
        "time.sleep(2); "
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(json.dumps({{'alive': True}}))"
    )
    slow_test = (
        "import subprocess\n"
        "import sys\n"
        "import time\n\n"
        "def test_slow() -> None:\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "    time.sleep(20)\n"
    )
    context, _ = make_context(
        tmp_path,
        valid_task_data,
        files={"calculator.py": "VALUE = 1\n", "tests/test_slow.py": slow_test},
        timeout_seconds=1,
    )

    result = RunTestsTool(context).execute(RunTestsInput(profile_command_id="acceptance:0"))
    time.sleep(2.5)

    assert result.error is not None
    assert result.data is not None
    assert result.ok is False
    assert result.error.code == "TIMEOUT"
    assert result.data["timed_out"] is True
    assert not marker.exists(), json.loads(marker.read_text()) if marker.exists() else ""


def test_test_cancellation_terminates_process_tree(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    marker = tmp_path / "cancelled-child.json"
    child_code = (
        "import json,time; "
        "time.sleep(2); "
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(json.dumps({{'alive': True}}))"
    )
    slow_test = (
        "import subprocess\n"
        "import sys\n"
        "import time\n\n"
        "def test_slow() -> None:\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "    time.sleep(20)\n"
    )
    context, _ = make_context(
        tmp_path,
        valid_task_data,
        files={"calculator.py": "VALUE = 1\n", "tests/test_slow.py": slow_test},
    )
    token = CancellationToken()
    context = ToolContext.create(
        context.workspace,
        context.task_spec,
        context.limits,
        cancellation_token=token,
        command_sandbox=context.command_sandbox,
    )
    timer = threading.Timer(0.25, token.cancel)
    timer.start()
    try:
        result = RunTestsTool(context).execute(RunTestsInput(profile_command_id="acceptance:0"))
    finally:
        timer.cancel()
    time.sleep(2.5)

    assert result.error is not None
    assert result.data is not None
    assert result.error.code == "CANCELLED"
    assert result.data["cancelled"] is True
    assert not marker.exists(), json.loads(marker.read_text()) if marker.exists() else ""
