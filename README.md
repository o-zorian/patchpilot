# PatchPilot

PatchPilot is a controlled and auditable coding-agent harness. This repository currently
contains milestone M0 only: the versioned task protocol, CLI skeleton, configuration and
structured logging, Run state machine, and SQLite persistence.

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

## M0 security boundary

M0 never executes TaskSpec commands, calls a model API, or modifies the target repository.
It rejects absolute/traversing glob patterns, requires `.git/**` in denied paths, enforces
fixed Python acceptance-command syntax, and checks all user budgets against configurable
system hard limits. Workspace isolation and executable tools begin in M1 and are deliberately
not present here.

