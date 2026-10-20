"""独立运行的持久化告警邮件网关。"""

from .app import create_app
from .config import GatewayConfig

__all__ = ["GatewayConfig", "create_app"]
