"""独立服务命令入口。"""

import argparse
import logging
from importlib.metadata import version

import uvicorn
from pydantic import ValidationError

from .app import create_app
from .config import GatewayConfig


def main() -> None:
    """读取环境配置后启动独立网关进程。"""
    parser = argparse.ArgumentParser(
        description="Durable SMTP gateway configured with GSTACK_SMTP_GATEWAY_* variables"
    )
    parser.add_argument(
        "--version", action="version", version=version("gstack-smtp-gateway")
    )
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = GatewayConfig()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in exc.errors())
        raise SystemExit(f"Invalid SMTP gateway configuration: {fields}") from None
    uvicorn.run(create_app(config), host=config.http_host, port=config.http_port)


__all__ = ["main"]
