from __future__ import annotations

from decimal import Decimal

from patchpilot.agent.budget import BudgetStopReason, BudgetTracker
from patchpilot.domain.task import BudgetSpec
from patchpilot.models.base import ModelConfig, TokenUsage


class ManualClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_budget_tracks_steps_tokens_cost_and_wall_time() -> None:
    clock = ManualClock()
    budget = BudgetSpec(
        max_steps=2,
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_usd=Decimal("0.00002"),
        max_wall_time_seconds=5,
        max_changed_files=2,
        max_patch_lines=20,
    )
    config = ModelConfig(
        model="offline-test",
        input_cost_per_million_usd=Decimal("2"),
        output_cost_per_million_usd=Decimal("4"),
    )
    tracker = BudgetTracker(budget, config, clock=clock)

    assert tracker.before_step() is None
    tracker.begin_step()
    tracker.record_model_call()
    tracker.record_tool_call()
    reason = tracker.record_usage(TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    clock.now += 3
    snapshot = tracker.snapshot()

    assert reason is None
    assert snapshot.steps == 1
    assert snapshot.model_calls == 1
    assert snapshot.tool_calls == 1
    assert snapshot.estimated_cost_usd == Decimal("0.000018")
    assert snapshot.wall_time_seconds == 3
    assert tracker.remaining_wall_time_seconds == 2

    tracker.begin_step()
    assert tracker.before_step() == BudgetStopReason.MAX_STEPS
    clock.now += 2
    assert tracker.check_non_step_limits() == BudgetStopReason.MAX_WALL_TIME_SECONDS


def test_budget_stops_at_hard_token_and_cost_boundaries() -> None:
    budget = BudgetSpec(
        max_steps=5,
        max_input_tokens=3,
        max_output_tokens=4,
        max_cost_usd=Decimal("1"),
        max_wall_time_seconds=30,
        max_changed_files=2,
        max_patch_lines=20,
    )
    config = ModelConfig(
        model="offline-test",
        input_cost_per_million_usd=Decimal("500000"),
    )

    input_tracker = BudgetTracker(budget, ModelConfig(model="offline-test"))
    assert (
        input_tracker.record_usage(TokenUsage(prompt_tokens=3, completion_tokens=0, total_tokens=3))
        == BudgetStopReason.MAX_INPUT_TOKENS
    )

    output_tracker = BudgetTracker(budget, ModelConfig(model="offline-test"))
    assert (
        output_tracker.record_usage(
            TokenUsage(prompt_tokens=0, completion_tokens=4, total_tokens=4)
        )
        == BudgetStopReason.MAX_OUTPUT_TOKENS
    )

    cost_tracker = BudgetTracker(budget, config)
    assert (
        cost_tracker.record_usage(TokenUsage(prompt_tokens=2, completion_tokens=0, total_tokens=2))
        == BudgetStopReason.MAX_COST_USD
    )
