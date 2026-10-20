"""协议、持久层和服务层共用的稳定类型。"""

from .queue import (
    ClaimedDelivery,
    DeliveryState,
    DeliveryStatus,
    MessageStatus,
    MessageTooLargeError,
    NotConfiguredError,
    Upstream,
)
from .smtp import SmtpSettings, SmtpSettingsWrite, TlsMode, validate_address

__all__ = [
    "ClaimedDelivery",
    "DeliveryState",
    "DeliveryStatus",
    "MessageStatus",
    "MessageTooLargeError",
    "NotConfiguredError",
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
    "Upstream",
    "validate_address",
]
