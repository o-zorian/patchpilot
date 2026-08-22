# ruff: noqa: E501
from __future__ import annotations

import html
import math
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations

from patchpilot.benchmark.real_models import (
    OutcomeClass,
    RealBenchmarkRunRecord,
    RealBenchmarkSuite,
    RealBenchmarkSummary,
)


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    """Return a two-sided 95% Wilson score interval."""
    if total == 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[math.ceil(0.95 * len(ordered)) - 1], 6)


def _group_metrics(records: list[RealBenchmarkRunRecord]) -> dict[str, object]:
    task_records = [
        record for record in records if record.outcome_class == OutcomeClass.TASK_RESULT
    ]
    passed = sum(record.passed for record in task_records)
    first_repetition = [record for record in records if record.repetition == 1]
    first_task_results = [
        record for record in first_repetition if record.outcome_class == OutcomeClass.TASK_RESULT
    ]
    first_passed = sum(record.passed for record in first_task_results)
    wall_times = [record.wall_time_seconds for record in records]
    return {
        "runs": len(records),
        "task_results": len(task_records),
        "passed": passed,
        "pass_rate": round(passed / len(task_records), 6) if task_records else None,
        "pass_rate_95ci": _wilson_interval(passed, len(task_records)),
        "pass_at_1": (
            round(first_passed / len(first_task_results), 6) if first_task_results else None
        ),
        "pass_at_1_95ci": _wilson_interval(first_passed, len(first_task_results)),
        "repeat_success_rate": round(passed / len(records), 6) if records else None,
        "repeat_success_rate_95ci": _wilson_interval(passed, len(records)),
        "api_errors": sum(record.outcome_class == OutcomeClass.API_ERROR for record in records),
        "infrastructure_errors": sum(
            record.outcome_class == OutcomeClass.INFRASTRUCTURE_ERROR for record in records
        ),
        "interrupted": sum(record.outcome_class == OutcomeClass.INTERRUPTED for record in records),
        "prompt_tokens": sum(record.prompt_tokens for record in records),
        "completion_tokens": sum(record.completion_tokens for record in records),
        "cost_usd": str(sum((record.estimated_cost_usd for record in records), Decimal(0))),
        "average_wall_time_seconds": (
            round(sum(record.wall_time_seconds for record in records) / len(records), 6)
            if records
            else 0.0
        ),
        "p95_wall_time_seconds": _percentile_95(wall_times),
        "average_steps": (
            round(sum(record.steps for record in records) / len(records), 6) if records else 0.0
        ),
        "average_tool_calls": (
            round(sum(record.tool_calls for record in records) / len(records), 6)
            if records
            else 0.0
        ),
        "first_gate_pass_rate": (
            round(sum(record.first_gate_passed for record in task_records) / len(task_records), 6)
            if task_records
            else None
        ),
    }


def _groups(
    records: list[RealBenchmarkRunRecord],
    key: Callable[[RealBenchmarkRunRecord], str],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[RealBenchmarkRunRecord]] = {}
    for record in records:
        grouped.setdefault(key(record), []).append(record)
    return {name: _group_metrics(items) for name, items in sorted(grouped.items())}


def _paired_comparisons(
    records: list[RealBenchmarkRunRecord],
) -> dict[str, dict[str, object]]:
    by_strategy: dict[str, dict[tuple[str, int], RealBenchmarkRunRecord]] = {}
    for record in records:
        by_strategy.setdefault(record.strategy.value, {})[(record.task_id, record.repetition)] = (
            record
        )
    comparisons: dict[str, dict[str, object]] = {}
    for left, right in combinations(sorted(by_strategy), 2):
        left_records = by_strategy[left]
        right_records = by_strategy[right]
        keys = sorted(set(left_records) & set(right_records))
        eligible = [
            key
            for key in keys
            if left_records[key].outcome_class == OutcomeClass.TASK_RESULT
            and right_records[key].outcome_class == OutcomeClass.TASK_RESULT
        ]
        left_wins = sum(
            left_records[key].passed and not right_records[key].passed for key in eligible
        )
        right_wins = sum(
            right_records[key].passed and not left_records[key].passed for key in eligible
        )
        both_pass = sum(left_records[key].passed and right_records[key].passed for key in eligible)
        both_fail = len(eligible) - left_wins - right_wins - both_pass
        comparisons[f"{left}_vs_{right}"] = {
            "left": left,
            "right": right,
            "matched_pairs": len(keys),
            "eligible_task_result_pairs": len(eligible),
            "excluded_api_infrastructure_or_interrupted_pairs": len(keys) - len(eligible),
            "left_wins": left_wins,
            "right_wins": right_wins,
            "both_pass": both_pass,
            "both_fail": both_fail,
            "paired_success_delta": (
                round((left_wins - right_wins) / len(eligible), 6) if eligible else None
            ),
        }
    return comparisons


