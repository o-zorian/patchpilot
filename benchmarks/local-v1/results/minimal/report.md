# PatchPilot Benchmark: local-v1

## Experiment configuration

- Task-set SHA-256: `220c7c8c44c44b52b518e6791c36f7ab8635e2257ee714b94b96068e8afdc115`
- Model: `scripted-benchmark-v1` (offline Scripted Model)
- Prompt version: `m6-v1`
- Repetitions: 1
- Strategies: full

## Overall metrics

- Runs: 1
- Pass rate: 100.0%
- First-gate pass rate: 100.0%
- Average steps/model calls/tool calls: 7.00 / 7.00 / 7.00
- Average tokens: 896.00 prompt / 175.00 completion
- Average estimated cost: $0.000710
- Average wall time: 3.242s
- Scope violation rate: 0.0%
- Regression rate: 0.0%

## Cost and pass-rate comparison

| Strategy | Runs | Pass rate | First gate | Avg cost USD | Avg wall s |
|---|---:|---:|---:|---:|---:|
| full | 1 | 100.0% | 100.0% | 0.000710 | 3.242 |

## Pass rate by defect category

| Defect | Runs | Pass rate |
|---|---:|---:|
| boundary | 1 | 100.0% |

## Successful cases

- `py-001` / `full`: PASSED; steps=7, tools=7

## Failed cases

- No cases available in this filtered run.

## Failure analysis

- None

Failures in this deterministic fixture run primarily indicate strategy capability limits or a Quality Gate rejection, not random model behavior.

## Conclusions this benchmark cannot support

- Results use a deterministic Scripted Model and measure harness behavior, not model intelligence.
- The local suite contains small synthetic repositories and does not represent SWE-bench.
- One repetition is reproducible but cannot estimate variance from stochastic real models.
- Trusted-local execution is reserved for project-owned fixtures; unknown repositories require Docker.
