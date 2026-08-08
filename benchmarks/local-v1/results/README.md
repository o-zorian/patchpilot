# Published Benchmark evidence

`full/summary.json`, `full/report.md`, and `full/report.html` are the aggregate evidence for the
80-run, four-strategy M6 experiment. `minimal/` is the complete one-run audit sample and includes
raw JSONL plus the run's events, patch, test log, Scorecard, and reports.

Release cleanup intentionally retains only representative per-run evidence rather than every
generated Workspace artifact. Recreate all raw runs with:

```text
patchpilot benchmark run benchmarks/local-v1 --output benchmark-results/local-v1
```

Benchmark execution is offline and uses `ScriptedModelClient`; it never reads model credentials
or calls a model API.