def build_real_summary(
    suite: RealBenchmarkSuite,
    records: list[RealBenchmarkRunRecord],
    *,
    experiment: dict[str, object],
    global_cost_limit: Decimal,
    reserved_unknown_cost: Decimal,
) -> RealBenchmarkSummary:
    task_results = [
        record for record in records if record.outcome_class == OutcomeClass.TASK_RESULT
    ]
    failure_reasons: dict[str, int] = {}
    for record in records:
        if not record.passed:
            key = record.error_code or record.result
            failure_reasons[key] = failure_reasons.get(key, 0) + 1
    actual_cost = sum((record.estimated_cost_usd for record in records), Decimal(0))
    total_latency = sum(record.model_latency_ms for record in records)
    wall_times = [record.wall_time_seconds for record in records]
    passed_count = sum(record.passed for record in task_results)
    task_limit = experiment.get("task_limit")
    selected_task_count = (
        min(len(suite.tasks), task_limit) if isinstance(task_limit, int) else len(suite.tasks)
    )
    strategy_values = experiment.get("strategies")
    strategy_count = len(strategy_values) if isinstance(strategy_values, list) else 0
    repetition_value = experiment.get("repetitions")
    repetition_count = repetition_value if isinstance(repetition_value, int) else 0
    expected_runs = selected_task_count * strategy_count * repetition_count
    matrix_complete = expected_runs > 0 and len(records) == expected_runs
    error_free = all(record.outcome_class == OutcomeClass.TASK_RESULT for record in records)
    return RealBenchmarkSummary(
        benchmark_id=suite.manifest.id,
        suite_kind=suite.manifest.suite_kind,
        generated_at=datetime.now(UTC),
        manifest_sha256=suite.manifest_sha256,
        task_set_sha256=suite.task_set_sha256,
        experiment=experiment,
        counts={
            "persisted_runs": len(records),
            "task_results": len(task_results),
            "passed": sum(record.passed for record in task_results),
            "failed": sum(not record.passed for record in task_results),
            "api_errors": sum(record.outcome_class == OutcomeClass.API_ERROR for record in records),
            "infrastructure_errors": sum(
                record.outcome_class == OutcomeClass.INFRASTRUCTURE_ERROR for record in records
            ),
            "interrupted": sum(
                record.outcome_class == OutcomeClass.INTERRUPTED for record in records
            ),
            "expected_runs": expected_runs,
            "matrix_complete": int(matrix_complete),
            "formal_result_valid": int(
                suite.manifest.suite_kind.value == "formal" and matrix_complete and error_free
            ),
        },
        cost={
            "actual_usage_cost_usd": str(actual_cost),
            "reserved_unknown_cost_usd": str(reserved_unknown_cost),
            "accounted_cost_usd": str(actual_cost + reserved_unknown_cost),
            "global_limit_usd": str(global_cost_limit),
        },
        latency={
            "total_model_latency_seconds": round(total_latency / 1_000, 6),
            "average_run_wall_seconds": (
                round(sum(record.wall_time_seconds for record in records) / len(records), 6)
                if records
                else 0.0
            ),
            "p95_run_wall_seconds": _percentile_95(wall_times),
        },
        agent_metrics={
            "average_steps": (
                round(sum(record.steps for record in records) / len(records), 6) if records else 0.0
            ),
            "average_tool_calls": (
                round(sum(record.tool_calls for record in records) / len(records), 6)
                if records
                else 0.0
            ),
            "first_gate_pass_rate": (
                round(
                    sum(record.first_gate_passed for record in task_results) / len(task_results), 6
                )
                if task_results
                else None
            ),
            "average_usage_cost_per_successful_run_usd": (
                str(actual_cost / passed_count) if passed_count else None
            ),
        },
        paired_comparisons=_paired_comparisons(records),
        by_strategy=_groups(records, lambda record: record.strategy.value),
        by_language=_groups(records, lambda record: record.language),
        by_difficulty=_groups(records, lambda record: record.difficulty),
        by_defect=_groups(records, lambda record: record.defect),
        failure_reasons=dict(sorted(failure_reasons.items())),
        limitations=[
            "Calibration results are infrastructure and difficulty checks and are excluded from formal conclusions.",
            "The task repositories are explicitly labeled curated snapshots; they are not full upstream projects.",
            "Usage-derived cost uses configured prices and may differ from provider billing, discounts, or cached-token pricing.",
            "API and infrastructure errors are reported separately and are not silently counted as model task failures.",
            "No formal result is valid unless all frozen task/strategy/repetition matrix entries are present.",
            "Pass rates use model task-result runs; repeat success rates use every persisted matrix entry and retain API/infrastructure outcomes in the denominator.",
            "95% confidence intervals are Wilson score intervals; paired comparisons only use matching task/repetition pairs and disclose excluded error pairs.",
        ],
    )


