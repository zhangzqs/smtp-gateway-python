"""网关缺失必需凭据或不安全监听配置时尽早失败。"""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from gstack_smtp_gateway import GatewayConfig


@pytest.mark.parametrize(
    "override",
    [
        {"api_password": ""},
        {"encryption_key": "invalid"},
        {"smtp_enabled": True},
        {"smtp_enabled": True, "smtp_password": "credential"},
        {"database_url": "mysql+asyncmy://localhost/database"},
        {"database_url": "invalid-url"},
        {"lease_seconds": 60, "delivery_timeout": 60},
        {"smtp_tls_cert": "certificate.pem"},
    ],
)
def test_invalid_runtime_configuration_is_rejected(override: dict[str, object]) -> None:
    values = {
        "api_username": "manager",
        "api_password": "test-api-credential",
        "encryption_key": Fernet.generate_key().decode(),
        "smtp_enabled": False,
    }
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({**values, **override})


def test_validated_configuration_does_not_expose_secrets() -> None:
    values = {
        "api_username": "manager",
        "api_password": "test-api-credential",
        "smtp_password": "test-smtp-credential",
        "encryption_key": Fernet.generate_key().decode(),
        "database_url": "postgresql+asyncpg://username:private-db-credential@db/gateway",
        "smtp_allow_insecure_auth": True,
    }
    config = GatewayConfig.model_validate(values)
    for field in ["api_password", "smtp_password", "encryption_key", "database_url"]:
        value = values[field]
        assert isinstance(value, str)
        assert value not in repr(config)
        assert value not in config.model_dump_json()
