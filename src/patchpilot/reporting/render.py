from __future__ import annotations

import html

from patchpilot.domain.scorecard import Scorecard


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


def render_html(scorecard: Scorecard) -> str:
    markdown = render_markdown(scorecard)
    escaped_report = html.escape(markdown)
    result = html.escape(scorecard.result.value)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>PatchPilot {result}</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:2rem auto;"
        "padding:0 1rem;color:#18212f}pre{white-space:pre-wrap;background:#f6f8fa;"
        "border:1px solid #d0d7de;border-radius:8px;padding:1rem}</style></head>"
        f"<body><h1>PatchPilot result: {result}</h1><pre>{escaped_report}</pre></body></html>\n"
    )
