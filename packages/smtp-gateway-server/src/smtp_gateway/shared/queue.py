"""批量投递与收件人状态管理共用的类型。"""

from dataclasses import dataclass, field

from pydantic import BaseModel, SecretStr

from smtp_gateway_client import DeliveryState, DeliveryStatus, TlsMode


class NotConfiguredError(Exception):
    """尚未配置上游，不能确认接收邮件。"""


class MessageTooLargeError(ValueError):
    """接收的邮件超过允许字节数。"""


class MessageStatus(BaseModel):
    id: str
    deliveries: list[DeliveryStatus]


@dataclass(frozen=True, slots=True, kw_only=True)
class Upstream:
    host: str
    port: int
    username: str
    from_address: str
    password: SecretStr | None = field(repr=False)
    tls: TlsMode

    @property
    def sender(self) -> str:
        return self.from_address or self.username


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedRecipient:
    id: str
    recipient: str
    attempts: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedDelivery:
    message_id: str
    token: str
    sender: str
    recipients: tuple[ClaimedRecipient, ...]
    content: bytes = field(repr=False)


__all__ = [
    "ClaimedDelivery",
    "ClaimedRecipient",
    "DeliveryState",
    "DeliveryStatus",
    "MessageStatus",
    "MessageTooLargeError",
    "NotConfiguredError",
    "Upstream",
]
