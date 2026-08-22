# PatchPilot

PatchPilot is a controlled, auditable repository-level coding-agent harness for small Python and
Go repositories. A model can inspect code, apply a text patch, run fixed test/lint profiles, and
iterate on deterministic feedback—but it cannot execute arbitrary shell commands, widen its file
scope, expand its budget, or declare itself successful.

Milestones M0–M7 are implemented: versioned TaskSpecs, isolated Workspaces, structured tools, a
project-owned Agent Loop, deterministic Quality Gate, API/Worker service mode, Docker Sandbox,
20-task Benchmark, standalone reports, CI, and release demos.

## Why this exists

Coding-agent demos often stop when a model emits plausible code. PatchPilot instead preserves the
evidence needed to evaluate a run: append-only events, normalized model/tool metadata, token and
cost accounting, final Git patch, acceptance-test output, deterministic Scorecard, and Markdown
and standalone HTML reports. The original repository is never modified.

## Ten-minute offline quick start

Prerequisites: Git and Python 3.12 or newer. Go is needed only for Go tasks; Docker is needed only
for unknown-code and Sandbox verification. Commands below make no model API call.

```text
python -m venv .venv
```

Activate the environment (`.venv\Scripts\Activate.ps1` on PowerShell or
`source .venv/bin/activate` on POSIX), then run:

```text
python -m pip install -e ".[dev]"
patchpilot benchmark validate benchmarks/local-v1 --json
patchpilot benchmark run benchmarks/local-v1 --output benchmark-results/demo --strategy full --limit 1 --concurrency 1 --json
```

The final command runs one complete `full` strategy task with `ScriptedModelClient`, writes no
credentials, performs no model network request, cleans its temporary Workspace, and produces:

- `benchmark-results/demo/report.html`: Benchmark summary and strategy chart.
- `benchmark-results/demo/runs/<run-id>/report.html`: complete Run page with metrics, Quality Gate,
  event timeline, model/tool details, Diff, tests, failures, and artifact links.
- `events.jsonl`, `final.patch`, `test.log`, `scorecard.json`, and `report.md` beside the Run page.

The committed [minimal run report](benchmarks/local-v1/results/minimal/report.html) is a release
sample. The complete 80-run experiment is summarized in
[the published Benchmark report](benchmarks/local-v1/results/full/report.html).

## Architecture and Agent Loop

```text
Typer CLI / FastAPI -> Task & Run Service -> Redis -> Worker
                                              |
                                              v
          SQLite/PostgreSQL <- Event recorder + project-owned Agent Loop
                                                     |
                           Model adapter <-> structured Tool Registry
                                                     |
                               isolated Workspace -> Docker Sandbox
                                                     |
                               deterministic Quality Gate -> artifacts
```

The full component, sequence, and trust-boundary diagrams are in
[docs/architecture.md](docs/architecture.md). CLI, API, and Worker all call the same `RunExecutor`;
there is one Agent Loop rather than three behaviorally different implementations.

At each bounded step, PatchPilot sends the task, recent context, and registered JSON Schemas to the
model. Valid structured tool calls execute and return bounded structured results. Invalid or
unknown calls are rejected; three repeated invalid calls stop the run. Step, input/output token,
estimated cost, wall-time, changed-file, and patch-line budgets are enforced by application code.
A `finish` call only requests verification. Recoverable gate failures can return to the Agent at
most twice.

## Tool protocol

Inputs are strict Pydantic models and outputs use one envelope:

```json
{
  "ok": true,
  "tool": "read_file",
  "summary": "read cases.py lines 1-80",
  "data": {},
  "error": null,
  "truncated": false,
  "duration_ms": 3
}
```

Registered tools are `list_files`, `search_code`, `read_file`, `apply_patch`, `git_diff`,
`run_tests`, `run_linter`, and `finish`. File tools revalidate paths on every call. Test and lint
tools select application-defined profile IDs; model-authored command strings are not accepted.

## Safety boundary

- Original repositories remain unchanged; each Run receives a clean independent Git Workspace.
- Absolute paths, traversal, `.git`, common credential files, denied paths, and escaping symlinks
  are rejected.
- Repository subprocesses use argv arrays with `shell=False`; package installation, downloads,
  Git push, command shells, and credential reads are outside the profile.
- Production defaults to Docker. Containers are networkless, non-root, capability-free, read-only
  outside the Workspace, CPU/memory/PID/time bounded, and removed after every command.
