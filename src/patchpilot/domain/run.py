from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.PASSED,
        RunStatus.FAILED,
        RunStatus.TIMEOUT,
        RunStatus.BUDGET_EXCEEDED,
        RunStatus.CANCELLED,
        RunStatus.SYSTEM_ERROR,
    }
)


class RunActor(StrEnum):
    WORKER = "worker"
    SYSTEM = "system"


class RunStrategy(StrEnum):
    SINGLE_SHOT = "single_shot"
    AGENT_LOOP = "agent_loop"
    AGENT_LOOP_WITH_TESTS = "agent_loop_with_tests"
    AGENT_LOOP_WITH_SEARCH = "agent_loop_with_search"
    FULL = "full"


class InvalidRunTransition(ValueError):
    """Raised when a Run status transition violates domain rules."""


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {
            RunStatus.PREPARING,
            RunStatus.RUNNING,
            RunStatus.CANCELLED,
            RunStatus.SYSTEM_ERROR,
        }
    ),
    RunStatus.PREPARING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.SYSTEM_ERROR}
    ),
    RunStatus.RUNNING: TERMINAL_STATUSES,
}


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    status: RunStatus = RunStatus.PENDING
    strategy: RunStrategy
    model: str = Field(min_length=1, max_length=255)
    prompt_version: str = Field(default="1", min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=255)
    workspace_id: str | None = Field(default=None, max_length=255)
    step_count: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    result_code: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def transition(
        self,
        target: RunStatus,
        *,
        actor: RunActor,
        at: datetime | None = None,
    ) -> Run:
        if self.status.is_terminal:
            raise InvalidRunTransition(f"terminal Run {self.status} cannot transition to {target}")
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidRunTransition(f"Run cannot transition from {self.status} to {target}")
        if target in {RunStatus.PREPARING, RunStatus.RUNNING} and actor != RunActor.WORKER:
            raise InvalidRunTransition(f"only a worker may transition a Run to {target}")

        transition_time = at or datetime.now(UTC)
        updates: dict[str, object] = {"status": target}
        if target in {RunStatus.PREPARING, RunStatus.RUNNING} and self.started_at is None:
            updates["started_at"] = transition_time
        if target.is_terminal:
            updates["finished_at"] = transition_time
        return self.model_copy(update=updates)
