from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(database_url: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    migration_url = database_url.replace("sqlite+aiosqlite:", "sqlite:", 1)
    migration_url = migration_url.replace("postgresql+asyncpg:", "postgresql+psycopg:", 1)
    config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
    command.upgrade(config, "head")
