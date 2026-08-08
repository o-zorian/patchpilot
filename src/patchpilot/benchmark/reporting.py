from __future__ import annotations

import html
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from patchpilot.benchmark.models import (
    BenchmarkRunRecord,
    BenchmarkSuite,
    BenchmarkSummary,
    MetricGroup,
)


def _metrics(records: list[BenchmarkRunRecord]) -> MetricGroup:
    count = len(records)

    def average(value: Callable[[BenchmarkRunRecord], int | float | Decimal]) -> float:
        return round(float(sum((value(record) for record in records), start=0)) / count, 6)

    def rate(predicate: Callable[[BenchmarkRunRecord], bool]) -> float:
        return round(sum(predicate(record) for record in records) / count, 6)

    return MetricGroup(
        runs=count,
        passed=sum(record.passed for record in records),
        pass_rate=rate(lambda record: record.passed),
        first_gate_pass_rate=rate(lambda record: record.first_gate_passed),
        average_steps=average(lambda record: record.steps),
        average_model_calls=average(lambda record: record.model_calls),
        average_tool_calls=average(lambda record: record.tool_calls),
        average_prompt_tokens=average(lambda record: record.prompt_tokens),
        average_completion_tokens=average(lambda record: record.completion_tokens),
        average_cost_usd=average(lambda record: record.estimated_cost_usd),
        average_wall_time_seconds=average(lambda record: record.wall_time_seconds),
        scope_violation_rate=rate(lambda record: record.scope_violation),
        regression_rate=rate(lambda record: record.regression),
    )


def _group(
    records: list[BenchmarkRunRecord],
    key: Callable[[BenchmarkRunRecord], str],
) -> dict[str, MetricGroup]:
    values: dict[str, list[BenchmarkRunRecord]] = {}
    for record in records:
        values.setdefault(key(record), []).append(record)
    return {name: _metrics(grouped) for name, grouped in sorted(values.items())}


def build_summary(
    suite: BenchmarkSuite,
    records: list[BenchmarkRunRecord],
    *,
    selected_strategies: Iterable[str],
    repetitions: int,
) -> BenchmarkSummary:
    if not records:
        raise ValueError("cannot summarize an empty benchmark run")
    failure_reasons: dict[str, int] = {}
    for record in records:
        if not record.passed:
            failure_reasons[record.result] = failure_reasons.get(record.result, 0) + 1
    return BenchmarkSummary(
        benchmark_id=suite.manifest.id,
        generated_at=datetime.now(UTC).isoformat(),
        task_set_sha256=suite.task_set_sha256,
        experiment={
            "task_count": len({record.task_id for record in records}),
            "strategies": list(selected_strategies),
            "model": suite.manifest.model,
            "model_parameters": {
                "temperature": suite.manifest.temperature,
                "max_tokens": suite.manifest.max_tokens,
            },
            "prompt_version": suite.manifest.prompt_version,
            "repetitions": repetitions,
            "network_model_calls": False,
        },
        overall=_metrics(records),
        by_strategy=_group(records, lambda record: record.strategy.value),
        by_language=_group(records, lambda record: record.language),
        by_difficulty=_group(records, lambda record: record.difficulty),
        by_defect=_group(records, lambda record: record.defect),
        successes=[record for record in records if record.passed][:3],
        failures=[record for record in records if not record.passed][:3],
        failure_reasons=dict(sorted(failure_reasons.items())),
        limitations=[
            "Results use a deterministic Scripted Model and measure harness behavior, "
            "not model intelligence.",
            "The local suite contains small synthetic repositories and does not "
            "represent SWE-bench.",
            "One repetition is reproducible but cannot estimate variance from "
            "stochastic real models.",
            "Trusted-local execution is reserved for project-owned fixtures; "
            "unknown repositories require Docker.",
        ],
    )


