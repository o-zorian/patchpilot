from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import func, select, text

from patchpilot.api.schemas import CreateRunRequest
from patchpilot.artifacts import ArtifactKind
from patchpilot.config import AppSettings
from patchpilot.domain.run import RunStatus
from patchpilot.domain.task import TaskSpec
from patchpilot.logging import configure_logging
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.models import RunRow
from patchpilot.persistence.repositories import RunNotFoundError, TaskNotFoundError
from patchpilot.queue import RedisRunQueue, RunQueue
from patchpilot.services import RunService


def _error(request: Request, code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={
            "error": {"code": code, "message": message},
            "request_id": request.state.request_id,
        },
    )


def _service(request: Request) -> RunService:
    return cast(RunService, request.app.state.service)


def _owner(
    request: Request,
    x_owner_id: Annotated[str | None, Header(alias="X-Owner-ID")] = None,
) -> str:
    owner_id = (x_owner_id or request.app.state.settings.service_owner_id).strip()
    if not owner_id or len(owner_id) > 128:
        raise ValueError("X-Owner-ID must contain 1 to 128 characters")
    return owner_id


def _event_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "sequence": row.sequence,
        "type": row.event_type,
        "payload": row.payload,
        "duration_ms": row.duration_ms,
        "created_at": row.created_at,
    }


