# Real Benchmark v1 protocol

Real Benchmark v1 is an experimental, auditable evaluation path. It does not replace the
deterministic `benchmarks/local-v1` Scripted Benchmark, and calibration results never count toward a
formal score. No real-model score is committed until the complete frozen matrix has actually run.

## Suites and provenance

- `benchmarks/real-calibration-v1`: five non-frozen calibration tasks for provider, budget,
  Sandbox, report, and difficulty checks.
- `benchmarks/real-v1`: 24 frozen curated-snapshot tasks, split evenly between Python and Go, with
  5 easy, 12 medium, and 7 hard tasks. Ten reference repairs change two files.
- `SOURCES.yaml`: per-task provenance, license, attribution, hidden-test target, reference-patch
  path, and expected changed-file count.
- Repository `LICENSE` files identify the MIT terms. These are project-authored cropped fixtures,
  not executions of complete upstream projects or claims about upstream bug history.

The manifest SHA-256 identifies `benchmark.yaml`. The task-set SHA-256 additionally covers every
TaskSpec, repository tree, hidden test, reference patch, and source registry. Any task correction
changes the task-set hash and must be reviewed before a paid formal run.

## Safety and paid-run gates

Offline validation, CI, pytest, and `local-v1` force real-model execution off. A paid request is
possible only when all of these conditions hold:

1. `.env` or the process environment sets `PATCHPILOT_ENABLE_REAL_MODEL=true`.
2. `MODEL_API_KEY` and `MODEL_NAME` are present.
3. Positive `MODEL_INPUT_COST_PER_MILLION_USD` and
   `MODEL_OUTPUT_COST_PER_MILLION_USD` values are configured.
4. The command includes `--real-model` and a positive `--max-total-cost-usd` hard ceiling.

The key stays in the host HTTP client. It is not logged, persisted, passed in argv, copied to a
Workspace, or mounted into Docker. Docker acceptance commands run without network as UID/GID
10001, with a read-only root filesystem, dropped capabilities, no-new-privileges, and CPU, memory,
PID, and wall-time limits. Hidden tests exist outside the model-visible repository and are copied
only by QualityGate for the test interval; their names and assertions are redacted from model
feedback and the injected file is removed afterward.

## Fixed comparison protocol

The formal manifest fixes `single_shot`, `agent_loop`, `agent_loop_with_tests`, and `full` in that
order, with three repetitions, temperature zero, prompt `real-v1`, and concurrency one by default.
All strategies receive the same task statement and bounded repository source snapshot. A formal
run rejects task limits, strategy subsets, and repetition overrides, preventing selective reruns.
Only transport 429, 5xx, and timeout errors receive bounded adapter retries; task failures are
scored once and are never selectively replayed.

Before a request, the runner reserves a conservative maximum request cost. A response settles the
reservation using provider token usage (or a clearly marked estimate). A timeout after dispatch is
retained as unknown billed cost. Completed Run keys are skipped on resume; a started but
uncompleted request becomes an interrupted record and is not charged again by automatic replay.

## Reproducibility record

`experiment.json` records the PatchPilot commit and dirty-status hash, manifest/task-set hashes,
baseline repository tree hashes and deterministic commits, hidden-test hashes, Docker image IDs and
RepoDigests, TaskSpec and prompt versions, strategy matrix, sanitized model configuration, and
Python, Go, Docker, OS, architecture, and run timestamps. It deliberately excludes the API key,
base URL, headers, `.env` content, prompts, responses, and full provider payloads.

Each Run is persisted immediately under `runs/<run-id>/` with `events.jsonl`, `final.patch`,
`test.log`, `scorecard.json`, `report.md`, and `report.html`. The suite directory contains append-only
`attempts.jsonl`, completed `raw.jsonl`, `summary.json`, `report.md`, and `report.html`.
`outcome_class` distinguishes deterministic task results from API errors, infrastructure errors,
and interrupted requests.

## Required sequence

With real execution forcibly disabled, run formatting, lint, strict typing, all pytest suites,
release/security tests, Docker tests, fixture audits, and the minimal offline Benchmark. Then load
the private `.env` without printing it and run:

```text
patchpilot benchmark real-ping --real-model --max-total-cost-usd 0.02 --json
patchpilot benchmark real-run benchmarks/real-calibration-v1 \
  --output benchmark-results/real-calibration-one --real-model \
  --max-total-cost-usd 0.12 --strategy full --repetitions 1 --limit 1 --concurrency 1 --json
patchpilot benchmark real-run benchmarks/real-calibration-v1 \
  --output benchmark-results/real-calibration-five --real-model \
  --max-total-cost-usd 0.60 --strategy full --repetitions 1 --limit 5 --concurrency 1 --json
patchpilot benchmark real-estimate benchmarks/real-v1 \
  --calibration-raw benchmark-results/real-calibration-five/raw.jsonl --json
```

Stop after calibration and review actual tokens, cost, latency, cleanup audits, expected matrix
cost, worst-case task-budget cost, estimated elapsed time, and rate-limit risk. Do not start the
288-run formal matrix until the owner explicitly approves the global budget and the task-set hash.

## Full-strategy high-budget ablation

`benchmarks/real-v1/experiments/real-v1-full-high-budget-v1.yaml` defines an independent,
exploratory budget ablation. It fixes all 24 frozen tasks, only the `full` strategy, three
repetitions, concurrency one, a $10 global hard limit, and wide emergency Run limits. The profile
cannot change scope, acceptance, Docker isolation, hidden tests, QualityGate scoring, prompt,
temperature, or patch-size constraints. Its SHA-256 and budget configuration participate in a new
experiment fingerprint, so it cannot resume or overwrite another experiment.

Validate without network access, then explicitly authorize the paid run into its separate output
directory:

```text
patchpilot benchmark real-experiment-validate benchmarks/real-v1 \
  --profile benchmarks/real-v1/experiments/real-v1-full-high-budget-v1.yaml --json
patchpilot benchmark real-experiment-run benchmarks/real-v1 \
  --profile benchmarks/real-v1/experiments/real-v1-full-high-budget-v1.yaml \
  --output benchmark-results/real-v1-full-high-budget-v1 \
  --max-total-cost-usd 10.00 --real-model --json
```

The resulting report is labeled `full strategy high-budget ablation`. Its different Run budget
makes it unsuitable for replacing the original `real-v1` full score or for the original
equal-budget four-strategy ranking.
