from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from patchpilot.agent.strategies import policy_for
from patchpilot.benchmark.models import MANDATORY_STRATEGIES, BenchmarkError, load_benchmark
from patchpilot.domain.run import RunStrategy


def suite_path() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks" / "local-v1"


def test_local_v1_has_required_scale_languages_difficulties_and_defects() -> None:
    suite = load_benchmark(suite_path())

    assert len(suite.tasks) == 20
    assert sum(task.language == "python" for task in suite.tasks) == 12
    assert sum(task.language == "go" for task in suite.tasks) == 8
    assert sum(task.difficulty == "easy" for task in suite.tasks) == 10
    assert sum(task.difficulty == "medium" for task in suite.tasks) == 10
    assert len({task.defect for task in suite.tasks}) == 9
    assert tuple(suite.manifest.strategies) == MANDATORY_STRATEGIES
    assert suite.tasks[0].spec.id == "py-001"


def test_benchmark_inputs_are_outside_agent_repository_templates() -> None:
    suite = load_benchmark(suite_path())

    for task in suite.tasks:
        assert not task.hidden_test.is_relative_to(task.repository_template)
        assert not task.scripted_patch.is_relative_to(task.repository_template)
        assert not (task.repository_template / task.hidden_target).exists()
        assert not (task.repository_template / ".git").exists()


def test_strategy_policies_have_expected_capability_boundaries() -> None:
    single = policy_for(RunStrategy.SINGLE_SHOT)
    loop = policy_for(RunStrategy.AGENT_LOOP)
    with_tests = policy_for(RunStrategy.AGENT_LOOP_WITH_TESTS)
    full = policy_for(RunStrategy.FULL)

    assert single.max_model_calls == 1
    assert "run_tests" not in loop.allowed_tools
    assert loop.gate_feedback is False
    assert "run_tests" in with_tests.allowed_tools
    assert with_tests.gate_feedback is True
    assert "search_code" in full.allowed_tools
    assert full.compact_context is True


def test_benchmark_scale_validation_rejects_filtered_fixture(tmp_path: Path) -> None:
    target = tmp_path / "mini"
    shutil.copytree(suite_path(), target)
    for task_path in (target / "tasks").glob("*.yaml"):
        if task_path.name != "py-001.yaml":
            task_path.unlink()
    with pytest.raises(BenchmarkError, match="at least 20 tasks"):
        load_benchmark(target)