def create_app(
    *,
    settings: AppSettings | None = None,
    database: Database | None = None,
    queue: RunQueue | None = None,
) -> FastAPI:
    configured = settings or AppSettings()
    owns_database = database is None
    owns_queue = queue is None
    db = database or Database(configured.database_url)
    run_queue = queue or RedisRunQueue(configured.redis_url, configured.redis_queue_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured.ensure_runtime_directories()
        await asyncio.to_thread(upgrade_database, configured.database_url)
        app.state.settings = configured
        app.state.database = db
        app.state.queue = run_queue
        app.state.service = RunService(db, run_queue, configured)
        try:
            yield
        finally:
            if owns_queue:
                await run_queue.close()
            if owns_database:
                await db.close()

    app = FastAPI(title="PatchPilot API", version="1.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.database = db
    app.state.queue = run_queue
    app.state.service = RunService(db, run_queue, configured)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        supplied = request.headers.get("X-Request-ID", "").strip()
        request.state.request_id = supplied[:128] or uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(TaskNotFoundError)
    async def task_not_found(request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return _error(request, "TASK_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(RunNotFoundError)
    async def run_not_found(request: Request, exc: RunNotFoundError) -> JSONResponse:
        return _error(request, "RUN_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError) -> JSONResponse:
        return _error(request, "FORBIDDEN", str(exc), status.HTTP_403_FORBIDDEN)

    @app.exception_handler(FileNotFoundError)
    async def artifact_not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
        return _error(request, "ARTIFACT_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return _error(request, "VALIDATION_ERROR", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "request validation failed",
                    "details": jsonable_encoder(exc.errors()),
                },
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        return _error(
            request,
            "INTERNAL_ERROR",
            "an internal service error occurred",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @app.post("/api/v1/tasks", status_code=status.HTTP_201_CREATED)
    async def create_task(
        spec: TaskSpec,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> dict[str, Any]:
        task = await service.create_task(spec, owner_id=owner_id, base_directory=Path.cwd())
        return task.model_dump(mode="json")

    @app.get("/api/v1/tasks")
    async def list_tasks(
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[dict[str, Any]]:
        tasks = await service.list_tasks(owner_id=owner_id, offset=offset, limit=limit)
        return [task.model_dump(mode="json") for task in tasks]

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(
        task_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> dict[str, Any]:
        task = await service.get_task(task_id, owner_id=owner_id)
        return task.model_dump(mode="json")

    @app.post("/api/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        body: CreateRunRequest,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        run, created = await service.submit_run(
            body.task_id,
            owner_id=owner_id,
            strategy=body.strategy,
            model=body.model,
            idempotency_key=idempotency_key,
        )
        return {
            "run_id": str(run.id),
            "run": run.model_dump(mode="json"),
            "created": created,
        }

    @app.get("/api/v1/runs")
    async def list_runs(
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[dict[str, Any]]:
        runs = await service.list_runs(owner_id=owner_id, offset=offset, limit=limit)
        return [run.model_dump(mode="json") for run in runs]

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> dict[str, Any]:
        run = await service.get_run(run_id, owner_id=owner_id)
        return run.model_dump(mode="json")

    @app.post("/api/v1/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_run(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> dict[str, Any]:
        run = await service.cancel_run(run_id, owner_id=owner_id)
        return run.model_dump(mode="json")

    @app.get("/api/v1/runs/{run_id}/events")
    async def get_events(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> list[dict[str, Any]]:
        rows = await service.list_events(run_id, owner_id=owner_id, after=after, limit=limit)
        return [_event_payload(row) for row in rows]

    @app.get("/api/v1/runs/{run_id}/stream")
    @app.get("/api/v1/runs/{run_id}/events/stream", include_in_schema=False)
    async def stream_events(
        request: Request,
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        await service.get_run(run_id, owner_id=owner_id)

        async def event_stream() -> AsyncIterator[str]:
            cursor = after
            while True:
                if await request.is_disconnected():
                    return
                rows = await service.list_events(run_id, owner_id=owner_id, after=cursor, limit=100)
                for row in rows:
                    cursor = row.sequence
                    payload = json.dumps(jsonable_encoder(_event_payload(row)), ensure_ascii=False)
                    yield f"id: {row.sequence}\nevent: {row.event_type}\ndata: {payload}\n\n"
                run = await service.get_run(run_id, owner_id=owner_id)
                if run.status.is_terminal and not rows:
                    return
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    artifact_routes = {
        "patch": (ArtifactKind.PATCH, "text/x-diff"),
        "scorecard": (ArtifactKind.SCORECARD, "application/json"),
        "report": (ArtifactKind.REPORT_HTML, "text/html"),
    }

    async def artifact_response(
        run_id: UUID,
        kind: ArtifactKind,
        media_type: str,
        service: RunService,
        owner_id: str,
    ) -> FileResponse:
        row, path = await service.get_artifact(run_id, kind, owner_id=owner_id)
        return FileResponse(path, media_type=media_type, filename=Path(row.path).name)

    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_name}")
    async def get_artifact(
        run_id: UUID,
        artifact_name: str,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> FileResponse:
        route = artifact_routes.get(artifact_name)
        if route is None:
            raise FileNotFoundError(f"Unknown artifact: {artifact_name}")
        kind, media_type = route
        return await artifact_response(run_id, kind, media_type, service, owner_id)

    @app.get("/api/v1/runs/{run_id}/patch")
    async def get_patch(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> FileResponse:
        return await artifact_response(run_id, ArtifactKind.PATCH, "text/x-diff", service, owner_id)

    @app.get("/api/v1/runs/{run_id}/scorecard")
    async def get_scorecard(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> FileResponse:
        return await artifact_response(
            run_id, ArtifactKind.SCORECARD, "application/json", service, owner_id
        )

    @app.get("/api/v1/runs/{run_id}/report")
    async def get_report(
        run_id: UUID,
        service: Annotated[RunService, Depends(_service)],
        owner_id: Annotated[str, Depends(_owner)],
    ) -> FileResponse:
        return await artifact_response(
            run_id, ArtifactKind.REPORT_HTML, "text/html", service, owner_id
        )

    @app.get("/api/v1/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    async def ready(request: Request) -> JSONResponse:
        try:
            async with request.app.state.database.session() as session:
                await session.execute(text("SELECT 1"))
            queue_ready = await request.app.state.queue.ping()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        code = 200 if queue_ready else 503
        return JSONResponse(
            status_code=code, content={"status": "ok" if queue_ready else "not_ready"}
        )

    @app.get("/api/v1/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        async with request.app.state.database.session() as session:
            result = await session.execute(
                select(RunRow.status, func.count(RunRow.id)).group_by(RunRow.status)
            )
            counts = {str(run_status): int(count) for run_status, count in result.all()}
        lines = ["# TYPE patchpilot_runs gauge"]
        for run_status in RunStatus:
            lines.append(
                f'patchpilot_runs{{status="{run_status.value}"}} {counts.get(run_status.value, 0)}'
            )
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/metrics/summary")
    async def metrics_summary(request: Request) -> dict[str, int | float | str]:
        async with request.app.state.database.session() as session:
            rows = list((await session.scalars(select(RunRow))).all())
        terminal = [row for row in rows if RunStatus(row.status).is_terminal]
        passed = sum(row.status == RunStatus.PASSED.value for row in terminal)
        durations = [
            (row.finished_at - row.started_at).total_seconds()
            for row in terminal
            if row.started_at is not None and row.finished_at is not None
        ]
        return {
            "runs_total": len(rows),
            "runs_terminal": len(terminal),
            "runs_passed": passed,
            "success_rate": passed / len(terminal) if terminal else 0.0,
            "estimated_cost_usd": str(sum(row.estimated_cost_usd for row in rows)),
            "average_duration_seconds": sum(durations) / len(durations) if durations else 0.0,
        }

    return app


def main() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    service_settings = settings.model_copy(update={"database_url": settings.postgres_database_url})
    uvicorn.run(
        create_app(settings=service_settings), host=settings.api_host, port=settings.api_port
    )
