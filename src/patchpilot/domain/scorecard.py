from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class QualityResult(StrEnum):
    PASSED = "PASSED"
    NO_PATCH = "NO_PATCH"
    TEST_FAILURE = "TEST_FAILURE"
    REGRESSION = "REGRESSION"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    PATCH_TOO_LARGE = "PATCH_TOO_LARGE"
    REQUIRED_TEST_NOT_RUN = "REQUIRED_TEST_NOT_RUN"
    TIMEOUT = "TIMEOUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INVALID_TOOL_LOOP = "INVALID_TOOL_LOOP"
    MODEL_ERROR = "MODEL_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    CANCELLED = "CANCELLED"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class GateChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    has_patch: bool
    scope_valid: bool
    patch_size_valid: bool
    tests_passed: bool
    required_tests_ran: bool
    budget_valid: bool


class ScorecardMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    changed_files: int = Field(default=0, ge=0)
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    wall_time_seconds: float = Field(ge=0)

    @field_serializer("estimated_cost_usd", when_used="json")
    def serialize_estimated_cost(self, value: Decimal) -> float:
        return float(value)


class ScorecardArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    patch: str
    events: str
    test_log: str
    scorecard: str
    report_markdown: str
    report_html: str


class GateFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: QualityResult
    summary: str = Field(min_length=1, max_length=2_000)
    failed_tests: list[str] = Field(default_factory=list, max_length=500)
    error_excerpt: str = Field(default="", max_length=4_000)


class Scorecard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    run_id: UUID
    task_id: str
    result: QualityResult
    checks: GateChecks
    metrics: ScorecardMetrics
    failures: list[GateFailure] = Field(default_factory=list)
    artifacts: ScorecardArtifacts


class GateOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: QualityResult
    scorecard: Scorecard
    recoverable: bool = False
    feedback: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.result == QualityResult.PASSED
