from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.domain.run import RunStrategy


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    strategy: RunStrategy = RunStrategy.FULL
    model: str = Field(min_length=1, max_length=255)
