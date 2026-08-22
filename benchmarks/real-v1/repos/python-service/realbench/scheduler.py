from __future__ import annotations

from realbench.models import Job


def order_jobs(jobs: list[Job]) -> list[Job]:
    """Higher priority first; ties preserve earlier submission order."""

    return sorted(jobs, key=lambda job: (-job.priority, -job.submitted_at))
