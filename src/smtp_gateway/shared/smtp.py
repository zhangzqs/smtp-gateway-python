"""SMTP 上游配置与信封地址的共享类型。"""

from email.errors import HeaderParseError
from email.headerregistry import Address
from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr, field_validator


class TlsMode(StrEnum):
    NONE = "none"
    STARTTLS = "starttls"
    IMPLICIT = "implicit"


class SmtpSettingsWrite(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    password: SecretStr | None = None
    tls: TlsMode = TlsMode.STARTTLS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value != value.strip() or any(char in value for char in "/@:# \t\r\n"):
            raise ValueError("SMTP host must be a hostname or IPv4 address")
        return value


class SmtpSettings(BaseModel):
    configured: bool
    read_only: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    tls: TlsMode = TlsMode.STARTTLS
    has_password: bool = False


def validate_address(value: str) -> str:
    """使用标准邮件解析器验证不含展示名称的 ASCII 信封地址。"""
    if not value or len(value) > 320 or any(char in value for char in "\r\n\x00"):
        raise ValueError("A valid mailbox address is required")
    try:
        value.encode("ascii")
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as exc:
        raise ValueError("An ASCII mailbox address is required") from exc
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("A mailbox without a display name is required")
    return value


__all__ = [
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
    "validate_address",
]
