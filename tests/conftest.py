from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from patchpilot.domain.task import TaskLimits


@pytest.fixture
def task_limits() -> TaskLimits:
    return TaskLimits(
        max_steps=30,
        max_input_tokens=250_000,
        max_output_tokens=64_000,
        max_cost_usd=Decimal("1.00"),
        max_wall_time_seconds=1_800,
        max_changed_files=50,
        max_patch_lines=2_000,
        max_command_timeout_seconds=1_800,
        max_cpu_limit=8,
        max_memory_limit_mb=8_192,
    )


@pytest.fixture
def valid_task_data(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    return {
        "version": "1",
        "id": "py-boundary-001",
        "title": "Fix a boundary condition",
        "repository": {
            "path": "repo",
            "base_ref": "main",
            "language": "python",
        },
        "goal": "Treat zero as the first page.",
        "allowed_paths": ["src/**", "tests/**"],
        "denied_paths": [".git/**", ".github/**"],
        "acceptance": {
            "commands": [
                {
                    "argv": ["python", "-m", "pytest", "tests/test_service.py::test_zero"],
                    "timeout_seconds": 120,
                }
            ],
            "required_tests": ["test_zero"],
        },
        "budget": {
            "max_steps": 15,
            "max_input_tokens": 80_000,
            "max_output_tokens": 16_000,
            "max_cost_usd": 0.2,
            "max_wall_time_seconds": 600,
            "max_changed_files": 5,
            "max_patch_lines": 300,
        },
        "execution": {"network": False, "cpu_limit": 2, "memory_limit_mb": 1_024},
        "metadata": {"difficulty": "easy", "tags": ["python", "boundary"]},
    }
