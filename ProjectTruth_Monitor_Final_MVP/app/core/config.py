from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ProjectTruth Monitor"
    environment: Literal["development", "test", "production"] = "development"
    secret_key: str = "local-development-change-me"  # noqa: S105 - rejected in production
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'projecttruth.db').as_posix()}"
    upload_dir: Path = ROOT_DIR / "data" / "uploads"
    report_dir: Path = ROOT_DIR / "data" / "reports"
    max_upload_mb: int = Field(default=15, ge=1, le=100)
    session_cookie_secure: bool = False
    session_cookie_name: str = "pt_session"
    session_max_age_seconds: int = Field(default=28_800, ge=300, le=604_800)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    enable_live_hrera: bool = False
    enable_scheduler: bool = False
    default_monitoring_cadence: Literal["manual", "daily", "weekly"] = "weekly"
    ocr_provider: str = "disabled"
    llm_provider: str = "disabled"
    llm_api_key: str | None = None
    source_request_timeout: float = Field(default=15.0, ge=1.0, le=60.0)
    source_max_response_mb: int = Field(default=8, ge=1, le=50)
    hrera_rate_limit_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    retention_upload_days: int = Field(default=90, ge=1)
    retention_report_days: int = Field(default=90, ge=1)
    login_rate_limit: int = Field(default=10, ge=1)
    refresh_rate_limit: int = Field(default=20, ge=1)
    upload_rate_limit: int = Field(default=10, ge=1)

    @field_validator("upload_dir", "report_dir", mode="before")
    @classmethod
    def resolve_storage_path(cls, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def allowed_host_list(self) -> list[str]:
        return [part.strip() for part in self.allowed_hosts.split(",") if part.strip()]

    def ensure_runtime_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def validate_for_startup(self) -> None:
        if (
            self.environment == "production"
            and self.secret_key == "local-development-change-me"  # noqa: S105
        ):
            raise RuntimeError("SECRET_KEY must be changed in production")
        if self.environment == "production" and len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
