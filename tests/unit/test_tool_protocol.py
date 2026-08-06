from __future__ import annotations

import pytest
from pydantic import ValidationError

from patchpilot.tools.base import ToolResult
from patchpilot.tools.files import ListFilesInput, ReadFileInput
from patchpilot.tools.patch import ApplyPatchInput, PatchParseError, parse_unified_patch
from patchpilot.tools.search import SearchCodeInput
from patchpilot.tools.tests import RunTestsInput


def test_tool_inputs_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReadFileInput.model_validate({"path": "x.py", "arbitrary_shell": "cmd /c whoami"})


def test_tool_input_json_schemas_are_strict_objects() -> None:
    for model in (ListFilesInput, ReadFileInput, SearchCodeInput, ApplyPatchInput, RunTestsInput):
        assert model.model_json_schema()["additionalProperties"] is False


def test_tool_result_requires_consistent_error_shape() -> None:
    with pytest.raises(ValidationError, match="failed results must have errors"):
        ToolResult(
            ok=False,
            tool="read_file",
            summary="failed",
            data=None,
            error=None,
            duration_ms=1,
        )


def test_patch_parser_counts_text_changes() -> None:
    parsed = parse_unified_patch(
        """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
-OLD = 1
+NEW = 1
+EXTRA = 2
"""
    )

    assert parsed.paths == ("src/a.py",)
    assert parsed.added_lines == 2
    assert parsed.deleted_lines == 1


@pytest.mark.parametrize(
    "patch",
    [
        "GIT binary patch\n",
        "diff --git a/old.py b/new.py\n@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/link b/link\nnew file mode 120000\n@@ -0,0 +1 @@\n+target\n",
        (
            "diff --git a/allowed.py b/allowed.py\n"
            "--- a/allowed.py\n"
            "+++ b/outside.py\n"
            "@@ -1 +1 @@\n-a\n+b\n"
        ),
    ],
)
def test_patch_parser_rejects_unsupported_patch_types(patch: str) -> None:
    with pytest.raises(PatchParseError):
        parse_unified_patch(patch)
