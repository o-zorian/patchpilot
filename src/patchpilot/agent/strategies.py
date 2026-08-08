from __future__ import annotations

from dataclasses import dataclass

from patchpilot.domain.run import RunStrategy


@dataclass(frozen=True, slots=True)
class StrategyPolicy:
    """Deterministic capabilities used to make benchmark strategies comparable."""

    strategy: RunStrategy
    allowed_tools: frozenset[str]
    gate_feedback: bool
    compact_context: bool
    max_model_calls: int | None = None


_BASE_TOOLS = frozenset({"read_file", "apply_patch", "git_diff", "finish"})


POLICIES: dict[RunStrategy, StrategyPolicy] = {
    RunStrategy.SINGLE_SHOT: StrategyPolicy(
        strategy=RunStrategy.SINGLE_SHOT,
        allowed_tools=frozenset({"apply_patch", "finish"}),
        gate_feedback=False,
        compact_context=False,
        max_model_calls=1,
    ),
    RunStrategy.AGENT_LOOP: StrategyPolicy(
        strategy=RunStrategy.AGENT_LOOP,
        allowed_tools=_BASE_TOOLS | {"list_files"},
        gate_feedback=False,
        compact_context=False,
    ),
    RunStrategy.AGENT_LOOP_WITH_TESTS: StrategyPolicy(
        strategy=RunStrategy.AGENT_LOOP_WITH_TESTS,
        allowed_tools=_BASE_TOOLS | {"list_files", "run_tests"},
        gate_feedback=True,
        compact_context=False,
    ),
    RunStrategy.AGENT_LOOP_WITH_SEARCH: StrategyPolicy(
        strategy=RunStrategy.AGENT_LOOP_WITH_SEARCH,
        allowed_tools=_BASE_TOOLS | {"list_files", "search_code"},
        gate_feedback=False,
        compact_context=False,
    ),
    RunStrategy.FULL: StrategyPolicy(
        strategy=RunStrategy.FULL,
        allowed_tools=_BASE_TOOLS | {"list_files", "search_code", "run_tests", "run_linter"},
        gate_feedback=True,
        compact_context=True,
    ),
}


def policy_for(strategy: RunStrategy) -> StrategyPolicy:
    return POLICIES[strategy]
