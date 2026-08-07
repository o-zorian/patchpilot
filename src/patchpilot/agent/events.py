from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.persistence.database import Database
from patchpilot.persistence.models import EventRow, ModelCallRow, ToolCallRow


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    WORKSPACE_READY = "workspace.ready"
    MODEL_REQUESTED = "model.requested"
    MODEL_RESPONDED = "model.responded"
    MODEL_RETRYING = "model.retrying"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(gt=0)
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    async def write(self, event: RunEvent) -> None: ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def write(self, event: RunEvent) -> None:
        self.events.append(event)


class JsonlEventSink:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def write(self, event: RunEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as event_file:
            event_file.write(line)
            event_file.flush()
            os.fsync(event_file.fileno())


class DatabaseEventSink:
    """Persist the same event plus normalized model/tool call rows."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def write(self, event: RunEvent) -> None:
        payload = event.payload
        async with self.database.session() as session:
            session.add(
                EventRow(
                    id=str(event.event_id),
                    run_id=str(event.run_id),
                    sequence=event.sequence,
                    event_type=event.type.value,
                    payload=payload,
                    duration_ms=event.duration_ms,
                    created_at=event.timestamp,
                )
            )
            if event.type == EventType.MODEL_RESPONDED:
                session.add(
                    ModelCallRow(
                        id=str(uuid4()),
                        run_id=str(event.run_id),
                        request_sequence=int(payload["request_sequence"]),
                        attempt=int(payload["attempt"]),
                        model=str(payload["model"]),
                        prompt_tokens=int(payload.get("prompt_tokens", 0)),
                        completion_tokens=int(payload.get("completion_tokens", 0)),
                        total_tokens=int(payload.get("total_tokens", 0)),
                        usage_estimated=bool(payload.get("usage_estimated", False)),
                        estimated_cost_usd=Decimal(str(payload.get("estimated_cost_usd", "0"))),
                        duration_ms=event.duration_ms or 0,
                        finish_reason=(
                            str(payload["finish_reason"])
                            if payload.get("finish_reason") is not None
                            else None
                        ),
                        provider_request_id=(
                            str(payload["provider_request_id"])
                            if payload.get("provider_request_id") is not None
                            else None
                        ),
                        status=str(payload["status"]),
                        error_code=(
                            str(payload["error_code"])
                            if payload.get("error_code") is not None
                            else None
                        ),
                        created_at=event.timestamp,
                    )
                )
            if event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}:
                session.add(
                    ToolCallRow(
                        id=str(uuid4()),
                        run_id=str(event.run_id),
                        request_sequence=int(payload["request_sequence"]),
                        tool_call_id=str(payload["tool_call_id"]),
                        tool_name=str(payload["tool"]),
                        input_summary=str(payload["input_summary"])[:1_000],
                        output_summary=str(payload["output_summary"])[:2_000],
                        status=str(payload["status"]),
                        error_code=(
                            str(payload["error_code"])
                            if payload.get("error_code") is not None
                            else None
                        ),
                        duration_ms=event.duration_ms or 0,
                        created_at=event.timestamp,
                    )
                )
            await session.commit()


class EventEmitter:
    def __init__(self, run_id: UUID, sinks: Sequence[EventSink]) -> None:
        if not sinks:
            raise ValueError("at least one event sink is required")
        self.run_id = run_id
        self._sinks = tuple(sinks)
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def emit(
        self,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        *,
        duration_ms: int | None = None,
    ) -> RunEvent:
        async with self._lock:
            self._sequence += 1
            event = RunEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                type=event_type,
                duration_ms=duration_ms,
                payload=payload or {},
            )
            for sink in self._sinks:
                await sink.write(event)
            return event
