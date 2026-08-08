# ruff: noqa: E501
# Standalone HTML/CSS templates retain readable source lines.

from __future__ import annotations

import html
import json
from pathlib import PurePosixPath
from typing import Any

from patchpilot.domain.scorecard import Scorecard

_MAX_REPORT_PATCH_CHARS = 50_000
_MAX_REPORT_TEST_CHARS = 80_000
_MAX_REPORT_EVENTS = 200
_MAX_EVENT_DETAIL_CHARS = 2_000


def render_markdown(scorecard: Scorecard) -> str:
    checks = "\n".join(
        f"- {name}: {'PASS' if value else 'FAIL'}"
        for name, value in scorecard.checks.model_dump().items()
    )
    failures = (
        "\n".join(
            f"- **{failure.result.value}**: {failure.summary}" for failure in scorecard.failures
        )
        or "- None"
    )
    metrics = scorecard.metrics
    artifacts = "\n".join(
        f"- {name}: `{path}`" for name, path in scorecard.artifacts.model_dump().items()
    )
    return (
        f"# PatchPilot Run {scorecard.run_id}\n\n"
        f"- Task: `{scorecard.task_id}`\n"
        f"- Result: **{scorecard.result.value}**\n\n"
        "## Quality checks\n\n"
        f"{checks}\n\n"
        "## Metrics\n\n"
        f"- Steps: {metrics.steps}\n"
        f"- Model calls: {metrics.model_calls}\n"
        f"- Tool calls: {metrics.tool_calls}\n"
        f"- Changed files: {metrics.changed_files}\n"
        f"- Patch lines: +{metrics.added_lines}/-{metrics.deleted_lines}\n"
        f"- Tokens: {metrics.prompt_tokens} prompt / {metrics.completion_tokens} completion\n"
        f"- Estimated cost: ${metrics.estimated_cost_usd}\n"
        f"- Wall time: {metrics.wall_time_seconds:.3f}s\n\n"
        "## Failures\n\n"
        f"{failures}\n\n"
        "## Artifacts\n\n"
        f"{artifacts}\n"
    )


def _bounded(value: str, maximum: int, label: str) -> str:
    if len(value) <= maximum:
        return value
    suffix = f"\n[PatchPilot {label} preview truncated; open the full artifact.]\n"
    return value[: maximum - len(suffix)] + suffix


def _display_text(value: str) -> str:
    rendered = "\n".join(line.rstrip() for line in value.splitlines())
    return rendered + ("\n" if value.endswith("\n") else "")


def _load_events(events_jsonl: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in events_jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events[-_MAX_REPORT_EVENTS:]


def _event_summary(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return "No structured payload"
    preferred = (
        "tool",
        "status",
        "result",
        "error_code",
        "finish_reason",
        "summary",
        "prompt_tokens",
        "completion_tokens",
    )
    parts = [f"{key}={payload[key]}" for key in preferred if payload.get(key) is not None]
    return "; ".join(parts) or "Structured event recorded"


def _render_timeline(events_jsonl: str) -> str:
    events = _load_events(events_jsonl)
    if not events:
        return '<p class="empty">No persisted events were available when this report was built.</p>'
    rows: list[str] = []
    for event in events:
        payload = event.get("payload", {})
        detail = _bounded(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            _MAX_EVENT_DETAIL_CHARS,
            "event",
        )
        duration = event.get("duration_ms")
        duration_text = "—" if duration is None else f"{duration} ms"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(event.get('sequence', '—')))}</td>"
            f"<td><code>{html.escape(str(event.get('type', 'unknown')))}</code></td>"
            f"<td>{html.escape(str(event.get('timestamp', '—')))}</td>"
            f"<td>{html.escape(duration_text)}</td>"
            "<td>"
            f"{html.escape(_event_summary(event))}"
            "<details><summary>Payload</summary>"
            f"<pre>{html.escape(detail)}</pre></details>"
            "</td></tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>#</th><th>Event</th>'
        f"<th>Timestamp</th><th>Duration</th><th>Details</th></tr></thead><tbody>{''.join(rows)}"
        "</tbody></table></div>"
    )


def _render_checks(scorecard: Scorecard) -> str:
    return "".join(
        '<li class="check"><span class="check-mark">'
        f"{'✓' if value else 'x'}</span><span>{html.escape(name.replace('_', ' ').title())}</span>"
        f"<strong>{'PASS' if value else 'FAIL'}</strong></li>"
        for name, value in scorecard.checks.model_dump().items()
    )


def _render_failures(scorecard: Scorecard) -> str:
    if not scorecard.failures:
        return '<p class="empty">No Quality Gate failures were recorded.</p>'
    return "".join(
        '<article class="failure"><strong>'
        f"{html.escape(failure.result.value)}</strong><p>{html.escape(failure.summary)}</p></article>"
        for failure in scorecard.failures
    )


def _render_artifacts(scorecard: Scorecard) -> str:
    items: list[str] = []
    for name, path in scorecard.artifacts.model_dump().items():
        filename = PurePosixPath(path).name
        items.append(
            f'<li><a href="{html.escape(filename, quote=True)}">{html.escape(name)}</a>'
            f"<code>{html.escape(path)}</code></li>"
        )
    return "".join(items)