def _rate(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):.1%}"


def render_real_markdown(summary: RealBenchmarkSummary) -> str:
    strategy_rows = (
        "\n".join(
            f"| {name} | {metrics['runs']} | {_rate(metrics['pass_at_1'])} | {_rate(metrics['repeat_success_rate'])} | {_rate(metrics['pass_rate'])} | {metrics['api_errors']} | {metrics['infrastructure_errors']} | ${metrics['cost_usd']} |"
            for name, metrics in summary.by_strategy.items()
        )
        or "| _No completed runs_ | 0 | 0 | n/a | 0 | 0 | $0 |"
    )
    failures = (
        "\n".join(f"- `{reason}`: {count}" for reason, count in summary.failure_reasons.items())
        or "- None"
    )
    limitations = "\n".join(f"- {item}" for item in summary.limitations)
    paired_rows = (
        "\n".join(
            f"| {metrics['left']} | {metrics['right']} | {metrics['eligible_task_result_pairs']} | {metrics['left_wins']} | {metrics['right_wins']} | {metrics['both_pass']} | {metrics['both_fail']} | {metrics['excluded_api_infrastructure_or_interrupted_pairs']} |"
            for metrics in summary.paired_comparisons.values()
        )
        or "| n/a | n/a | 0 | 0 | 0 | 0 | 0 | 0 |"
    )
    return (
        f"# PatchPilot Real Benchmark: {summary.benchmark_id}\n\n"
        f"> Suite classification: **{summary.suite_kind.value}**. "
        + (
            "These results are not part of the formal Benchmark.\n\n"
            if summary.suite_kind.value == "calibration"
            else "Formal conclusions require a complete frozen matrix.\n\n"
        )
        + "## Reproducibility identity\n\n"
        f"- Manifest SHA-256: `{summary.manifest_sha256}`\n"
        f"- Task-set SHA-256: `{summary.task_set_sha256}`\n"
        f"- Provider label: `{summary.experiment.get('provider')}`\n"
        f"- Requested model: `{summary.experiment.get('requested_model')}`\n"
        f"- Prompt version: `{summary.experiment.get('prompt_version')}`\n"
        f"- PatchPilot commit: `{summary.experiment.get('patchpilot_git_commit')}`"
        f" (dirty={summary.experiment.get('patchpilot_git_dirty')})\n\n"
        "## Outcome classes\n\n"
        f"- Persisted runs: {summary.counts['persisted_runs']}\n"
        f"- Model task results: {summary.counts['task_results']} "
        f"({summary.counts['passed']} passed / {summary.counts['failed']} failed)\n"
        f"- API errors: {summary.counts['api_errors']}\n"
        f"- Infrastructure errors: {summary.counts['infrastructure_errors']}\n"
        f"- Interrupted/unknown billing: {summary.counts['interrupted']}\n\n"
        f"- Matrix complete: {bool(summary.counts['matrix_complete'])} "
        f"({summary.counts['persisted_runs']} / {summary.counts['expected_runs']})\n"
        f"- Formal result valid: {bool(summary.counts['formal_result_valid'])}\n\n"
        "## Cost accounting\n\n"
        f"- Usage-derived cost: ${summary.cost['actual_usage_cost_usd']}\n"
        f"- Conservatively reserved unknown cost: ${summary.cost['reserved_unknown_cost_usd']}\n"
        f"- Accounted cost / hard limit: ${summary.cost['accounted_cost_usd']} / ${summary.cost['global_limit_usd']}\n\n"
        "## Strategy comparison\n\n"
        "| Strategy | Runs | pass@1 | Repeat success | Model task pass | API errors | Infra errors | Cost |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|\n"
        f"{strategy_rows}\n\n"
        "## Latency and agent activity\n\n"
        f"- Average / P95 wall time: {summary.latency['average_run_wall_seconds']}s / {summary.latency['p95_run_wall_seconds']}s\n"
        f"- Average steps / tool calls: {summary.agent_metrics['average_steps']} / {summary.agent_metrics['average_tool_calls']}\n"
        f"- First Gate pass rate: {_rate(summary.agent_metrics['first_gate_pass_rate'])}\n"
        f"- Usage cost per successful Run: ${summary.agent_metrics['average_usage_cost_per_successful_run_usd']}\n\n"
        "## Paired comparisons\n\n"
        "| Left | Right | Eligible pairs | Left wins | Right wins | Both pass | Both fail | Error pairs excluded |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|\n"
        f"{paired_rows}\n\n"
        "## Failure reasons\n\n"
        f"{failures}\n\n"
        "## Limitations\n\n"
        f"{limitations}\n"
    )


