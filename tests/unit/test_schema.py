from __future__ import annotations

import json
from pathlib import Path

from patchpilot.domain.task import task_spec_json_schema


def test_checked_in_schema_matches_model() -> None:
    schema_path = Path("schemas/task-spec-v1.json")
    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))

    assert checked_in == task_spec_json_schema()
