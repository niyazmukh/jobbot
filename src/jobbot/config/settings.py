"""Application settings and local storage conventions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Local-first application settings."""

    model_config = SettingsConfigDict(
        env_prefix="JOBBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "jobbot"
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".jobbot")
    database_url: str | None = None
    artifact_retention_days: int = 180
    trace_retention_days: int = 45
    auto_submit_threshold: float = 0.95
    extractor_threshold: float = 0.90
    field_mapping_threshold: float = 0.92
    answer_threshold: float = 0.88
    model_call_daily_budget_usd: float = 5.0
    model_call_weekly_budget_usd: float = 25.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobbot.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def browser_profiles_dir(self) -> Path:
        return self.data_dir / "browser-profiles"

    @property
    def prompts_dir(self) -> Path:
        return self.data_dir / "prompts"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.artifacts_dir,
            self.browser_profiles_dir,
            self.prompts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    settings = Settings()
    settings.ensure_dirs()
    return settings