- The Docker socket, SSH material, host credentials, and model API key are never mounted into the
  execution container.
- Events store bounded hashes and summaries rather than Authorization headers, full model payloads,
  or large repository output. Artifacts are indexed by SHA-256 and byte size.
- Real model execution is disabled unless `PATCHPILOT_ENABLE_REAL_MODEL=true` is explicitly set.

Trusted-local execution exists only for project-owned fixtures and the offline Benchmark. It is
not a security boundary for unknown repositories.

## Deterministic Quality Gate

The gate recomputes the Git diff, verifies allowed/denied paths and untracked files, checks changed
file and patch-line limits, runs configured static checks and every acceptance argv, verifies that
required test names actually ran, and rechecks runtime budgets. Deterministic code assigns results
such as `PASSED`, `NO_PATCH`, `TEST_FAILURE`, `SCOPE_VIOLATION`, `TIMEOUT`, or
`BUDGET_EXCEEDED`; the model never selects the result.

Every terminal gated Run writes `final.patch`, `events.jsonl`, `test.log`, `scorecard.json`,
`report.md`, and `report.html` under `ARTIFACT_ROOT/runs/<run-id>/`.

## Benchmark method and published result

`benchmarks/local-v1` contains 20 deterministic tasks: 12 Python, 8 Go, ten easy, ten medium, and
nine defect categories. Hidden tests are injected only during Quality Gate. The fixed offline
Scripted Model compares four policies:

1. `single_shot`: one response and no test feedback.
2. `agent_loop`: tool iteration without test feedback.
3. `agent_loop_with_tests`: test and gate feedback enabled.
4. `full`: list/search/read, tests, context compaction, and Quality Gate.

The committed one-repetition experiment contains 80 runs. It recorded 25% / 50% / 80% / 100% pass
rates for `single_shot`, `agent_loop`, `agent_loop_with_tests`, and `full`, respectively, with an
overall 63.75% pass rate. These are deterministic fixture results that demonstrate harness and
strategy behavior. They do **not** measure general model coding ability, estimate stochastic
variance, represent production repositories, or support a SWE-bench claim.

Run and compare fresh results:

```text
patchpilot benchmark run benchmarks/local-v1 --output benchmark-results/all
patchpilot benchmark compare benchmark-results/all/summary.json benchmarks/local-v1/results/full/summary.json
```

The output directory must be absent or empty. This Scripted Benchmark remains offline and keeps its
published behavior. Real-model experiments use the separate `real-*` commands and suites below.

## Real Benchmark v1 (experimental; no published score)

`benchmarks/real-calibration-v1` contains five non-scoring calibration tasks.
`benchmarks/real-v1` is a frozen 24-task Python/Go curated-snapshot suite: 5 easy, 12 medium, and
7 hard tasks across at least eight defect categories. It is independent of `local-v1`; no
ScriptedModel result is treated as a real-model result. Validate both suites without an API call:

```text
patchpilot benchmark real-validate benchmarks/real-calibration-v1 --verify-fixtures --json
patchpilot benchmark real-validate benchmarks/real-v1 --verify-fixtures --json
```

Paid commands require all three gates: `PATCHPILOT_ENABLE_REAL_MODEL=true`, a non-empty
`MODEL_API_KEY`, and the command-line `--real-model` flag. Positive input/output pricing is also
required so the global cost ceiling can be enforced. Run only calibration until the full 288-run
matrix (24 tasks × 4 strategies × 3 repetitions) has an approved budget:

```text
patchpilot benchmark real-ping --real-model --max-total-cost-usd 0.02 --json
patchpilot benchmark real-run benchmarks/real-calibration-v1 --output benchmark-results/real-calibration \
  --real-model --max-total-cost-usd 0.60 --strategy full --repetitions 1 --concurrency 1 --json
patchpilot benchmark real-estimate benchmarks/real-v1 \
  --calibration-raw benchmark-results/real-calibration/raw.jsonl --json
```

Completed runs are persisted immediately and skipped on resume. A request interrupted after it was
sent is recorded as unknown billed cost and is not automatically retried as a new paid Run. See
[`docs/real-benchmark-v1.md`](docs/real-benchmark-v1.md) for the protocol, artifacts, freeze rules,
and audit checklist. No formal real-model pass rate is published in this repository.

## Explicit real-model demo

This path incurs provider cost and is never used by CI. Prerequisites are a running Docker daemon,
the two local Sandbox images, and an OpenAI-compatible endpoint. First prepare the committed demo
fixture as a separate clean Git repository:

