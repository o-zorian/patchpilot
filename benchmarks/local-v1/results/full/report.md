# PatchPilot Benchmark: local-v1

## Experiment configuration

- Task-set SHA-256: `220c7c8c44c44b52b518e6791c36f7ab8635e2257ee714b94b96068e8afdc115`
- Model: `scripted-benchmark-v1` (offline Scripted Model)
- Prompt version: `m6-v1`
- Repetitions: 1
- Strategies: single_shot, agent_loop, agent_loop_with_tests, full

## Overall metrics

- Runs: 80
- Pass rate: 63.7%
- First-gate pass rate: 56.2%
- Average steps/model calls/tool calls: 3.62 / 3.62 / 3.69
- Average tokens: 438.62 prompt / 90.94 completion
- Average estimated cost: $0.000356
- Average wall time: 2.779s
- Scope violation rate: 0.0%
- Regression rate: 0.0%

## Cost and pass-rate comparison

| Strategy | Runs | Pass rate | First gate | Avg cost USD | Avg wall s |
|---|---:|---:|---:|---:|---:|
| agent_loop | 20 | 50.0% | 50.0% | 0.000234 | 1.210 |
| agent_loop_with_tests | 20 | 80.0% | 50.0% | 0.000385 | 4.883 |
| full | 20 | 100.0% | 100.0% | 0.000710 | 4.174 |
| single_shot | 20 | 25.0% | 25.0% | 0.000093 | 0.849 |

## Pass rate by defect category

| Defect | Runs | Pass rate |
|---|---:|---:|
| boundary | 12 | 75.0% |
| cache | 8 | 50.0% |
| exception | 4 | 50.0% |
| json-api | 12 | 58.3% |
| null-handling | 8 | 75.0% |
| pagination | 8 | 75.0% |
| path | 12 | 58.3% |
| sorting | 8 | 75.0% |
| sql-condition | 8 | 50.0% |

## Successful cases

- `py-001` / `agent_loop`: PASSED; steps=3, tools=3
- `py-002` / `agent_loop`: PASSED; steps=3, tools=3
- `py-003` / `agent_loop`: PASSED; steps=3, tools=3

## Failed cases

- `go-001` / `agent_loop`: NO_PATCH; steps=2, tools=2
- `go-002` / `agent_loop`: NO_PATCH; steps=2, tools=2
- `go-003` / `agent_loop`: NO_PATCH; steps=2, tools=2

## Failure analysis

- `NO_PATCH`: 29

Failures in this deterministic fixture run primarily indicate strategy capability limits or a Quality Gate rejection, not random model behavior.

## Conclusions this benchmark cannot support

- Results use a deterministic Scripted Model and measure harness behavior, not model intelligence.
- The local suite contains small synthetic repositories and does not represent SWE-bench.
- One repetition is reproducible but cannot estimate variance from stochastic real models.
- Trusted-local execution is reserved for project-owned fixtures; unknown repositories require Docker.
