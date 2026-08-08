# ruff: noqa: E501
# Standalone HTML/CSS templates retain readable source lines.

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
    strategy_rows: list[str] = []
    strategy_bars: list[str] = []
    for name, metrics in summary.by_strategy.items():
        escaped_name = html.escape(name)
        strategy_rows.append(
            f"<tr><td><code>{escaped_name}</code></td><td>{metrics.runs}</td>"
            f"<td>{metrics.pass_rate:.1%}</td><td>{metrics.first_gate_pass_rate:.1%}</td>"
            f"<td>{metrics.average_steps:.2f}</td><td>{metrics.average_prompt_tokens:.1f} / "
            f"{metrics.average_completion_tokens:.1f}</td>"
            f"<td>${metrics.average_cost_usd:.6f}</td>"
            f"<td>{metrics.average_wall_time_seconds:.3f}s</td></tr>"
        )
        width = max(0.0, min(metrics.pass_rate * 100, 100.0))
        strategy_bars.append(
            '<div class="bar-row"><code>'
            f'{escaped_name}</code><div class="track"><span style="width:{width:.2f}%"></span>'
            f"</div><strong>{metrics.pass_rate:.1%}</strong></div>"
        )

    def group_rows(groups: dict[str, MetricGroup]) -> str:
        return "".join(
            f"<tr><td>{html.escape(name)}</td><td>{metrics.runs}</td>"
            f"<td>{metrics.pass_rate:.1%}</td><td>{metrics.average_cost_usd:.6f}</td></tr>"
            for name, metrics in groups.items()
        )

    def case_cards(records: list[BenchmarkRunRecord], empty: str) -> str:
        if not records:
            return f'<p class="muted">{html.escape(empty)}</p>'
        return "".join(
            '<article class="case"><div><strong>'
            f"{html.escape(record.task_id)}</strong><code>{html.escape(record.strategy.value)}</code>"
            f'</div><span class="result">{html.escape(record.result)}</span>'
            f"<p>{html.escape(record.language)} · {html.escape(record.difficulty)} · "
            f"{html.escape(record.defect)}</p><small>{record.steps} steps · "
            f"{record.tool_calls} tools · ${record.estimated_cost_usd}</small></article>"
            for record in records
        )

    failures = (
        "".join(
            f"<li><code>{html.escape(reason)}</code><strong>{count}</strong></li>"
            for reason, count in summary.failure_reasons.items()
        )
        or "<li>None</li>"
    )
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in summary.limitations)
    generated = html.escape(summary.generated_at)
    benchmark_id = html.escape(summary.benchmark_id)
    task_hash = html.escape(summary.task_set_sha256)
    model = html.escape(str(summary.experiment["model"]))
    prompt_version = html.escape(str(summary.experiment["prompt_version"]))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{benchmark_id} · PatchPilot Benchmark</title>
  <style>
    :root {{ --ink:#172238; --muted:#627085; --line:#dce3ec; --panel:#fff; --bg:#eef3f8;
      --accent:#315efb; --accent2:#15a37d; }} * {{ box-sizing:border-box; }}
    body {{ margin:0; font:14px/1.55 Inter,ui-sans-serif,system-ui,sans-serif;
      color:var(--ink); background:var(--bg); }}
    main {{ width:min(1200px,calc(100% - 32px)); margin:32px auto 72px; }}
    header,section {{ border:1px solid var(--line); border-radius:16px; background:var(--panel);
      box-shadow:0 8px 26px rgba(23,34,56,.06); }} header {{ padding:30px;
      color:#fff; background:linear-gradient(135deg,#14213d,#315efb); }}
    section {{ margin-top:18px; padding:24px; }} h1 {{ margin:3px 0 8px;
      font-size:clamp(28px,5vw,46px); }} h2 {{ margin:0 0 18px; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }}
    .eyebrow {{ text-transform:uppercase; letter-spacing:.12em; opacity:.75; font-weight:800; }}
    .identity {{ opacity:.82; overflow-wrap:anywhere; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
    .metric {{ padding:16px; border:1px solid var(--line); border-radius:12px; background:#f8fafc; }}
    .metric strong {{ display:block; font-size:24px; }} .metric span,.muted {{ color:var(--muted); }}
    .bar-chart {{ display:grid; gap:12px; }} .bar-row {{ display:grid;
      grid-template-columns:minmax(170px,1.2fr) minmax(180px,4fr) 60px; gap:12px; align-items:center; }}
    .track {{ height:13px; border-radius:999px; overflow:hidden; background:#e5eaf1; }}
    .track span {{ display:block; height:100%; border-radius:inherit;
      background:linear-gradient(90deg,var(--accent),var(--accent2)); }}
    .table-wrap {{ overflow:auto; }} table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
    th {{ color:var(--muted); text-transform:uppercase; letter-spacing:.05em; font-size:12px; }}
    .groups {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }}
    .subpanel {{ padding:16px; border:1px solid var(--line); border-radius:12px; overflow:auto; }}
    .subpanel h3 {{ margin:0 0 10px; }} .cases {{ display:grid;
      grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; }}
    .case {{ padding:15px; border:1px solid var(--line); border-radius:12px; }}
    .case div {{ display:flex; justify-content:space-between; gap:8px; }}
    .case p {{ color:var(--muted); }} .result {{ display:inline-block; margin-top:10px;
      color:var(--accent2); font-weight:800; }}
    .failure-list {{ max-width:520px; padding:0; list-style:none; }} .failure-list li {{ display:flex;
      justify-content:space-between; padding:9px 0; border-bottom:1px solid var(--line); }}
    @media (max-width:650px) {{ .bar-row {{ grid-template-columns:1fr 55px; }}
      .bar-row code {{ grid-column:1/-1; }} }}
    @media (prefers-color-scheme:dark) {{ :root {{ --ink:#e6edf6; --muted:#aeb9c8;
      --line:#344155; --panel:#172033; --bg:#0f1726; }} .metric {{ background:#101a2b; }}
      .track {{ background:#303c51; }} }}
  </style>
</head>
<body>
<main>
  <header><div class="eyebrow">Offline, reproducible evaluation</div><h1>{benchmark_id}</h1>
    <p>{model} · prompt {prompt_version} · generated {generated}</p>
    <p class="identity">Task-set SHA-256: {task_hash}</p></header>
  <section><h2>Overall metrics</h2><div class="metrics">
    <div class="metric"><strong>{summary.overall.runs}</strong><span>runs</span></div>
    <div class="metric"><strong>{summary.overall.pass_rate:.1%}</strong><span>pass rate</span></div>
    <div class="metric"><strong>{summary.overall.first_gate_pass_rate:.1%}</strong><span>first-gate pass</span></div>
    <div class="metric"><strong>{summary.overall.average_steps:.2f}</strong><span>average steps</span></div>
    <div class="metric"><strong>${summary.overall.average_cost_usd:.6f}</strong><span>average estimated cost</span></div>
    <div class="metric"><strong>{summary.overall.average_wall_time_seconds:.3f}s</strong><span>average wall time</span></div>
  </div></section>
  <section><h2>Strategy comparison</h2><div class="bar-chart" role="img" aria-label="Pass rate by Agent strategy">{"".join(strategy_bars)}</div></section>
  <section><h2>Cost, tokens, and pass rate</h2><div class="table-wrap"><table><thead><tr>
    <th>Strategy</th><th>Runs</th><th>Pass rate</th><th>First gate</th><th>Avg steps</th>
    <th>Avg prompt / completion tokens</th><th>Avg cost</th><th>Avg wall</th></tr></thead>
    <tbody>{"".join(strategy_rows)}</tbody></table></div></section>
  <section><h2>Category breakdown</h2><div class="groups">
    <div class="subpanel"><h3>Language</h3><table><tbody>{group_rows(summary.by_language)}</tbody></table></div>
    <div class="subpanel"><h3>Difficulty</h3><table><tbody>{group_rows(summary.by_difficulty)}</tbody></table></div>
    <div class="subpanel"><h3>Defect</h3><table><tbody>{group_rows(summary.by_defect)}</tbody></table></div>
  </div></section>
  <section><h2>Representative successes</h2><div class="cases">{case_cards(summary.successes, "No successful cases in this run.")}</div></section>
  <section><h2>Representative failures</h2><div class="cases">{case_cards(summary.failures, "No failed cases in this run.")}</div></section>
  <section><h2>Failure analysis</h2><ul class="failure-list">{failures}</ul>
    <p class="muted">In this deterministic fixture run, failures indicate strategy capability limits or deterministic Quality Gate rejection, not random model behavior.</p></section>
  <section><h2>Conclusions this benchmark cannot support</h2><ul>{limitations}</ul></section>
</main>
</body>
</html>
"""
