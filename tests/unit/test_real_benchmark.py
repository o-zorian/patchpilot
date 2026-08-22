from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from patchpilot.benchmark.models import BenchmarkError
from patchpilot.benchmark.real_models import RealSuiteKind, load_real_benchmark
from patchpilot.benchmark.real_runner import (
    AuditedRealModelClient,
    GlobalCostLedger,
    GlobalCostLimitError,
    require_real_model,
)
from patchpilot.config import AppSettings, SettingsError
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
