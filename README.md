# PatchPilot

PatchPilot is a controlled and auditable coding-agent harness. This repository currently
contains milestones M0 through M3: the versioned task protocol and persistence skeleton,
independent trusted-local Workspaces, controlled tools, an OpenAI-compatible model boundary,
offline scripted models, a project-owned Agent Loop, and deterministic Quality Gate reports.

## M0 quick start

```text
python -m pip install -e ".[dev]"
patchpilot task validate path/to/task.yaml
patchpilot task schema --output schemas/task-spec-v1.json
patchpilot db upgrade
patchpilot run create path/to/task.yaml --model scripted-test --strategy full
patchpilot run show RUN_ID
```

Repository paths in a TaskSpec are resolved relative to the TaskSpec file. M0 accepts only
the Python P0 acceptance profile (`python -m pytest` plus restricted selectors). Creating a
Run validates and snapshots the TaskSpec, applies pending SQLite migrations, and persists a
Run in `pending`; it does not invoke a model or execute repository code.

## M1 controlled local tools

`WorkspaceManager` creates an independent clone under `WORKSPACE_ROOT` after confirming the
source repository is clean. Every tool call resolves paths again; absolute paths, traversal,
Git metadata, common Secret files, out-of-scope writes, and escaping symbolic links are
rejected. Patch application supports atomic text create/update/delete operations and checks
the TaskSpec file/line budgets before and after application.

The local Python profile executes only application-defined `python -m pytest` argv arrays.
It never uses a shell, strips model/API credentials from the child environment, caps output,
and terminates the complete process tree on timeout. This trusted-local runner is intended
only for project-owned fixtures and benchmarks. It does not claim Docker-grade network,
filesystem, CPU, or memory isolation; unknown repositories remain out of scope until the
Docker sandbox milestone.

## M2 Agent Loop

The Agent Loop builds a bounded task prompt, exposes only registered Pydantic tool schemas,
feeds every structured tool result back to the model, and stops on a `finish` request,
repeated invalid calls, a model failure, or a hard step, token, cost, or wall-time budget.
Transient 429, 5xx, and timeout failures use bounded exponential backoff with jitter. Model
and tool payloads are not copied into events; event records contain identifiers, hashes,
small summaries, usage, cost, and timing data.

`ScriptedModelClient` and `FakeModelClient` perform no network I/O and are used by the test
suite. `OpenAICompatibleClient` is the explicit production adapter and requires both
`MODEL_NAME` and `MODEL_API_KEY`. Each run can write append-only `events.jsonl` and the same
semantic events to SQLite, with normalized model-call and tool-call trace rows.

Without a configured Quality Gate, the M2 loop still stops safely at `finish_requested` and
never marks a run passed.

## M3 Quality Gate and reports

The M3 runner attaches a deterministic Quality Gate to the same Agent Loop. A `finish` call
causes the application—not the model—to recompute the complete Git diff from the Workspace
baseline, validate every tracked and untracked path, enforce file and line budgets, run all
configured acceptance commands, and verify required test names from pytest JUnit output.
Recoverable `NO_PATCH`, `TEST_FAILURE`, `REGRESSION`, and `REQUIRED_TEST_NOT_RUN` results can
be returned to the Agent at most twice. Scope, sandbox, timeout, and hard-budget failures do
not loop indefinitely.

Every terminal M3 execution writes `final.patch`, `test.log`, `scorecard.json`, `report.md`,
`report.html`, and `events.jsonl` below its run-scoped artifact directory. Artifact writes are
atomic and indexed in SQLite with SHA-256 and byte size. Reports contain deterministic check
results and bounded summaries; full patch and test output stay in artifacts rather than event
payloads.

M3 does not add FastAPI, PostgreSQL, Redis, asynchronous Workers, SSE, cancellation, Docker,
Go, or Benchmark execution. Trusted-local mode remains limited to project-owned fixtures.
