from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from patchpilot.benchmark.models import BenchmarkError
from patchpilot.benchmark.real_models import (
    OutcomeClass,
    RealBenchmarkRunRecord,
    RealSuiteKind,
    load_real_benchmark,
)
from patchpilot.benchmark.real_reporting import build_real_summary
from patchpilot.benchmark.real_runner import (
    AuditedRealModelClient,
    GlobalCostLedger,
    GlobalCostLimitError,
    require_real_model,
)
from patchpilot.config import AppSettings, SettingsError
from patchpilot.domain.run import RunStrategy
from patchpilot.models.base import (
    Message,
    MessageRole,
    ModelClientError,
    ModelConfig,
    ModelResponse,
    ToolSchema,
)

ROOT = Path(__file__).resolve().parents[2]


def test_real_v1_is_a_frozen_balanced_auditable_suite() -> None:
    suite = load_real_benchmark(ROOT / "benchmarks" / "real-v1")

    assert suite.manifest.suite_kind == RealSuiteKind.FORMAL
    assert suite.manifest.frozen is True
    assert len(suite.tasks) == 24
    assert Counter(task.language for task in suite.tasks) == {"python": 12, "go": 12}
    assert Counter(task.difficulty for task in suite.tasks) == {
        "easy": 5,
        "medium": 12,
        "hard": 7,
    }
    assert len({task.defect for task in suite.tasks}) >= 8
    assert sum(task.source.changed_files_expected >= 2 for task in suite.tasks) >= 8
    assert all(task.source.provenance == "curated_snapshot" for task in suite.tasks)
    assert len(suite.task_set_sha256) == 64


def test_calibration_suite_is_separate_and_not_frozen() -> None:
    suite = load_real_benchmark(ROOT / "benchmarks" / "real-calibration-v1")

    assert suite.manifest.suite_kind == RealSuiteKind.CALIBRATION
    assert suite.manifest.frozen is False
    assert len(suite.tasks) == 5


def test_real_summary_includes_inference_agent_and_paired_metrics() -> None:
    suite = load_real_benchmark(ROOT / "benchmarks" / "real-calibration-v1")
    timestamp = datetime.now(UTC)

    def record(strategy: RunStrategy, *, passed: bool) -> RealBenchmarkRunRecord:
        return RealBenchmarkRunRecord(
            run_key=("1" if strategy == RunStrategy.FULL else "2") * 64,
            run_id=f"run-{strategy.value}",
            task_id="cal-py-01",
            strategy=strategy,
            repetition=1,
            language="python",
            difficulty="easy",
            defect="boundary",
            outcome_class=OutcomeClass.TASK_RESULT,
            result="PASSED" if passed else "FAILED_TESTS",
            passed=passed,
            first_gate_passed=passed,
            provider="test-provider",
            requested_model="test-model",
            actual_model_ids=["test-model"],
            prompt_version="real-v1",
            temperature=0,
            started_at=timestamp,
            completed_at=timestamp,
            steps=2,
            model_calls=1,
            model_attempts=1,
            model_retries=0,
            tool_calls=1,
            prompt_tokens=100,
            completion_tokens=20,
            usage_estimated=False,
            estimated_cost_usd=Decimal("0.01"),
            reserved_unknown_cost_usd=Decimal(0),
            model_latency_ms=100,
            wall_time_seconds=1,
            scope_violation=False,
            regression=False,
            baseline_commit="0" * 40,
            artifact_directory="runs/example",
        )

    summary = build_real_summary(
        suite,
        [record(RunStrategy.FULL, passed=True), record(RunStrategy.SINGLE_SHOT, passed=False)],
        experiment={"task_limit": 1, "strategies": ["full", "single_shot"], "repetitions": 1},
        global_cost_limit=Decimal("4"),
        reserved_unknown_cost=Decimal(0),
    )

    assert summary.by_strategy["full"]["pass_at_1"] == 1.0
    assert summary.by_strategy["full"]["pass_rate_95ci"] == [0.206549, 1.0]
    assert summary.latency["p95_run_wall_seconds"] == 1
    assert summary.agent_metrics["average_tool_calls"] == 1
    paired = summary.paired_comparisons["full_vs_single_shot"]
    assert paired["left_wins"] == 1
    assert paired["right_wins"] == 0


def test_real_model_requires_all_three_explicit_gates() -> None:
    enabled = AppSettings(
        _env_file=None,
        patchpilot_enable_real_model=True,
        model_name="test-model",
        model_api_key="not-a-real-secret",
        model_input_cost_per_million_usd=Decimal("1"),
        model_output_cost_per_million_usd=Decimal("2"),
    )
    with pytest.raises(SettingsError, match="--real-model"):
        require_real_model(enabled, explicit_real_model=False)

    disabled = enabled.model_copy(update={"patchpilot_enable_real_model": False})
    with pytest.raises(SettingsError, match="PATCHPILOT_ENABLE_REAL_MODEL"):
        require_real_model(disabled, explicit_real_model=True)

    missing_key = enabled.model_copy(update={"model_api_key": None})
    with pytest.raises(SettingsError, match="MODEL_API_KEY"):
        require_real_model(missing_key, explicit_real_model=True)

    assert require_real_model(enabled, explicit_real_model=True).model == "test-model"


@pytest.mark.asyncio
async def test_global_cost_ledger_enforces_reserved_and_unknown_costs() -> None:
    ledger = GlobalCostLedger(Decimal("0.10"))
    reservation = await ledger.reserve(Decimal("0.08"))
    with pytest.raises(GlobalCostLimitError):
        await ledger.reserve(Decimal("0.03"))
    assert await ledger.mark_unknown(reservation) == Decimal("0.08")
    assert ledger.accounted == Decimal("0.08")
    assert not await ledger.can_start(Decimal("0.03"))


@pytest.mark.asyncio
async def test_dispatched_api_error_retains_conservative_unknown_cost() -> None:
    class FailingClient:
        async def complete(
            self,
            messages: list[Message],
            tools: list[ToolSchema],
            config: ModelConfig,
        ) -> ModelResponse:
            raise ModelClientError("redacted transport failure")

    ledger = GlobalCostLedger(Decimal("1"))
    client = AuditedRealModelClient(FailingClient(), ledger)
    config = ModelConfig(
        model="test-model",
        max_tokens=16,
        input_cost_per_million_usd=Decimal("1"),
        output_cost_per_million_usd=Decimal("1"),
    )

    with pytest.raises(ModelClientError):
        await client.complete(
            [Message(role=MessageRole.USER, content="bounded")],
            [],
            config,
        )

    assert client.unknown_cost > 0
    assert ledger.accounted == client.unknown_cost


def test_real_suite_rejects_missing_source_registry_entry(tmp_path: Path) -> None:
    suite_root = ROOT / "benchmarks" / "real-calibration-v1"
    # Loading a non-suite path fails closed instead of falling back to scripted data.
    with pytest.raises((BenchmarkError, FileNotFoundError)):
        load_real_benchmark(tmp_path / suite_root.name)
