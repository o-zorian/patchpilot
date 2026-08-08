from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from patchpilot.persistence.database import Database
from patchpilot.persistence.models import ArtifactRow


class ArtifactKind(StrEnum):
    PATCH = "patch"
    TEST_LOG = "test_log"
    SCORECARD = "scorecard"
    EVENT_LOG = "event_log"
    REPORT_MARKDOWN = "report_markdown"
    REPORT_HTML = "report_html"


_FILENAMES = {
    ArtifactKind.PATCH: "final.patch",
    ArtifactKind.TEST_LOG: "test.log",
    ArtifactKind.SCORECARD: "scorecard.json",
    ArtifactKind.EVENT_LOG: "events.jsonl",
    ArtifactKind.REPORT_MARKDOWN: "report.md",
    ArtifactKind.REPORT_HTML: "report.html",
}


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    kind: ArtifactKind
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactStore:
    """Run-scoped atomic artifact storage with optional database indexing."""

    def __init__(
        self,
        root: Path,
        run_id: UUID,
        *,
        database: Database | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.run_id = run_id
        self.run_root = (self.root / str(run_id)).resolve()
        if self.run_root.parent != self.root:
            raise ValueError("artifact Run path escaped ARTIFACT_ROOT")
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.database = database

    def path_for(self, kind: ArtifactKind) -> Path:
        path = (self.run_root / _FILENAMES[kind]).resolve()
        if path.parent != self.run_root:
            raise ValueError("artifact path escaped the Run artifact directory")
        return path

    def relative_path_for(self, kind: ArtifactKind) -> str:
        return self.path_for(kind).relative_to(self.root).as_posix()

    async def write_text(self, kind: ArtifactKind, content: str) -> ArtifactRecord:
        path = self.path_for(kind)
        await asyncio.to_thread(self._atomic_write, path, content.encode("utf-8"))
        return await self.record_existing(kind)

    async def record_existing(self, kind: ArtifactKind) -> ArtifactRecord:
        path = self.path_for(kind)
        data = await asyncio.to_thread(path.read_bytes)
        record = ArtifactRecord(
            run_id=self.run_id,
            kind=kind,
            path=self.relative_path_for(kind),
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        if self.database is not None:
            await self._upsert(record)
        return record

    async def _upsert(self, record: ArtifactRecord) -> None:
        if self.database is None:
            return
        async with self.database.session() as session:
            row = await session.scalar(
                select(ArtifactRow).where(
                    ArtifactRow.run_id == str(record.run_id),
                    ArtifactRow.kind == record.kind.value,
                )
            )
            if row is None:
                session.add(
                    ArtifactRow(
                        id=str(record.id),
                        run_id=str(record.run_id),
                        kind=record.kind.value,
                        path=record.path,
                        sha256=record.sha256,
                        size_bytes=record.size_bytes,
                        created_at=record.created_at,
                    )
                )
            else:
                row.path = record.path
                row.sha256 = record.sha256
                row.size_bytes = record.size_bytes
                row.created_at = record.created_at
            await session.commit()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as artifact_file:
                artifact_file.write(data)
                artifact_file.flush()
                os.fsync(artifact_file.fileno())
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
