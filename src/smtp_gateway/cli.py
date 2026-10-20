"""独立服务命令入口。"""

import argparse
import logging
from importlib.metadata import version
from pathlib import Path

import uvicorn
from pydantic import ValidationError
from pydantic_settings import SettingsError

from .app import create_app
from .config import ConfigurationFileError, GatewayConfig


def main() -> None:
    """合并显式 YAML 和环境变量后启动独立网关进程。"""
    parser = argparse.ArgumentParser(
        description="Durable SMTP gateway configured with YAML and SMTP_GATEWAY_* variables"
    )
    parser.add_argument(
        "--version", action="version", version=version("smtp-gateway-python")
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        help="Load YAML configuration; environment variables override file values",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = GatewayConfig.load(args.config_file)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(map(str, item["loc"])) or "configuration"
            for item in exc.errors(include_input=False, include_context=False)
        )
        raise SystemExit(f"Invalid SMTP gateway configuration: {fields}") from None
    except ConfigurationFileError as exc:
        raise SystemExit(f"Invalid SMTP gateway configuration file: {exc}") from None
    except SettingsError:
        raise SystemExit("Invalid SMTP gateway environment configuration") from None
    uvicorn.run(create_app(config), host=config.http.host, port=config.http.port)


__all__ = ["main"]
