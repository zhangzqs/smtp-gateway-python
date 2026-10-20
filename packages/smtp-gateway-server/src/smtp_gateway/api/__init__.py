"""受认证的上游 SMTP 配置与投递记录查询接口。"""

from .router import create_router

__all__ = ["create_router"]
