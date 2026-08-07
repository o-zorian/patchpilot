from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from patchpilot.domain.task import BudgetSpec
from patchpilot.models.base import ModelConfig, TokenUsage


class BudgetStopReason(StrEnum):
    MAX_STEPS = "max_steps"
    MAX_INPUT_TOKENS = "max_input_tokens"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    MAX_COST_USD = "max_cost_usd"
    MAX_WALL_TIME_SECONDS = "max_wall_time_seconds"


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    steps: int
    model_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal
    wall_time_seconds: float


class BudgetTracker:
    def __init__(
        self,
        budget: BudgetSpec,
        model_config: ModelConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.model_config = model_config
        self._clock = clock
        self._started = clock()
        self.steps = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_cost_usd = Decimal("0")

    @property
    def wall_time_seconds(self) -> float:
        return max(0.0, self._clock() - self._started)

    @property
    def remaining_wall_time_seconds(self) -> float:
        return max(0.0, self.budget.max_wall_time_seconds - self.wall_time_seconds)

    def before_step(self) -> BudgetStopReason | None:
        if self.wall_time_seconds >= self.budget.max_wall_time_seconds:
            return BudgetStopReason.MAX_WALL_TIME_SECONDS
        if self.steps >= self.budget.max_steps:
            return BudgetStopReason.MAX_STEPS
        return None

    def begin_step(self) -> None:
        self.steps += 1

    def record_model_call(self) -> None:
        self.model_calls += 1

    def record_usage(self, usage: TokenUsage) -> BudgetStopReason | None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.estimated_cost_usd += self.model_config.cost_for(usage)
        return self.check_non_step_limits()

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def check_non_step_limits(self) -> BudgetStopReason | None:
        if self.wall_time_seconds >= self.budget.max_wall_time_seconds:
            return BudgetStopReason.MAX_WALL_TIME_SECONDS
        if self.prompt_tokens >= self.budget.max_input_tokens:
            return BudgetStopReason.MAX_INPUT_TOKENS
        if self.completion_tokens >= self.budget.max_output_tokens:
            return BudgetStopReason.MAX_OUTPUT_TOKENS
        if self.estimated_cost_usd >= self.budget.max_cost_usd:
            return BudgetStopReason.MAX_COST_USD
        return None

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            steps=self.steps,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            estimated_cost_usd=self.estimated_cost_usd,
            wall_time_seconds=self.wall_time_seconds,
        )
