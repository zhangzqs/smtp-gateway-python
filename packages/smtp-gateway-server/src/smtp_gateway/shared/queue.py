"""按收件人投递时由持久层与服务层共享的类型。"""

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, SecretStr

from smtp_gateway_client import TlsMode


class NotConfiguredError(Exception):
    """尚未配置上游，不能确认接收邮件。"""


class MessageTooLargeError(ValueError):
    """接收的邮件超过允许字节数。"""


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


@dataclass(frozen=True, slots=True, kw_only=True)
class Upstream:
    host: str
    port: int
    username: str
    password: SecretStr | None = field(repr=False)
    tls: TlsMode


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedDelivery:
    id: str
    message_id: str
    token: str
    sender: str
    recipient: str
    content: bytes = field(repr=False)
    attempts: int


__all__ = [
    "ClaimedDelivery",
    "DeliveryState",
    "DeliveryStatus",
    "MessageStatus",
    "MessageTooLargeError",
    "NotConfiguredError",
    "Upstream",
]
