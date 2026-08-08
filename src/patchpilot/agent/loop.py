from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from patchpilot.agent.budget import BudgetSnapshot, BudgetStopReason, BudgetTracker
from patchpilot.agent.events import EventEmitter, EventType
from patchpilot.agent.prompts import PROMPT_VERSION, build_initial_messages
from patchpilot.agent.registry import FinishInput, ToolRegistry
from patchpilot.domain.cancellation import CancellationToken
from patchpilot.domain.run import RunStatus
from patchpilot.domain.scorecard import QualityResult, Scorecard, ScorecardMetrics
from patchpilot.models.base import (
    Message,
    MessageRole,
    ModelClient,
    ModelClientError,
    ModelConfig,
    ModelResponse,
    ModelTimeoutError,
    ToolCall,
)
from patchpilot.quality.gate import QualityGate
from patchpilot.tools.base import ToolContext


class AgentLoopStatus(StrEnum):
    FINISH_REQUESTED = "finish_requested"
    PASSED = "passed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"


class AgentMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: int
    model_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal
    wall_time_seconds: float

    @classmethod
    def from_snapshot(cls, snapshot: BudgetSnapshot) -> AgentMetrics:
        return cls(
            steps=snapshot.steps,
            model_calls=snapshot.model_calls,
            tool_calls=snapshot.tool_calls,
            prompt_tokens=snapshot.prompt_tokens,
            completion_tokens=snapshot.completion_tokens,
            estimated_cost_usd=snapshot.estimated_cost_usd,
            wall_time_seconds=snapshot.wall_time_seconds,
        )

    def to_scorecard_metrics(self) -> ScorecardMetrics:
        return ScorecardMetrics(
            steps=self.steps,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            estimated_cost_usd=self.estimated_cost_usd,
            wall_time_seconds=self.wall_time_seconds,
        )

    @classmethod
    def from_scorecard_metrics(cls, metrics: ScorecardMetrics) -> AgentMetrics:
        return cls(
            steps=metrics.steps,
            model_calls=metrics.model_calls,
            tool_calls=metrics.tool_calls,
            prompt_tokens=metrics.prompt_tokens,
            completion_tokens=metrics.completion_tokens,
            estimated_cost_usd=metrics.estimated_cost_usd,
            wall_time_seconds=metrics.wall_time_seconds,
        )


class AgentLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: AgentLoopStatus
    result_code: str
    error_code: str | None = None
    finish_request: FinishInput | None = None
    scorecard: Scorecard | None = None
    metrics: AgentMetrics
    prompt_version: str = PROMPT_VERSION


