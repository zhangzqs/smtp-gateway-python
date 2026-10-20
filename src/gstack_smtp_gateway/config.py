"""独立服务的环境变量配置。"""

from pathlib import Path
from typing import Self

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class GatewayConfig(BaseSettings):
    """通过 GSTACK_SMTP_GATEWAY_ 环境变量配置服务。"""

    model_config = SettingsConfigDict(
        env_prefix="GSTACK_SMTP_GATEWAY_", extra="forbid", frozen=True
    )

    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///smtp-gateway.db")
    encryption_key: SecretStr
    api_username: str = Field(min_length=1)
    api_password: SecretStr
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=18081, ge=0, le=65535)
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=1025, ge=0, le=65535)
    smtp_username: str = "grafana"
    smtp_password: SecretStr
    smtp_tls_cert: Path | None = None
    smtp_tls_key: Path | None = None
    smtp_allow_insecure_auth: bool = False
    max_message_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_recipients: int = Field(default=100, ge=1, le=1000)
    max_attempts: int = Field(default=5, ge=1, le=100)
    retry_delay: float = Field(default=30, ge=1)
    max_retry_delay: float = Field(default=3600, ge=1)
    delivery_timeout: float = Field(default=60, gt=0)
    lease_seconds: float = Field(default=120, gt=0)
    poll_interval: float = Field(default=1, gt=0)
    worker_concurrency: int = Field(default=4, ge=1, le=32)
    retention_seconds: float = Field(default=7 * 24 * 3600, ge=0)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        try:
            url = make_url(self.database_url.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("Database URL is invalid") from exc
        if url.drivername not in {"sqlite+aiosqlite", "postgresql+asyncpg"}:
            raise ValueError(
                "Database URL must use sqlite+aiosqlite or postgresql+asyncpg"
            )
        if not self.api_password.get_secret_value():
            raise ValueError("API password must not be empty")
        try:
            Fernet(self.encryption_key.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Encryption key must be a Fernet key") from exc
        if self.lease_seconds <= self.delivery_timeout:
            raise ValueError("Lease duration must exceed delivery timeout")
        if bool(self.smtp_tls_cert) != bool(self.smtp_tls_key):
            raise ValueError("SMTP TLS certificate and key must be configured together")
        if not self.smtp_username or not self.smtp_password.get_secret_value():
            raise ValueError("SMTP username and password are required")
        if self.smtp_tls_cert is None and not self.smtp_allow_insecure_auth:
            raise ValueError("SMTP requires TLS or explicit insecure AUTH opt-in")
        return self


__all__ = ["GatewayConfig"]
