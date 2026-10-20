"""SMTP 网关异步管理客户端及共享配置和投递记录类型。"""

from .client import SmtpGatewayAPIError, SmtpGatewayClient
from .models import (
    DeliveryState,
    DeliveryStatus,
    MessageContent,
    MessagePage,
    MessageRecord,
    SmtpSettings,
    SmtpSettingsWrite,
    TlsMode,
)

__all__ = [
    "DeliveryState",
    "DeliveryStatus",
    "MessageContent",
    "MessagePage",
    "MessageRecord",
    "SmtpGatewayAPIError",
    "SmtpGatewayClient",
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
]