class _BudgetReached(RuntimeError):
    def __init__(self, reason: BudgetStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _RunCancelled(RuntimeError):
    pass


Sleep = Callable[[float], Awaitable[None]]


class AgentLoop:
    """Project-owned structured tool loop with deterministic safety boundaries."""

    def __init__(
        self,
        *,
        model_client: ModelClient,
        model_config: ModelConfig,
        tool_context: ToolContext,
        registry: ToolRegistry,
        events: EventEmitter,
        sleep: Sleep = asyncio.sleep,
        random_source: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        quality_gate: QualityGate | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.model_client = model_client
        self.model_config = model_config
        self.tool_context = tool_context
        self.registry = registry
        self.events = events
        self.sleep = sleep
        self.random = random_source or random.Random()
        self.budget = BudgetTracker(tool_context.task_spec.budget, model_config, clock=clock)
        self.quality_gate = quality_gate
        self.cancellation_token = cancellation_token or tool_context.cancellation_token

    async def run(self, run_id: UUID) -> AgentLoopResult:
        if run_id != self.events.run_id:
            raise ValueError("AgentLoop run_id must match EventEmitter run_id")
        messages = build_initial_messages(self.tool_context)
        await self.events.emit(
            EventType.RUN_CREATED,
            {
                "task_id": self.tool_context.task_spec.id,
                "model": self.model_config.model,
                "prompt_version": PROMPT_VERSION,
            },
        )
        await self.events.emit(
            EventType.WORKSPACE_READY,
            {
                "workspace_id": self.tool_context.workspace.id,
                "baseline_commit": self.tool_context.workspace.baseline_commit,
                "initial_git_status": self.tool_context.workspace.initial_git_status,
            },
        )
        invalid_fingerprint: str | None = None
        repeated_invalid_calls = 0
        gate_attempts = 0
        gate_feedbacks = 0

        while True:
            gate_feedback_received = False
            if self.cancellation_token.is_cancelled:
                return await self._cancelled_result(run_id)
            stop_reason = self.budget.before_step()
            if stop_reason is not None:
                return await self._budget_result(run_id, stop_reason)
            self.budget.begin_step()
            try:
                response, attempt = await self._request_model(messages)
            except _BudgetReached as exc:
                return await self._budget_result(run_id, exc.reason)
            except _RunCancelled:
                return await self._cancelled_result(run_id)
            except ModelClientError as exc:
                return await self._failure_result(
                    run_id,
                    result_code="MODEL_ERROR",
                    error_code=exc.code,
                )

            budget_after_usage = self.budget.record_usage(response.usage)
            call_cost = self.model_config.cost_for(response.usage)
            await self.events.emit(
                EventType.MODEL_RESPONDED,
                {
                    "request_sequence": self.budget.steps,
                    "attempt": attempt,
                    "model": response.model,
                    "status": "completed",
                    "error_code": None,
                    "finish_reason": response.finish_reason,
                    "provider_request_id": response.provider_request_id,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "usage_estimated": response.usage.estimated,
                    "estimated_cost_usd": str(call_cost),
                },
                duration_ms=response.latency_ms,
            )
            if budget_after_usage is not None:
                return await self._budget_result(run_id, budget_after_usage)

            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if not response.tool_calls:
                continue

            for call in response.tool_calls:
                if self.cancellation_token.is_cancelled:
                    return await self._cancelled_result(run_id)
                wall_stop = self.budget.check_non_step_limits()
                if wall_stop is not None:
                    return await self._budget_result(run_id, wall_stop)
                input_summary = self._tool_input_summary(call)
                await self.events.emit(
                    EventType.TOOL_REQUESTED,
                    {
                        "request_sequence": self.budget.steps,
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "input_summary": input_summary,
                    },
                )
                await self.events.emit(
                    EventType.TOOL_STARTED,
                    {
                        "request_sequence": self.budget.steps,
                        "tool_call_id": call.id,
                        "tool": call.name,
                    },
                )
                execution = await asyncio.to_thread(
                    self.registry.execute,
                    call,
                    timeout_seconds=self.budget.remaining_wall_time_seconds,
                )
                self.budget.record_tool_call()
                result = execution.result
                error_code = result.error.code if result.error is not None else None
                event_type = EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED
                await self.events.emit(
                    event_type,
                    {
                        "request_sequence": self.budget.steps,
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "input_summary": input_summary,
                        "output_summary": result.summary,
                        "status": "completed" if result.ok else "failed",
                        "error_code": error_code,
                        "truncated": result.truncated,
                    },
                    duration_ms=result.duration_ms,
                )
                messages.append(
                    Message(
                        role=MessageRole.TOOL,
                        name=call.name,
                        tool_call_id=call.id,
                        content=result.model_dump_json(),
                    )
                )
                if self.cancellation_token.is_cancelled:
                    return await self._cancelled_result(run_id)

                if error_code in {"INVALID_TOOL_CALL", "UNKNOWN_TOOL"}:
                    fingerprint = self._invalid_fingerprint(call, error_code)
                    if fingerprint == invalid_fingerprint:
                        repeated_invalid_calls += 1
                    else:
                        invalid_fingerprint = fingerprint
                        repeated_invalid_calls = 1
                    if repeated_invalid_calls >= 3:
                        return await self._failure_result(
                            run_id,
                            result_code="INVALID_TOOL_LOOP",
                            error_code="INVALID_TOOL_LOOP",
                        )
                else:
                    invalid_fingerprint = None
                    repeated_invalid_calls = 0

                if execution.finish_request is not None:
                    metrics = AgentMetrics.from_snapshot(self.budget.snapshot())
                    if self.quality_gate is not None:
                        gate_attempts += 1
                        outcome = await self.quality_gate.evaluate(
                            metrics.to_scorecard_metrics(),
                            attempt=gate_attempts,
                            remaining_wall_time_seconds=(self.budget.remaining_wall_time_seconds),
                        )
                        gate_metrics = AgentMetrics.from_scorecard_metrics(
                            outcome.scorecard.metrics
                        )
                        post_gate_budget = self.budget.check_non_step_limits()
                        if post_gate_budget is not None:
                            return await self._budget_result(run_id, post_gate_budget)
                        if outcome.passed:
                            await self.events.emit(
                                EventType.RUN_COMPLETED,
                                {
                                    "status": AgentLoopStatus.PASSED.value,
                                    "result_code": QualityResult.PASSED.value,
                                    "steps": metrics.steps,
                                    "tool_calls": metrics.tool_calls,
                                    "gate_attempts": gate_attempts,
                                },
                            )
                            await self.quality_gate.finalize_run(
                                RunStatus.PASSED,
                                outcome.scorecard,
                            )
                            await self.quality_gate.finalize_event_log()
                            return AgentLoopResult(
                                run_id=run_id,
                                status=AgentLoopStatus.PASSED,
                                result_code=QualityResult.PASSED.value,
                                finish_request=execution.finish_request,
                                scorecard=outcome.scorecard,
                                metrics=gate_metrics,
                            )
                        if (
                            outcome.recoverable
                            and outcome.feedback is not None
                            and gate_feedbacks < 2
                        ):
                            gate_feedbacks += 1
                            messages.append(
                                Message(
                                    role=MessageRole.USER,
                                    content=json.dumps(
                                        outcome.feedback,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                )
                            )
                            invalid_fingerprint = None
                            repeated_invalid_calls = 0
                            gate_feedback_received = True
                            break
                        status = (
                            AgentLoopStatus.CANCELLED
                            if outcome.result == QualityResult.CANCELLED
                            else AgentLoopStatus.TIMEOUT
                            if outcome.result == QualityResult.TIMEOUT
                            else AgentLoopStatus.BUDGET_EXCEEDED
                            if outcome.result == QualityResult.BUDGET_EXCEEDED
                            else AgentLoopStatus.FAILED
                        )
                        await self.events.emit(
                            EventType.RUN_CANCELLED
                            if status == AgentLoopStatus.CANCELLED
                            else EventType.RUN_FAILED,
                            {
                                "status": status.value,
                                "result_code": outcome.result.value,
                                "error_code": None,
                                "steps": metrics.steps,
                                "gate_attempts": gate_attempts,
                            },
                        )
                        run_status = (
                            RunStatus.CANCELLED
                            if status == AgentLoopStatus.CANCELLED
                            else RunStatus.TIMEOUT
                            if status == AgentLoopStatus.TIMEOUT
                            else RunStatus.BUDGET_EXCEEDED
                            if status == AgentLoopStatus.BUDGET_EXCEEDED
                            else RunStatus.FAILED
                        )
                        await self.quality_gate.finalize_run(
                            run_status,
                            outcome.scorecard,
                        )
                        await self.quality_gate.finalize_event_log()
                        return AgentLoopResult(
                            run_id=run_id,
                            status=status,
                            result_code=outcome.result.value,
                            finish_request=execution.finish_request,
                            scorecard=outcome.scorecard,
                            metrics=gate_metrics,
                        )
                    await self.events.emit(
                        EventType.RUN_COMPLETED,
                        {
                            "status": AgentLoopStatus.FINISH_REQUESTED.value,
                            "result_code": "FINISH_REQUESTED",
                            "steps": metrics.steps,
                            "tool_calls": metrics.tool_calls,
                        },
                    )
                    return AgentLoopResult(
                        run_id=run_id,
                        status=AgentLoopStatus.FINISH_REQUESTED,
                        result_code="FINISH_REQUESTED",
                        finish_request=execution.finish_request,
                        metrics=metrics,
                    )

                wall_stop = self.budget.check_non_step_limits()
                if wall_stop is not None:
                    return await self._budget_result(run_id, wall_stop)
            if gate_feedback_received:
                continue

    async def _request_model(self, messages: list[Message]) -> tuple[ModelResponse, int]:
        max_attempts = self.model_config.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            wall_stop = self.budget.check_non_step_limits()
            if wall_stop is not None:
                raise _BudgetReached(wall_stop)
            self.budget.record_model_call()
            await self.events.emit(
                EventType.MODEL_REQUESTED,
                {
                    "request_sequence": self.budget.steps,
                    "attempt": attempt,
                    "model": self.model_config.model,
                    "message_count": len(messages),
                    "tool_schema_count": len(self.registry.schemas),
                },
            )
            started = time.monotonic()
            try:
                timeout = min(
                    self.model_config.request_timeout_seconds,
                    self.budget.remaining_wall_time_seconds,
                )
                if timeout <= 0:
                    raise _BudgetReached(BudgetStopReason.MAX_WALL_TIME_SECONDS)
                model_task = asyncio.create_task(
                    self.model_client.complete(
                        list(messages),
                        self.registry.schemas,
                        self.model_config,
                    )
                )
                cancellation_task = asyncio.create_task(self.cancellation_token.wait())
                done, _ = await asyncio.wait(
                    {model_task, cancellation_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_task in done:
                    model_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await model_task
                    raise _RunCancelled
                cancellation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancellation_task
                if model_task not in done:
                    model_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await model_task
                    raise TimeoutError
                return model_task.result(), attempt
            except TimeoutError as exc:
                error: ModelClientError = ModelTimeoutError("model request timed out")
                error.__cause__ = exc
            except ModelClientError as exc:
                error = exc

            duration_ms = max(0, round((time.monotonic() - started) * 1_000))
            await self.events.emit(
                EventType.MODEL_RESPONDED,
                {
                    "request_sequence": self.budget.steps,
                    "attempt": attempt,
                    "model": self.model_config.model,
                    "status": "failed",
                    "error_code": error.code,
                    "finish_reason": None,
                    "provider_request_id": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "usage_estimated": False,
                    "estimated_cost_usd": "0",
                },
                duration_ms=duration_ms,
            )
            if not error.retryable or attempt >= max_attempts:
                raise error
            base_delay = min(
                self.model_config.retry_base_seconds * (2 ** (attempt - 1)),
                self.model_config.retry_max_seconds,
            )
            delay = base_delay * self.random.uniform(0.8, 1.2)
            if delay >= self.budget.remaining_wall_time_seconds:
                raise _BudgetReached(BudgetStopReason.MAX_WALL_TIME_SECONDS)
            await self.events.emit(
                EventType.MODEL_RETRYING,
                {
                    "request_sequence": self.budget.steps,
                    "attempt": attempt,
                    "error_code": error.code,
                    "delay_seconds": delay,
                },
            )
            await self.sleep(delay)
        raise RuntimeError("model retry loop exhausted without a result")

    async def _budget_result(
        self,
        run_id: UUID,
        reason: BudgetStopReason,
    ) -> AgentLoopResult:
        metrics = AgentMetrics.from_snapshot(self.budget.snapshot())
        scorecard = None
        if self.quality_gate is not None:
            scorecard = await self.quality_gate.finalize_terminal(
                QualityResult.BUDGET_EXCEEDED,
                metrics.to_scorecard_metrics(),
                summary=f"hard budget exhausted: {reason.value}",
            )
        await self.events.emit(
            EventType.RUN_FAILED,
            {
                "status": AgentLoopStatus.BUDGET_EXCEEDED.value,
                "result_code": "BUDGET_EXCEEDED",
                "budget_reason": reason.value,
                "steps": metrics.steps,
            },
        )
        if self.quality_gate is not None:
            if scorecard is None:
                raise RuntimeError("Quality Gate did not create a budget Scorecard")
            await self.quality_gate.finalize_run(
                RunStatus.BUDGET_EXCEEDED,
                scorecard,
                error_code=reason.value,
            )
            await self.quality_gate.finalize_event_log()
        return AgentLoopResult(
            run_id=run_id,
            status=AgentLoopStatus.BUDGET_EXCEEDED,
            result_code="BUDGET_EXCEEDED",
            error_code=reason.value,
            scorecard=scorecard,
            metrics=metrics,
        )

    async def _failure_result(
        self,
        run_id: UUID,
        *,
        result_code: str,
        error_code: str,
    ) -> AgentLoopResult:
        metrics = AgentMetrics.from_snapshot(self.budget.snapshot())
        try:
            quality_result = QualityResult(result_code)
        except ValueError:
            quality_result = QualityResult.SYSTEM_ERROR
        scorecard = None
        if self.quality_gate is not None:
            scorecard = await self.quality_gate.finalize_terminal(
                quality_result,
                metrics.to_scorecard_metrics(),
                summary=f"Agent Loop terminated with {result_code}: {error_code}",
            )
        await self.events.emit(
            EventType.RUN_FAILED,
            {
                "status": AgentLoopStatus.FAILED.value,
                "result_code": result_code,
                "error_code": error_code,
                "steps": metrics.steps,
            },
        )
        if self.quality_gate is not None:
            if scorecard is None:
                raise RuntimeError("Quality Gate did not create a failure Scorecard")
            await self.quality_gate.finalize_run(
                RunStatus.FAILED,
                scorecard,
                error_code=error_code,
            )
            await self.quality_gate.finalize_event_log()
        return AgentLoopResult(
            run_id=run_id,
            status=AgentLoopStatus.FAILED,
            result_code=result_code,
            error_code=error_code,
            scorecard=scorecard,
            metrics=metrics,
        )

    async def _cancelled_result(self, run_id: UUID) -> AgentLoopResult:
        metrics = AgentMetrics.from_snapshot(self.budget.snapshot())
        scorecard = None
        if self.quality_gate is not None:
            scorecard = await self.quality_gate.finalize_terminal(
                QualityResult.CANCELLED,
                metrics.to_scorecard_metrics(),
                summary="Run cancelled cooperatively",
            )
        await self.events.emit(
            EventType.RUN_CANCELLED,
            {
                "status": AgentLoopStatus.CANCELLED.value,
                "result_code": QualityResult.CANCELLED.value,
                "steps": metrics.steps,
            },
        )
        if self.quality_gate is not None:
            if scorecard is None:
                raise RuntimeError("Quality Gate did not create a cancellation Scorecard")
            await self.quality_gate.finalize_run(RunStatus.CANCELLED, scorecard)
            await self.quality_gate.finalize_event_log()
        return AgentLoopResult(
            run_id=run_id,
            status=AgentLoopStatus.CANCELLED,
            result_code=QualityResult.CANCELLED.value,
            scorecard=scorecard,
            metrics=metrics,
        )

    @staticmethod
    def _tool_input_summary(call: ToolCall) -> str:
        text = call.arguments_text()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        keys = (
            sorted(call.arguments)
            if isinstance(call.arguments, dict)
            else ["unparsed_json_arguments"]
        )
        return f"sha256={digest}; chars={len(text)}; keys={','.join(keys)}"

    @staticmethod
    def _invalid_fingerprint(call: ToolCall, error_code: str) -> str:
        material = f"{call.name}\0{call.arguments_text()}\0{error_code}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