```text
python scripts/prepare_demo.py
docker build -t patchpilot-python:latest -f docker/python/Dockerfile .
docker build -t patchpilot-go:latest -f docker/go/Dockerfile .
```

Set configuration in your process environment, not in a committed file:

```dotenv
PATCHPILOT_ENABLE_REAL_MODEL=true
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_API_KEY=<set-only-in-your-shell-or-secret-manager>
MODEL_NAME=<openai-compatible-model-name>
MODEL_INPUT_COST_PER_MILLION_USD=<provider-input-price>
MODEL_OUTPUT_COST_PER_MILLION_USD=<provider-output-price>
SANDBOX_MODE=docker
```

Then run the low-budget demo (replace the option value with the same model name):

```text
patchpilot task validate examples/demo-task.yaml
patchpilot run execute examples/demo-task.yaml --model <openai-compatible-model-name> --strategy full --json
```

The TaskSpec caps the run at 10 steps, 20,000 input tokens, 4,000 output tokens, USD 0.05, one
changed file, and 20 patch lines. A model call or a plausible patch is not a pass; inspect the
Scorecard and HTML report. Never record the `.env` file, key, environment dump, or Authorization
header. Remove `demo-work/` after the demo; it is ignored by Git.

## API and Worker

Start PostgreSQL and Redis with the release Compose file, then run API and Worker on the host so
the Worker can safely create sibling Docker Sandbox containers with correct host Workspace paths:

```text
docker compose up -d postgres redis
patchpilot-api
patchpilot-worker
```

The entry points use `POSTGRES_DATABASE_URL` and `REDIS_URL`. FastAPI serves interactive OpenAPI at
`http://127.0.0.1:8000/docs` and JSON at `/openapi.json`. The versioned API prefix is `/api/v1` and
includes Task/Run creation, idempotent submission, owner-scoped reads, cancellation, paginated
events, SSE, integrity-checked artifacts, health, summary metrics, and Prometheus text metrics.

Production deployments must replace the example database password and provide persistent artifact
and Workspace storage. The Compose file intentionally supplies infrastructure only; containerizing
the Worker while bind-mounting Workspaces through a host Docker socket requires deployment-specific
path translation and is not claimed by this release.

## Development and verification

```text
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest -m "not docker"
patchpilot benchmark validate benchmarks/local-v1 --json
patchpilot benchmark run benchmarks/local-v1 --output benchmark-results/minimal --strategy full --limit 1 --concurrency 1 --json
```

Real Docker verification is opt-in and uses no model API:

```text
docker build -t patchpilot-python:latest -f docker/python/Dockerfile .
docker build -t patchpilot-go:latest -f docker/go/Dockerfile .
PATCHPILOT_RUN_DOCKER_TESTS=1 SANDBOX_DOCKER_BINARY=docker python -m pytest -m docker
```

GitHub Actions fixes `PATCHPILOT_ENABLE_REAL_MODEL=false` and empty model variables, runs formatting,
lint, strict typing, all deterministic tests, an offline minimal Benchmark, wheel build, and a
separate Docker isolation job. It cannot call a paid or real model endpoint.

## Demo and reference material

- [Under-three-minute recording script](docs/demo-script.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Versioned TaskSpec JSON Schema](schemas/task-spec-v1.json)
- [Local Benchmark design](benchmarks/local-v1/README.md)
- [Product requirements](docs/02-patchpilot-requirements.md)

## Known limitations

- The built-in Benchmark uses small synthetic repositories and a Scripted Model; its numbers are
  not a claim about real-model intelligence.
- Trusted-local mode cannot enforce network, filesystem, CPU, or memory isolation and must never run
  unknown code.
- Docker availability and Linux cgroup behavior are environment-dependent. CI verifies the Linux
  path; macOS/Windows require Docker Desktop and may expose different resource accounting.
- Service mode is single-owner by configuration. It preserves owner scoping but does not implement
  multi-user authentication or authorization.
- Provider compatibility is limited to OpenAI-compatible Chat Completions with structured tool
  calls. Provider-specific Responses APIs are outside this release.
- Benchmark hidden tests are suitable for local evaluation, not a defense against a malicious
  repository with host access; unknown code still requires Docker.
- This release does not push branches, create pull requests, modify the source repository, provide
  an IDE plugin, execute arbitrary shell, or coordinate multiple Agents.
