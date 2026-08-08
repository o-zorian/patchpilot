from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from patchpilot.models.base import ModelConfig


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
    postgres_database_url: str = (
        "postgresql+asyncpg://patchpilot:patchpilot@localhost:5432/patchpilot"
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_name: str = "patchpilot:runs"
    service_owner_id: str = Field(default="local", min_length=1, max_length=128)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8_000, gt=0, le=65_535)
    worker_poll_seconds: float = Field(default=1, gt=0)
    worker_heartbeat_seconds: float = Field(default=5, gt=0)
    worker_cancel_poll_seconds: float = Field(default=0.1, gt=0)
    artifact_root: Path = Path("./artifacts")
    workspace_root: Path = Path("./workspaces")
    log_level: str = "INFO"
    tool_output_max_chars: int = Field(default=20_000, gt=0)
    tool_list_max_files: int = Field(default=1_000, gt=0)
    tool_search_max_results: int = Field(default=100, gt=0)
    tool_read_max_lines: int = Field(default=400, gt=0, le=400)
    tool_max_file_bytes: int = Field(default=1_048_576, gt=0)

    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: SecretStr | None = None
    model_name: str | None = None
    model_temperature: float = Field(default=0, ge=0, le=2)
    model_max_tokens: int = Field(default=4_096, gt=0)
    model_request_timeout_seconds: float = Field(default=60, gt=0)
    model_max_retries: int = Field(default=3, ge=0, le=3)
    model_input_cost_per_million_usd: Decimal = Field(default=Decimal("0"), ge=0)
    model_output_cost_per_million_usd: Decimal = Field(default=Decimal("0"), ge=0)

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

    def database_backend(self) -> str:
        backend = make_url(self.database_url).get_backend_name()
        if backend not in {"sqlite", "postgresql"}:
            raise SettingsError("DATABASE_URL must use SQLite or PostgreSQL")
        return backend

    def ensure_runtime_directories(self) -> None:
        if self.database_backend() == "sqlite":
            self.sqlite_database_path().parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)
        self.workspace_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)

    def real_model_config(self) -> ModelConfig:
        if not self.model_name:
            raise SettingsError("MODEL_NAME is required for the real model client")
        if self.model_api_key is None or not self.model_api_key.get_secret_value():
            raise SettingsError("MODEL_API_KEY is required for the real model client")
        return ModelConfig(
            base_url=self.model_base_url,
            api_key=self.model_api_key,
            model=self.model_name,
            temperature=self.model_temperature,
            max_tokens=self.model_max_tokens,
            request_timeout_seconds=self.model_request_timeout_seconds,
            max_retries=self.model_max_retries,
            input_cost_per_million_usd=self.model_input_cost_per_million_usd,
            output_cost_per_million_usd=self.model_output_cost_per_million_usd,
        )
