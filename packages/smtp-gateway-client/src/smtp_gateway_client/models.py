"""SMTP 网关配置与投递记录接口的请求及安全响应。"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class TlsMode(StrEnum):
    NONE = "none"
    STARTTLS = "starttls"
    IMPLICIT = "implicit"


class _ResponseModel(BaseModel):
    """共享响应模型的字段过滤与错误脱敏约定。"""

    model_config = ConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
    )


class SmtpSettings(_ResponseModel):
    """过滤响应中的额外字段，保留调用方需要的设置状态。"""

    configured: bool
    host: str = ""
    port: int = Field(
        default=587,
        ge=1,
        le=65535,
    )
    username: str = ""
    from_address: str = ""
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
    from_address: str = Field(default="", max_length=320)
    password: SecretStr | None = None
    tls: TlsMode = TlsMode.STARTTLS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """主机名不允许嵌入认证信息、端口或路径。"""
        if value != value.strip() or any(char in value for char in "/@:# \t\r\n"):
            raise ValueError("SMTP host must be a hostname or IPv4 address")
        return value


class DeliveryState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"


class DeliveryStatus(_ResponseModel):
    """单个收件人的投递状态，不包含邮件正文。"""

    recipient: str
    state: DeliveryState
    attempts: int
    last_error: str | None
    sender: str | None = None


class MessageRecord(_ResponseModel):
    """邮件信封及收件人状态，时间统一为 UTC。"""

    id: str
    sender: str
    subject: str
    created_at: datetime
    deliveries: list[DeliveryStatus]

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """拒绝无时区时间，并将带时区时间转换为 UTC。"""
        if value.utcoffset() is None:
            raise ValueError("Message timestamp must include a time zone")
        return value.astimezone(UTC)


class MessageContent(_ResponseModel):
    """邮件主题和可预览正文；不包含附件或原始邮件。"""

    id: str
    subject: str
    text: str | None = Field(default=None, repr=False)
    html: str | None = Field(default=None, repr=False)


class MessagePage(_ResponseModel):
    """分页返回邮件投递记录。"""

    items: list[MessageRecord]
    total: int = Field(ge=0)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


__all__ = [
    "DeliveryState",
    "DeliveryStatus",
    "MessageContent",
    "MessagePage",
    "MessageRecord",
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
]
