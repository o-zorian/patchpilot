# ruff: noqa: E501
from __future__ import annotations

import html
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from patchpilot.benchmark.real_models import (
    OutcomeClass,
    RealBenchmarkRunRecord,
    RealBenchmarkSuite,
    RealBenchmarkSummary,
)


def _group_metrics(records: list[RealBenchmarkRunRecord]) -> dict[str, object]:
    task_records = [
        record for record in records if record.outcome_class == OutcomeClass.TASK_RESULT
    ]
    passed = sum(record.passed for record in task_records)
    return {
        "runs": len(records),
        "task_results": len(task_records),
        "passed": passed,
        "pass_rate": round(passed / len(task_records), 6) if task_records else None,
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
    }


def _groups(
    records: list[RealBenchmarkRunRecord],
    key: Callable[[RealBenchmarkRunRecord], str],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[RealBenchmarkRunRecord]] = {}
    for record in records:
        grouped.setdefault(key(record), []).append(record)
    return {name: _group_metrics(items) for name, items in sorted(grouped.items())}


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
        },
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
        ],
    )


def _rate(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):.1%}"


def render_real_markdown(summary: RealBenchmarkSummary) -> str:
    strategy_rows = (
        "\n".join(
            f"| {name} | {metrics['runs']} | {metrics['task_results']} | {_rate(metrics['pass_rate'])} | {metrics['api_errors']} | {metrics['infrastructure_errors']} | ${metrics['cost_usd']} |"
            for name, metrics in summary.by_strategy.items()
        )
        or "| _No completed runs_ | 0 | 0 | n/a | 0 | 0 | $0 |"
    )
    failures = (
        "\n".join(f"- `{reason}`: {count}" for reason, count in summary.failure_reasons.items())
        or "- None"
    )
    limitations = "\n".join(f"- {item}" for item in summary.limitations)
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
        "| Strategy | Runs | Task results | Pass rate | API errors | Infra errors | Cost |\n"
        "|---|---:|---:|---:|---:|---:|---:|\n"
        f"{strategy_rows}\n\n"
        "## Failure reasons\n\n"
        f"{failures}\n\n"
        "## Limitations\n\n"
        f"{limitations}\n"
    )


def render_real_html(summary: RealBenchmarkSummary) -> str:
    rows = (
        "".join(
            f"<tr><td><code>{html.escape(name)}</code></td><td>{metrics['runs']}</td><td>{metrics['task_results']}</td><td>{_rate(metrics['pass_rate'])}</td><td>{metrics['api_errors']}</td><td>{metrics['infrastructure_errors']}</td><td>${html.escape(str(metrics['cost_usd']))}</td></tr>"
            for name, metrics in summary.by_strategy.items()
        )
        or '<tr><td colspan="7">No completed runs</td></tr>'
    )
    failures = (
        "".join(
            f"<li><code>{html.escape(reason)}</code><strong>{count}</strong></li>"
            for reason, count in summary.failure_reasons.items()
        )
        or "<li>None</li>"
    )
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in summary.limitations)
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
<section><h2>Strategy comparison</h2><div class="table"><table><thead><tr><th>Strategy</th><th>Runs</th><th>Task results</th><th>Pass rate</th><th>API errors</th><th>Infra errors</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><h2>Failure reasons</h2><ul class="failures">{failures}</ul></section><section><h2>Limitations</h2><ul>{limitations}</ul></section></main></body></html>"""
