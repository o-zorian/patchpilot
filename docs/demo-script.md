# PatchPilot release demo script (2:45)

This recording uses only the offline Scripted Model. It is deterministic, incurs no model cost,
and is safe to repeat. Start with a clean checkout and an activated Python 3.12+ virtual
environment where `python -m pip install -e ".[dev]"` has completed.

## 0:00–0:20 — Frame the problem

Show `README.md` and say:

> PatchPilot is a controlled repository-level coding-agent harness. The interesting part is not
> code generation alone; it is the auditable loop around an untrusted model: scoped tools,
> isolated execution, deterministic verification, budgets, and reproducible evaluation.

## 0:20–0:45 — Show the contract and safety boundary

Open `benchmarks/local-v1/tasks/py-001.yaml`. Point out the goal, `allowed_paths`, fixed pytest
argv, required test, and hard budgets. Briefly show `docs/architecture.md` and explain that CLI,
API, and Worker share one execution path.

## 0:45–1:10 — Validate the release Benchmark

Run:

```text
patchpilot benchmark validate benchmarks/local-v1 --json
```

Call out 20 tasks, the 12 Python / 8 Go split, nine defect categories, and four strategies.

## 1:10–1:45 — Run one complete offline Agent task

Run:

```text
patchpilot benchmark run benchmarks/local-v1 --output benchmark-results/demo --strategy full --limit 1 --concurrency 1 --json
```

Explain that the Scripted Model performs list, search, read, patch, test, diff, and finish calls.
It cannot use the network and `finish` still triggers the deterministic Quality Gate. Point out
that the source fixture remains unchanged and the temporary Workspace is removed.

## 1:45–2:25 — Inspect evidence, not a claim

Open `benchmark-results/demo/runs/<run-id>/report.html`. Scroll through execution metrics,
Quality Gate checks, the event timeline, tool/model metadata, Git diff, test output, failures, and
integrity-indexed artifacts. Then open `benchmark-results/demo/report.html` to show the strategy
comparison report format.

## 2:25–2:45 — Close with limits

Say:

> This offline result validates harness behavior, not model intelligence. The included Benchmark
> is synthetic and does not support SWE-bench claims. Real model execution is separately gated by
> `PATCHPILOT_ENABLE_REAL_MODEL=true`, requires an API key, and still runs repository code only in
> Docker.

Stop the recording. Do not display a `.env` file, terminal environment dump, API key, or an
Authorization header.
