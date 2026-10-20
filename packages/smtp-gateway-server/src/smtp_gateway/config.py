"""从 YAML 和环境变量加载按职责划分的服务配置。"""

import logging
from pathlib import Path
from typing import Self, override

import yaml
from cryptography.fernet import Fernet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationFileError(ValueError):
    """配置文件无法加载，消息中不包含原始文件内容。"""


class _Section(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class AuthConfig(_Section):
    username: str = Field(min_length=1)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("Authentication password must not be empty")
        return value


class HttpConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=18081, ge=0, le=65535)
    auth: AuthConfig


class SmtpAuthConfig(AuthConfig):
    username: str = Field(default="grafana", min_length=1)


class SmtpTlsConfig(_Section):
    cert_file: Path | None = None
    key_file: Path | None = None

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("SMTP TLS certificate and key must be configured together")
        return self


class SmtpConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=1025, ge=0, le=65535)
    auth: SmtpAuthConfig
    tls: SmtpTlsConfig = Field(default_factory=SmtpTlsConfig)
    allow_insecure_auth: bool = False

    @model_validator(mode="after")
    def validate_transport_security(self) -> Self:
        if self.tls.cert_file is None and not self.allow_insecure_auth:
            raise ValueError("SMTP requires TLS or explicit insecure AUTH opt-in")
        return self


class DatabaseConfig(_Section):
    url: SecretStr = SecretStr("sqlite+aiosqlite:///smtp-gateway.db")
    encryption_key: SecretStr

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("Database URL is invalid") from exc
        if url.drivername not in {"sqlite+aiosqlite", "postgresql+asyncpg"}:
            raise ValueError(
                "Database URL must use sqlite+aiosqlite or postgresql+asyncpg"
            )
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Encryption key must be a Fernet key") from exc
        return value


class LimitsConfig(_Section):
    max_message_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_recipients: int = Field(default=100, ge=1, le=1000)


class DeliveryConfig(_Section):
    max_attempts: int = Field(default=5, ge=1, le=100)
    retry_delay: float = Field(default=30, ge=1)
    max_retry_delay: float = Field(default=3600, ge=1)
    timeout: float = Field(default=60, gt=0)
    lease_seconds: float = Field(default=120, gt=0)
    poll_interval: float = Field(default=1, gt=0)
    concurrency: int = Field(default=4, ge=1, le=32)
    retention_seconds: float = Field(default=7 * 24 * 3600, ge=0)

    @model_validator(mode="after")
    def validate_lease(self) -> Self:
        if self.lease_seconds <= self.timeout:
            raise ValueError("Lease duration must exceed delivery timeout")
        return self


class GatewayConfig(BaseSettings):
    """环境变量通过 SMTP_GATEWAY_ 前缀和双下划线定位嵌套字段。"""

    model_config = SettingsConfigDict(
        env_prefix="SMTP_GATEWAY_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    http: HttpConfig
    smtp: SmtpConfig
    database: DatabaseConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """先递归合并环境变量和 YAML 字典，再进行完整配置校验。"""
        del settings_cls, dotenv_settings, file_secret_settings
        # source 调试日志发生在 SecretStr 校验前，可能包含原始凭据。
        logging.getLogger("pydantic_settings").setLevel(logging.WARNING)
        return env_settings, init_settings

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        """显式读取 YAML；文件内路径保持相对进程工作目录。"""
        if path is None:
            return cls()
        try:
            values = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ConfigurationFileError("Unable to read configuration file") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationFileError(
                "Configuration file contains invalid YAML"
            ) from exc
        if values is None:
            values = {}
        if not isinstance(values, dict) or any(
            not isinstance(key, str) for key in values
        ):
            raise ConfigurationFileError(
                "Configuration file must contain a mapping with string keys"
            )
        return cls(**values)


__all__ = ["ConfigurationFileError", "GatewayConfig"]