def render_real_html(summary: RealBenchmarkSummary) -> str:
    rows = (
        "".join(
            f"<tr><td><code>{html.escape(name)}</code></td><td>{metrics['runs']}</td><td>{_rate(metrics['pass_at_1'])}</td><td>{_rate(metrics['repeat_success_rate'])}</td><td>{_rate(metrics['pass_rate'])}</td><td>{metrics['api_errors']}</td><td>{metrics['infrastructure_errors']}</td><td>${html.escape(str(metrics['cost_usd']))}</td></tr>"
            for name, metrics in summary.by_strategy.items()
        )
        or '<tr><td colspan="8">No completed runs</td></tr>'
    )
    failures = (
        "".join(
            f"<li><code>{html.escape(reason)}</code><strong>{count}</strong></li>"
            for reason, count in summary.failure_reasons.items()
        )
        or "<li>None</li>"
    )
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in summary.limitations)
    paired_rows = (
        "".join(
            f"<tr><td><code>{html.escape(str(metrics['left']))}</code></td><td><code>{html.escape(str(metrics['right']))}</code></td><td>{metrics['eligible_task_result_pairs']}</td><td>{metrics['left_wins']}</td><td>{metrics['right_wins']}</td><td>{metrics['both_pass']}</td><td>{metrics['both_fail']}</td><td>{metrics['excluded_api_infrastructure_or_interrupted_pairs']}</td></tr>"
            for metrics in summary.paired_comparisons.values()
        )
        or '<tr><td colspan="8">No eligible pairs</td></tr>'
    )
    calibration = summary.suite_kind.value == "calibration"
    formal_valid = bool(summary.counts["formal_result_valid"])
    banner = (
        "Calibration only — excluded from formal Benchmark conclusions"
        if calibration
        else (
            "Formal frozen suite — complete and free of API/infrastructure errors"
            if formal_valid
            else "INCOMPLETE/INVALID formal result — do not quote a pass rate"
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(summary.benchmark_id)} · PatchPilot Real Benchmark</title>
<style>:root{{--bg:#eef3f8;--panel:#fff;--ink:#172238;--muted:#607086;--line:#d8e0ea;--accent:#315efb;--warn:#b54708}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,sans-serif}}main{{width:min(1150px,calc(100% - 32px));margin:32px auto 72px}}header,section{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px;margin-top:18px;box-shadow:0 8px 28px #1722380d}}header{{color:#fff;background:linear-gradient(135deg,#14213d,#315efb)}}h1{{font-size:clamp(28px,5vw,46px);margin:4px 0}}.banner{{padding:10px 14px;border-radius:9px;background:#fff4e5;color:var(--warn);font-weight:800}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}}.metric{{padding:14px;border:1px solid var(--line);border-radius:11px}}.metric strong{{display:block;font-size:24px}}.metric span,.muted{{color:var(--muted)}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}}.table{{overflow:auto}}.failures{{max-width:600px;list-style:none;padding:0}}.failures li{{display:flex;justify-content:space-between;padding:8px;border-bottom:1px solid var(--line)}}code{{overflow-wrap:anywhere}}</style></head>
<body><main><header><p>Auditable OpenAI-compatible model evaluation</p><h1>{html.escape(summary.benchmark_id)}</h1><p>{html.escape(str(summary.experiment.get("provider")))} · {html.escape(str(summary.experiment.get("requested_model")))} · {html.escape(str(summary.generated_at))}</p></header>
<section><div class="banner">{html.escape(banner)}</div><p class="muted">Manifest SHA-256: <code>{summary.manifest_sha256}</code><br>Task-set SHA-256: <code>{summary.task_set_sha256}</code></p></section>
<section><h2>Outcome classes</h2><div class="metrics"><div class="metric"><strong>{summary.counts["persisted_runs"]}</strong><span>persisted runs</span></div><div class="metric"><strong>{summary.counts["task_results"]}</strong><span>task results</span></div><div class="metric"><strong>{summary.counts["passed"]}</strong><span>passed</span></div><div class="metric"><strong>{summary.counts["api_errors"]}</strong><span>API errors</span></div><div class="metric"><strong>{summary.counts["infrastructure_errors"]}</strong><span>infra errors</span></div></div></section>
<section><h2>Cost accounting</h2><p>Usage ${html.escape(summary.cost["actual_usage_cost_usd"])} + unknown reserve ${html.escape(summary.cost["reserved_unknown_cost_usd"])} = <strong>${html.escape(summary.cost["accounted_cost_usd"])}</strong> / hard limit ${html.escape(summary.cost["global_limit_usd"])}</p></section>
<section><h2>Strategy comparison</h2><div class="table"><table><thead><tr><th>Strategy</th><th>Runs</th><th>pass@1</th><th>Repeat success</th><th>Model task pass</th><th>API errors</th><th>Infra errors</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><h2>Latency and agent activity</h2><div class="metrics"><div class="metric"><strong>{summary.latency["average_run_wall_seconds"]}s</strong><span>average wall time</span></div><div class="metric"><strong>{summary.latency["p95_run_wall_seconds"]}s</strong><span>P95 wall time</span></div><div class="metric"><strong>{summary.agent_metrics["average_steps"]}</strong><span>average steps</span></div><div class="metric"><strong>{summary.agent_metrics["average_tool_calls"]}</strong><span>average tool calls</span></div><div class="metric"><strong>{_rate(summary.agent_metrics["first_gate_pass_rate"])}</strong><span>first Gate pass rate</span></div></div></section>
<section><h2>Paired comparisons</h2><div class="table"><table><thead><tr><th>Left</th><th>Right</th><th>Eligible</th><th>Left wins</th><th>Right wins</th><th>Both pass</th><th>Both fail</th><th>Error pairs excluded</th></tr></thead><tbody>{paired_rows}</tbody></table></div></section>
<section><h2>Failure reasons</h2><ul class="failures">{failures}</ul></section><section><h2>Limitations</h2><ul>{limitations}</ul></section></main></body></html>"""
