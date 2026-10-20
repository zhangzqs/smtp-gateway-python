"""SMTP 网关异步管理客户端及共享配置类型。"""

from .client import SmtpGatewayAPIError, SmtpGatewayClient
from .models import SmtpSettings, SmtpSettingsWrite, TlsMode

__all__ = [
    "SmtpGatewayAPIError",
    "SmtpGatewayClient",
    "SmtpSettings",
    "SmtpSettingsWrite",
    "TlsMode",
]
