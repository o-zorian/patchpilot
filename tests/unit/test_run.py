from __future__ import annotations

from uuid import uuid4

import pytest

from patchpilot.domain.run import (
    InvalidRunTransition,
    Run,
    RunActor,
    RunStatus,
    RunStrategy,
)


def make_run() -> Run:
    return Run(task_id=uuid4(), strategy=RunStrategy.FULL, model="scripted-test")


def test_worker_can_drive_run_to_terminal_state() -> None:
    pending = make_run()

    preparing = pending.transition(RunStatus.PREPARING, actor=RunActor.WORKER)
    running = preparing.transition(RunStatus.RUNNING, actor=RunActor.WORKER)
    passed = running.transition(RunStatus.PASSED, actor=RunActor.WORKER)

    assert pending.status == RunStatus.PENDING
    assert preparing.started_at is not None
    assert running.started_at == preparing.started_at
    assert passed.finished_at is not None
    assert passed.status.is_terminal


def test_only_worker_can_start_run() -> None:
    with pytest.raises(InvalidRunTransition, match="only a worker"):
        make_run().transition(RunStatus.RUNNING, actor=RunActor.SYSTEM)


def test_terminal_state_cannot_transition() -> None:
    cancelled = make_run().transition(RunStatus.CANCELLED, actor=RunActor.SYSTEM)

    with pytest.raises(InvalidRunTransition, match="terminal"):
        cancelled.transition(RunStatus.RUNNING, actor=RunActor.WORKER)


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidRunTransition, match="cannot transition"):
        make_run().transition(RunStatus.PASSED, actor=RunActor.WORKER)
