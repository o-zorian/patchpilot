# PatchPilot architecture

PatchPilot is a modular monolith: the CLI, FastAPI process, and Redis Worker share the same
`RunService`, `RunExecutor`, project-owned Agent Loop, tool registry, Quality Gate, and artifact
store. There is no second execution implementation hidden behind the API.

```mermaid
flowchart TB
    U["User / API client"] --> CLI["Typer CLI"]
    U --> API["FastAPI /api/v1"]
    CLI --> SVC["Task & Run Service"]
    API --> SVC
    SVC --> DB[("SQLite or PostgreSQL")]
    SVC --> Q["Redis Run Queue"]
    Q --> W["Run Worker"]
    CLI --> EXEC["Shared RunExecutor"]
    W --> EXEC
    EXEC --> LOOP["Project-owned Agent Loop"]
    LOOP --> MODEL["OpenAI-compatible adapter"]
    LOOP --> REG["Pydantic Tool Registry"]
    LOOP --> EVT["Event recorder"]
    REG --> WS["Per-run Workspace"]
    WS --> BOX["Networkless Docker Sandbox"]
    BOX --> TOOLS["Search · Read · Patch · Test · Lint"]
    LOOP --> GATE["Deterministic Quality Gate"]
    GATE --> ART["SHA-256 indexed artifacts"]
    EVT --> ART
    ART --> REPORT["Standalone HTML report"]
```

## Run sequence

```mermaid
sequenceDiagram
    participant User
    participant Service
    participant Worker
    participant Agent as Agent Loop
    participant Model
    participant Sandbox
    participant Gate as Quality Gate

    User->>Service: Submit versioned TaskSpec
    Service-->>User: Return pending run_id
    Service->>Worker: Enqueue run_id
    Worker->>Agent: Create isolated Workspace
    loop Bounded steps and budgets
        Agent->>Model: Messages + registered JSON Schemas
        Model-->>Agent: Structured tool calls
        Agent->>Sandbox: Fixed argv / scoped file operation
        Sandbox-->>Agent: Bounded structured result
    end
    Agent->>Gate: finish requests verification
    Gate->>Sandbox: Run deterministic acceptance commands
    Gate-->>Agent: Pass or bounded failure feedback
    Gate-->>User: Patch, events, tests, Scorecard, reports
```

## Real Benchmark v1 execution

```mermaid
flowchart LR
    CLI["Explicit real-* CLI"] --> GATE{"enable flag + key + --real-model"}
    GATE --> HOST["Host-only OpenAI-compatible adapter"]
    HOST --> CAP["Per-request reservation + global cost ledger"]
    CAP --> LOOP["Same bounded Agent Loop"]
    LOOP --> WS["Disposable Git Workspace"]
    WS --> DOCKER["Non-root, networkless Docker Sandbox"]
    DOCKER --> QG["Quality Gate injects hidden test"]
    QG --> RUN["Run artifacts + atomic raw.jsonl checkpoint"]
    RUN --> REPORT["summary.json + Markdown/HTML reports"]
    QG -. "hidden source and assertions withheld" .-> LOOP
```

The benchmark copies a curated repository snapshot and creates a deterministic baseline commit;
the committed snapshot itself is hash-checked before and after every fixture audit. Model calls run
only on the host. The API key and Authorization header are never placed in the Workspace, Docker
environment, event stream, reports, or command arguments. Each formal task receives the same
bounded repository snapshot, model, prompt version, temperature, and task budget under all four
strategies; only the documented policy capabilities differ.

## Trust boundaries

- Model output is untrusted. It can select only registered tools with validated Pydantic inputs.
- Every file operation resolves the target below the per-run Workspace and rejects traversal,
  denied paths, and escaping symbolic links.
- Commands are application-owned argv arrays. PatchPilot never uses `shell=True` for repository
  execution.
- Unknown code runs in a non-root Docker container with no network, a read-only root filesystem,
  bounded CPU/memory/PIDs, and no Docker socket or model credential.
- The model API call happens in the host Worker. `MODEL_API_KEY` is excluded from Workspace and
  sandbox environments and is never stored in events.
- A model `finish` call is only a request. Deterministic code assigns the final result after scope,
  patch-budget, acceptance-test, required-test, and runtime-budget checks.

## Persistence and artifacts

SQLite supports local CLI work. PostgreSQL plus Redis supports asynchronous service mode. Event
rows contain bounded metadata; large outputs stay in run-scoped artifacts. Patch, JSONL events,
test log, Scorecard, Markdown report, and HTML report are atomically written and indexed by byte
size and SHA-256.