def render_markdown(summary: BenchmarkSummary) -> str:
    configured_strategies = summary.experiment.get("strategies", [])
    strategy_text = (
        ", ".join(str(item) for item in configured_strategies)
        if isinstance(configured_strategies, list)
        else str(configured_strategies)
    )
    strategy_rows = "\n".join(
        f"| {name} | {metrics.runs} | {metrics.pass_rate:.1%} | "
        f"{metrics.first_gate_pass_rate:.1%} | {metrics.average_cost_usd:.6f} | "
        f"{metrics.average_wall_time_seconds:.3f} |"
        for name, metrics in summary.by_strategy.items()
    )
    category_rows = "\n".join(
        f"| {name} | {metrics.runs} | {metrics.pass_rate:.1%} |"
        for name, metrics in summary.by_defect.items()
    )

    def cases(records: list[BenchmarkRunRecord]) -> str:
        return (
            "\n".join(
                f"- `{record.task_id}` / `{record.strategy.value}`: {record.result}; "
                f"steps={record.steps}, tools={record.tool_calls}"
                for record in records
            )
            or "- No cases available in this filtered run."
        )

    failures = (
        "\n".join(f"- `{reason}`: {count}" for reason, count in summary.failure_reasons.items())
        or "- None"
    )
    limitations = "\n".join(f"- {item}" for item in summary.limitations)
    return (
        f"# PatchPilot Benchmark: {summary.benchmark_id}\n\n"
        "## Experiment configuration\n\n"
        f"- Task-set SHA-256: `{summary.task_set_sha256}`\n"
        f"- Model: `{summary.experiment['model']}` (offline Scripted Model)\n"
        f"- Prompt version: `{summary.experiment['prompt_version']}`\n"
        f"- Repetitions: {summary.experiment['repetitions']}\n"
        f"- Strategies: {strategy_text}\n\n"
        "## Overall metrics\n\n"
        f"- Runs: {summary.overall.runs}\n"
        f"- Pass rate: {summary.overall.pass_rate:.1%}\n"
        f"- First-gate pass rate: {summary.overall.first_gate_pass_rate:.1%}\n"
        f"- Average steps/model calls/tool calls: {summary.overall.average_steps:.2f} / "
        f"{summary.overall.average_model_calls:.2f} / {summary.overall.average_tool_calls:.2f}\n"
        f"- Average tokens: {summary.overall.average_prompt_tokens:.2f} prompt / "
        f"{summary.overall.average_completion_tokens:.2f} completion\n"
        f"- Average estimated cost: ${summary.overall.average_cost_usd:.6f}\n"
        f"- Average wall time: {summary.overall.average_wall_time_seconds:.3f}s\n"
        f"- Scope violation rate: {summary.overall.scope_violation_rate:.1%}\n"
        f"- Regression rate: {summary.overall.regression_rate:.1%}\n\n"
        "## Cost and pass-rate comparison\n\n"
        "| Strategy | Runs | Pass rate | First gate | Avg cost USD | Avg wall s |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"{strategy_rows}\n\n"
        "## Pass rate by defect category\n\n"
        "| Defect | Runs | Pass rate |\n|---|---:|---:|\n"
        f"{category_rows}\n\n"
        "## Successful cases\n\n"
        f"{cases(summary.successes)}\n\n"
        "## Failed cases\n\n"
        f"{cases(summary.failures)}\n\n"
        "## Failure analysis\n\n"
        f"{failures}\n\n"
        "Failures in this deterministic fixture run primarily indicate strategy capability "
        "limits or a Quality Gate rejection, not random model behavior.\n\n"
        "## Conclusions this benchmark cannot support\n\n"
        f"{limitations}\n"
    )


def render_html(summary: BenchmarkSummary) -> str:
    markdown = render_markdown(summary)
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{metrics.runs}</td>"
        f"<td>{metrics.pass_rate:.1%}</td><td>{metrics.average_cost_usd:.6f}</td></tr>"
        for name, metrics in summary.by_strategy.items()
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(summary.benchmark_id)} benchmark</title>"
        "<style>body{font:15px/1.5 system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}"
        "table{border-collapse:collapse}th,td{border:1px solid #ccd;padding:.45rem .7rem}"
        "pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border-radius:8px}</style>"
        "</head><body><h1>PatchPilot Benchmark</h1><table><thead><tr><th>Strategy</th>"
        f"<th>Runs</th><th>Pass rate</th><th>Avg cost USD</th></tr></thead><tbody>{rows}"
        f"</tbody></table><pre>{html.escape(markdown)}</pre></body></html>\n"
    )
