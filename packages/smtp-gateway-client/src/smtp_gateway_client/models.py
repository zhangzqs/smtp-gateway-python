"""SMTP 网关配置接口的请求与安全响应。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class TlsMode(StrEnum):
    NONE = "none"
    STARTTLS = "starttls"
    IMPLICIT = "implicit"


class SmtpSettings(BaseModel):
    """过滤响应中的额外字段，保留调用方需要的设置状态。"""

    model_config = ConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
    )

    configured: bool
    read_only: bool = False
    host: str = ""
    port: int = Field(
        default=587,
        ge=1,
        le=65535,
    )
    username: str = ""
    tls: TlsMode = TlsMode.STARTTLS
    has_password: bool = False


class SmtpSettingsWrite(BaseModel):
    """保存完整设置；省略密码时由网关保留已保存的值。"""

    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
    )

    host: str = Field(
        min_length=1,
        max_length=253,
    )
    port: int = Field(
        ge=1,
        le=65535,
    )
    username: str = Field(
        default="",
        max_length=320,
    )
    password: SecretStr | None = None
    tls: TlsMode = TlsMode.STARTTLS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """主机名不允许嵌入认证信息、端口或路径。"""
        if value != value.strip() or any(char in value for char in "/@:# \t\r\n"):
            raise ValueError("SMTP host must be a hostname or IPv4 address")
        return value


__all__ = ["SmtpSettings", "SmtpSettingsWrite", "TlsMode"]
