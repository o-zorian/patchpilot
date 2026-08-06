from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class SettingsError(ValueError):
    """Raised when runtime settings are incompatible with the current milestone."""


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/patchpilot.db"
    artifact_root: Path = Path("./artifacts")
    workspace_root: Path = Path("./workspaces")
    log_level: str = "INFO"
    tool_output_max_chars: int = Field(default=20_000, gt=0)
    tool_list_max_files: int = Field(default=1_000, gt=0)
    tool_search_max_results: int = Field(default=100, gt=0)
    tool_read_max_lines: int = Field(default=400, gt=0, le=400)
    tool_max_file_bytes: int = Field(default=1_048_576, gt=0)

    hard_max_steps: int = Field(default=30, gt=0)
    hard_max_input_tokens: int = Field(default=250_000, gt=0)
    hard_max_output_tokens: int = Field(default=64_000, gt=0)
    hard_max_cost_usd: Decimal = Field(default=Decimal("1.00"), gt=0)
    hard_max_wall_time_seconds: int = Field(default=1_800, gt=0)
    hard_max_changed_files: int = Field(default=50, gt=0)
    hard_max_patch_lines: int = Field(default=2_000, gt=0)
    hard_max_command_timeout_seconds: int = Field(default=1_800, gt=0)
    hard_max_cpu_limit: int = Field(default=8, gt=0)
    hard_max_memory_limit_mb: int = Field(default=8_192, gt=0)

    def sqlite_database_path(self) -> Path:
        url = make_url(self.database_url)
        if url.get_backend_name() != "sqlite":
            raise SettingsError("the current CLI supports only SQLite DATABASE_URL values")
        if not url.database or url.database == ":memory:":
            raise SettingsError("runtime commands require a file-backed SQLite database")
        return Path(url.database).expanduser().resolve()

    def ensure_runtime_directories(self) -> None:
        self.sqlite_database_path().parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)
        self.workspace_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)
