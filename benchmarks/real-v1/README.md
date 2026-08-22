# PatchPilot Real Benchmark v1

`real-v1` is the frozen-candidate formal suite for paid OpenAI-compatible model runs.
It is fully separate from `local-v1`; no ScriptedModel patches or published offline
results are used here.

The repositories are honestly labeled project-authored **curated snapshots** under MIT,
not full upstream projects. `SOURCES.yaml` is the provenance and attribution registry.
Reference fixes and hidden tests live outside model-visible repositories. The runner copies
and initializes each repository deterministically, and QualityGate alone injects hidden tests.

The suite has 24 tasks: 12 Python and 12 Go; 5 easy, 12 medium, and 7 hard; 10 tasks have
reference fixes spanning 2 files. A formal run always uses all four strategies and three
repetitions. Partial or selective formal runs are rejected.

Validation does not call a model:

```text
patchpilot benchmark real-validate benchmarks/real-v1 --verify-fixtures --json
```

Do not launch the formal paid matrix until its manifest hash and total cost budget have been
reviewed and approved.
