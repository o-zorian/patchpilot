from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from patchpilot.benchmark.runner import run_benchmark
from patchpilot.domain.run import RunStrategy


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.mark.asyncio
async def test_minimal_benchmark_is_offline_isolated_and_writes_all_reports(
    tmp_path: Path,
) -> None:
    suite = Path(__file__).resolve().parents[2] / "benchmarks" / "local-v1"
    repositories = suite / "repos"
    before = _tree_hash(repositories)

    result = await run_benchmark(
        suite,
        tmp_path / "result",
        strategies=(RunStrategy.FULL,),
        limit=1,
        concurrency=1,
    )

    assert result.summary.overall.runs == 1
    assert result.summary.overall.pass_rate == 1
    assert result.summary.experiment["network_model_calls"] is False
    assert _tree_hash(repositories) == before
    assert not (tmp_path / "result" / ".work").exists()
    for name in ("raw.jsonl", "summary.json", "report.md", "report.html"):
        assert (tmp_path / "result" / name).is_file()

    record = result.records[0]
    event_path = tmp_path / "result" / record.artifact_directory / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    event_text = event_path.read_text(encoding="utf-8")
    assert "context.compacted" in {event["type"] for event in events}
    assert "test_patchpilot_hidden.py" not in event_text
    assert "hidden_tests" not in event_text