def render_html(
    scorecard: Scorecard,
    *,
    patch: str = "",
    test_log: str = "",
    events_jsonl: str = "",
) -> str:
    metrics = scorecard.metrics
    result = html.escape(scorecard.result.value)
    status_class = "passed" if scorecard.result.value == "PASSED" else "failed"
    patch_preview = _display_text(
        _bounded(patch or "No patch was produced.", _MAX_REPORT_PATCH_CHARS, "patch")
    )
    test_preview = _display_text(
        _bounded(
            test_log or "No acceptance commands were run.",
            _MAX_REPORT_TEST_CHARS,
            "test log",
        )
    )
    task = html.escape(scorecard.task_id)
    run_id = html.escape(str(scorecard.run_id))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>PatchPilot {result} · {task}</title>
  <style>
    :root {{ --ink:#142033; --muted:#5d6a7d; --line:#dbe2ea; --panel:#fff;
      --bg:#f2f5f8; --accent:#315efb; --good:#087f5b; --bad:#c92a2a; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:14px/1.55 Inter,ui-sans-serif,system-ui,sans-serif;
      color:var(--ink); background:var(--bg); }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:32px auto 72px; }}
    header,section {{ background:var(--panel); border:1px solid var(--line); border-radius:16px;
      box-shadow:0 8px 28px rgba(20,32,51,.06); }}
    header {{ padding:28px; background:linear-gradient(135deg,#14213d,#243b6b); color:#fff; }}
    h1 {{ margin:4px 0 8px; font-size:clamp(26px,4vw,42px); line-height:1.1; }}
    h2 {{ margin:0 0 18px; font-size:20px; }}
    p {{ margin:6px 0; }}
    section {{ padding:24px; margin-top:18px; }}
    code,pre {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }}
    pre {{ margin:10px 0 0; padding:16px; overflow:auto; white-space:pre-wrap;
      overflow-wrap:anywhere; border-radius:10px; color:#dbe7ff; background:#0d1729; }}
    a {{ color:var(--accent); }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; opacity:.72; font-weight:700; }}
    .status {{ display:inline-block; margin-top:14px; padding:5px 11px; border-radius:999px;
      font-weight:800; letter-spacing:.04em; background:#fff; }}
    .status.passed {{ color:var(--good); }} .status.failed {{ color:var(--bad); }}
    .identity {{ opacity:.78; overflow-wrap:anywhere; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:12px; }}
    .metric {{ padding:16px; border:1px solid var(--line); border-radius:12px; background:#f8fafc; }}
    .metric strong {{ display:block; font-size:23px; }} .metric span {{ color:var(--muted); }}
    .checks {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px;
      padding:0; list-style:none; }}
    .check {{ display:grid; grid-template-columns:28px 1fr auto; align-items:center; gap:8px;
      padding:11px 12px; border:1px solid var(--line); border-radius:10px; }}
    .check-mark {{ font-size:20px; }}
    .table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }}
    td details {{ margin-top:5px; }} td details pre {{ max-width:620px; font-size:12px; }}
    .failure {{ border-left:4px solid var(--bad); padding:10px 14px; background:#fff5f5; }}
    .empty {{ color:var(--muted); }}
    .artifacts {{ display:grid; gap:8px; padding:0; list-style:none; }}
    .artifacts li {{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:12px;
      padding:9px 0; border-bottom:1px solid var(--line); }}
    @media (prefers-color-scheme:dark) {{ :root {{ --ink:#e5edf7; --muted:#aeb9c8;
      --line:#344155; --panel:#172033; --bg:#0f1726; }} .metric {{ background:#101a2b; }}
      .failure {{ background:#321c24; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">Auditable coding-agent run</div>
    <h1>{task}</h1>
    <p class="identity">Run {run_id}</p>
    <span class="status {status_class}">{result}</span>
  </header>
  <section aria-labelledby="metrics-heading">
    <h2 id="metrics-heading">Execution metrics</h2>
    <div class="metrics">
      <div class="metric"><strong>{metrics.steps}</strong><span>steps</span></div>
      <div class="metric"><strong>{metrics.model_calls}</strong><span>model calls</span></div>
      <div class="metric"><strong>{metrics.tool_calls}</strong><span>tool calls</span></div>
      <div class="metric"><strong>{metrics.prompt_tokens + metrics.completion_tokens}</strong><span>tokens</span></div>
      <div class="metric"><strong>${metrics.estimated_cost_usd}</strong><span>estimated cost</span></div>
      <div class="metric"><strong>{metrics.wall_time_seconds:.3f}s</strong><span>wall time</span></div>
    </div>
  </section>
  <section><h2>Deterministic Quality Gate</h2><ul class="checks">{_render_checks(scorecard)}</ul></section>
  <section><h2>Run timeline</h2>{_render_timeline(events_jsonl)}</section>
  <section><h2>Git diff</h2><p class="empty">{metrics.changed_files} changed files · +{metrics.added_lines}/-{metrics.deleted_lines} lines</p><pre>{html.escape(patch_preview)}</pre></section>
  <section><h2>Acceptance test result</h2><pre>{html.escape(test_preview)}</pre></section>
  <section><h2>Failure history</h2>{_render_failures(scorecard)}</section>
  <section><h2>Integrity-indexed artifacts</h2><ul class="artifacts">{_render_artifacts(scorecard)}</ul></section>
</main>
</body>
</html>
"""
