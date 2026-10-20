"""HTTP 边界的配置、邮件和安全响应模型。"""

from email.errors import HeaderParseError
from email.headerregistry import Address
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


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


class MessageWrite(BaseModel):
    sender: str
    recipients: list[str] = Field(min_length=1, max_length=1000)
    subject: str = Field(max_length=998)
    text: str | None = None
    html: str | None = None

    @field_validator("sender")
    @classmethod
    def validate_sender(cls, value: str) -> str:
        return validate_address(value)

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_address(value) for value in values))

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("Subject must not contain control characters")
        return value

    @model_validator(mode="after")
    def validate_body(self) -> Self:
        if self.text is None and self.html is None:
            raise ValueError("At least one of text or html is required")
        return self


class MessageAccepted(BaseModel):
    id: str


class DeliveryState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"


class DeliveryStatus(BaseModel):
    recipient: str
    state: DeliveryState
    attempts: int
    last_error: str | None


class MessageStatus(BaseModel):
    id: str
    deliveries: list[DeliveryStatus]


__all__ = [
    "DeliveryState",
    "DeliveryStatus",
    "MessageAccepted",
    "MessageStatus",
    "MessageWrite",
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
    "validate_address",
]
