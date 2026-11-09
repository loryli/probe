"""Validated configuration; each application owns its settings instance."""

from pathlib import Path
from typing import Literal

from pydantic import Field, PositiveFloat, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INTERVIEW_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = Field(default=None, repr=False)
    data_dir: Path = Path("data")
    database_probe_timeout_seconds: PositiveFloat = 3.0
    qwen_api_key: SecretStr | None = Field(default=None, repr=False)
    qwen_model: str | None = None
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_timeout_seconds: float = Field(default=120, ge=10, le=300)
    qwen_max_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    qwen_enable_thinking: bool | None = False
    # 只用于面试的判定与提问；资料抽取仍用供应商默认值，避免同一份资料抽出不同结果。
    qwen_interview_temperature: float = Field(default=0.8, ge=0.0, le=1.5)
    ocr_model_dir: Path | None = None

    @field_validator("qwen_base_url")
    @classmethod
    def validate_model_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("qwen_base_url 必须是无凭据、查询参数和片段的 HTTP(S) 接口基础地址")
        return value.rstrip("/")

    @field_validator("qwen_model")
    @classmethod
    def validate_model_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 150):
            raise ValueError("qwen_model 需填写有效模型标识，最多 150 字符")
        return value.strip() if value else None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("database_url must be a valid SQLAlchemy URL") from None
        if url.drivername not in {"mysql+aiomysql", "sqlite+aiosqlite"} or not url.database:
            raise ValueError(
                "database_url must use mysql+aiomysql or sqlite+aiosqlite and name a database"
            )
        if url.drivername == "sqlite+aiosqlite" and url.database == ":memory:":
            raise ValueError("SQLite development mode requires a persistent database file")
        return value

    @property
    def resolved_database_url(self) -> str | None:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if self.environment == "local":
            return URL.create(
                "sqlite+aiosqlite",
                database=(self.data_dir / "interview.sqlite3").resolve().as_posix(),
            ).render_as_string(hide_password=False)
        return None
