# PatchPilot

PatchPilot is a controlled and auditable coding-agent harness. This repository currently
contains milestones M0 and M1: the versioned task protocol, CLI and persistence skeleton,
independent trusted-local Workspaces, strict path policy, structured file/search/patch/diff
tools, and the Python pytest command profile.

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

M1 does not include a model client, Agent Loop, event stream, Quality Gate, API, Worker, Go
profile, or Benchmark runner.
