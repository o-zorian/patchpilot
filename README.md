# PatchPilot

PatchPilot is a controlled and auditable coding-agent harness. This repository currently
contains milestones M0 through M2: the versioned task protocol and persistence skeleton,
independent trusted-local Workspaces, controlled tools, an OpenAI-compatible model boundary,
offline scripted models, and a project-owned structured Agent Loop.

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

M2 deliberately stops at `finish_requested`. It does not mark a run passed, execute a
Quality Gate, produce final reports, or add API, Worker, Docker, Go, or Benchmark features;
those belong to later milestones.
