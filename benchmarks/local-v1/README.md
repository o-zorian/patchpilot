# PatchPilot local-v1 benchmark

This M6 suite contains 20 deterministic repair tasks: 12 Python and 8 Go tasks.
The repositories under `repos/` are immutable templates. The runner copies each template,
initializes Git metadata in a temporary preparation directory, and then creates the normal
PatchPilot Workspace clone. It never runs against or writes to the templates directly.

Hidden tests under `hidden_tests/` and scripted model patches under `scripted_model/` are
runner-only inputs. Neither directory is copied into an Agent-visible Workspace. Hidden tests
are injected after diff/scope inspection, executed by the deterministic Quality Gate, and
removed before any feedback round. Failure feedback redacts hidden assertions and source.

Run all four strategies with:

```text
patchpilot benchmark run benchmarks/local-v1 --output artifacts/benchmark-local-v1
```

This suite uses only the offline `ScriptedModelClient`. Its results assess harness and strategy
behavior, not real-model coding quality.
