from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from patchpilot.domain.scorecard import (
    GateChecks,
    QualityResult,
    Scorecard,
    ScorecardArtifacts,
    ScorecardMetrics,
)
from patchpilot.quality.gate import classify_gate
from patchpilot.reporting.render import render_html, render_markdown


def checks(**updates: bool) -> GateChecks:
    values = {
        "has_patch": True,
        "scope_valid": True,
        "patch_size_valid": True,
        "tests_passed": True,
        "required_tests_ran": True,
        "budget_valid": True,
    }
    values.update(updates)
    return GateChecks.model_validate(values)


@pytest.mark.parametrize(
    ("gate_checks", "options", "expected"),
    [
        (checks(), {}, QualityResult.PASSED),
        (checks(budget_valid=False), {}, QualityResult.BUDGET_EXCEEDED),
        (checks(has_patch=False), {}, QualityResult.NO_PATCH),
        (checks(scope_valid=False), {}, QualityResult.SCOPE_VIOLATION),
        (checks(patch_size_valid=False), {}, QualityResult.PATCH_TOO_LARGE),
        (checks(tests_passed=False), {}, QualityResult.TEST_FAILURE),
        (checks(tests_passed=False), {"regression": True}, QualityResult.REGRESSION),
        (checks(required_tests_ran=False), {}, QualityResult.REQUIRED_TEST_NOT_RUN),
        (checks(tests_passed=False), {"timed_out": True}, QualityResult.TIMEOUT),
        (
            checks(tests_passed=False),
            {"sandbox_error": True},
            QualityResult.SANDBOX_ERROR,
        ),
    ],
)
def test_quality_result_classification_is_deterministic(
    gate_checks: GateChecks,
    options: dict[str, bool],
    expected: QualityResult,
) -> None:
    assert classify_gate(gate_checks, **options) == expected


def test_scorecard_markdown_and_html_are_deterministic_and_escaped() -> None:
    scorecard = Scorecard(
        run_id=uuid4(),
        task_id="task-<script>alert(1)</script>",
        result=QualityResult.PASSED,
        checks=checks(),
        metrics=ScorecardMetrics(
            steps=2,
            model_calls=2,
            tool_calls=2,
            changed_files=1,
            added_lines=1,
            deleted_lines=1,
            prompt_tokens=20,
            completion_tokens=10,
            estimated_cost_usd=Decimal("0.01"),
            wall_time_seconds=1.25,
        ),
        artifacts=ScorecardArtifacts(
            patch="run/final.patch",
            events="run/events.jsonl",
            test_log="run/test.log",
            scorecard="run/scorecard.json",
            report_markdown="run/report.md",
            report_html="run/report.html",
        ),
    )

    markdown = render_markdown(scorecard)
    html = render_html(scorecard)

    assert "Result: **PASSED**" in markdown
    assert "task-<script>" in markdown
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert html == render_html(scorecard)
    assert isinstance(
        json.loads(scorecard.model_dump_json())["metrics"]["estimated_cost_usd"], float
    )
