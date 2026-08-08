from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from patchpilot.api.app import create_app
from patchpilot.config import AppSettings
from patchpilot.domain.run import RunStatus
from patchpilot.models.base import (
    Message,
    ModelConfig,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from patchpilot.models.fake import ScriptedModelClient
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.queue import InMemoryRunQueue
from patchpilot.services import RunExecutor
from patchpilot.worker.main import RunWorker
from tests.helpers import create_git_repository


def response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        tool_calls=list(calls),
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="scripted-service",
        provider_request_id=f"script-{uuid4()}",
        latency_ms=0,
    )


def task_payload(tmp_path: Path, valid_task_data: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    source = create_git_repository(
        tmp_path / "original",
        {
            "calculator.py": "def add(left: int, right: int) -> int:\n    return left - right\n",
            "tests/test_calculator.py": (
                "from calculator import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    payload = dict(valid_task_data)
    payload["repository"] = dict(valid_task_data["repository"])
    payload["repository"]["path"] = str(source)
    payload["allowed_paths"] = ["calculator.py", "tests/**"]
    payload["denied_paths"] = [".git/**", "README.md"]
    payload["acceptance"] = {
        "commands": [{"argv": ["python", "-m", "pytest"], "timeout_seconds": 30}],
        "required_tests": ["test_add"],
    }
    return payload, source


def scripted_client() -> ScriptedModelClient:
    patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
"""
    return ScriptedModelClient(
        [
            response(ToolCall(id="patch", name="apply_patch", arguments={"patch": patch})),
            response(
                ToolCall(
                    id="finish",
                    name="finish",
                    arguments={
                        "summary": "fixed through the service Worker",
                        "tests_run": [],
                        "remaining_risks": [],
                    },
                )
            ),
        ]
    )


def service_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'service.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
        worker_poll_seconds=0.01,
        worker_heartbeat_seconds=0.02,
        worker_cancel_poll_seconds=0.01,
    )


def model_config(_: Any) -> ModelConfig:
    return ModelConfig(model="scripted-service", max_retries=0, request_timeout_seconds=30)


@pytest.mark.asyncio
async def test_api_worker_idempotency_events_artifacts_and_workspace_isolation(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    settings = service_settings(tmp_path)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    queue = InMemoryRunQueue()
    app = create_app(settings=settings, database=database, queue=queue)
    payload, source = task_payload(tmp_path, valid_task_data)
    original = (source / "calculator.py").read_bytes()
    client = scripted_client()
    executor = RunExecutor(
        database,
        settings,
        model_client_factory=lambda _: client,
        model_config_factory=model_config,
    )
    worker = RunWorker(database, queue, executor, worker_id="test-worker", poll_seconds=0.01)

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api:
                task_response = await api.post("/api/v1/tasks", json=payload)
                assert task_response.status_code == 201
                task_id = task_response.json()["id"]

                headers = {"Idempotency-Key": "same-request"}
                run_body = {"task_id": task_id, "strategy": "full", "model": "scripted"}
                first = await api.post("/api/v1/runs", json=run_body, headers=headers)
                second = await api.post("/api/v1/runs", json=run_body, headers=headers)
                assert first.status_code == 202
                assert first.json()["created"] is True
                assert first.json()["run_id"] == first.json()["run"]["id"]
                assert second.json()["created"] is False
                assert second.json()["run"]["id"] == first.json()["run"]["id"]
                assert first.json()["run"]["status"] == RunStatus.PENDING.value
                run_id = UUID(first.json()["run"]["id"])

                assert await worker.run_once() is True
                client.assert_exhausted()
                completed = await api.get(f"/api/v1/runs/{run_id}")
                assert completed.json()["status"] == RunStatus.PASSED.value

                events = await api.get(f"/api/v1/runs/{run_id}/events")
                sequences = [event["sequence"] for event in events.json()]
                assert sequences == list(range(1, len(sequences) + 1))
                assert events.json()[-1]["type"] == "run.completed"

                stream = await api.get(f"/api/v1/runs/{run_id}/stream")
                assert stream.status_code == 200
                assert "event: run.completed" in stream.text

                patch_artifact = await api.get(f"/api/v1/runs/{run_id}/patch")
                scorecard = await api.get(f"/api/v1/runs/{run_id}/scorecard")
                report = await api.get(f"/api/v1/runs/{run_id}/report")
                assert patch_artifact.status_code == 200
                assert "return left + right" in patch_artifact.text
                assert scorecard.json()["result"] == "PASSED"
                assert report.status_code == 200
                assert "text/html" in report.headers["content-type"]

                forbidden = await api.get(
                    f"/api/v1/runs/{run_id}", headers={"X-Owner-ID": "another-owner"}
                )
                assert forbidden.status_code == 403
                assert forbidden.json()["error"]["code"] == "FORBIDDEN"

                metrics = await api.get("/metrics")
                assert 'patchpilot_runs{status="passed"} 1' in metrics.text
                summary = await api.get("/metrics/summary")
                assert summary.json()["success_rate"] == 1.0
                assert (await api.get("/health/live")).status_code == 200
                assert (await api.get("/health/ready")).status_code == 200

                pending = await api.post(
                    "/api/v1/runs",
                    json={"task_id": task_id, "strategy": "full", "model": "never-called"},
                )
                pending_id = pending.json()["run_id"]
                first_cancel = await api.post(f"/api/v1/runs/{pending_id}/cancel")
                second_cancel = await api.post(f"/api/v1/runs/{pending_id}/cancel")
                assert first_cancel.json()["status"] == RunStatus.CANCELLED.value
                assert second_cancel.json()["status"] == RunStatus.CANCELLED.value
                cancel_events = (await api.get(f"/api/v1/runs/{pending_id}/events")).json()
                assert [event["type"] for event in cancel_events] == ["run.cancelled"]
                assert await worker.run_once() is True
    finally:
        await queue.close()
        await database.close()

    assert (source / "calculator.py").read_bytes() == original
    assert not any((tmp_path / "workspaces").iterdir())


class BlockingFakeModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled fake model resumed unexpectedly")


@pytest.mark.asyncio
async def test_running_run_can_be_cancelled_cooperatively(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    settings = service_settings(tmp_path)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    queue = InMemoryRunQueue()
    app = create_app(settings=settings, database=database, queue=queue)
    payload, _ = task_payload(tmp_path, valid_task_data)
    blocking = BlockingFakeModel()
    executor = RunExecutor(
        database,
        settings,
        model_client_factory=lambda _: blocking,
        model_config_factory=model_config,
    )
    worker = RunWorker(database, queue, executor, worker_id="cancel-worker", poll_seconds=0.01)

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api:
                task_id = (await api.post("/api/v1/tasks", json=payload)).json()["id"]
                submitted = await api.post(
                    "/api/v1/runs",
                    json={"task_id": task_id, "strategy": "full", "model": "blocking-fake"},
                )
                run_id = UUID(submitted.json()["run"]["id"])

                work = asyncio.create_task(worker.run_once())
                await asyncio.wait_for(blocking.started.wait(), timeout=5)
                cancellation = await api.post(f"/api/v1/runs/{run_id}/cancel")
                assert cancellation.status_code == 202
                assert cancellation.json()["cancel_requested_at"] is not None
                await asyncio.wait_for(work, timeout=5)

                run = await api.get(f"/api/v1/runs/{run_id}")
                assert run.json()["status"] == RunStatus.CANCELLED.value
                events = (await api.get(f"/api/v1/runs/{run_id}/events")).json()
                assert events[-1]["type"] == "run.cancelled"
    finally:
        await queue.close()
        await database.close()
