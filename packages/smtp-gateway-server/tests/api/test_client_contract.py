"""客户端公开接口与服务端管理 API 使用相同的协议模型。"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from smtp_gateway import GatewayConfig, create_app
from smtp_gateway.services import DeliveryWorker
from smtp_gateway_client import (
    SmtpGatewayAPIError,
    SmtpGatewayClient,
    SmtpSettingsWrite,
    TlsMode,
)


@pytest.mark.asyncio
async def test_client_manages_settings_through_server_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in tuple(os.environ):
        if name.startswith("SMTP_GATEWAY_"):
            monkeypatch.delenv(name)

    async def idle(_self: DeliveryWorker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(DeliveryWorker, "run", idle)
    monkeypatch.setattr("smtp_gateway.app.SmtpListener.start", AsyncMock())
    monkeypatch.setattr("smtp_gateway.app.SmtpListener.close", AsyncMock())
    config = GatewayConfig.model_validate(
        {
            "http": {"auth": {"username": "manager", "password": "api-credential"}},
            "smtp": {
                "auth": {"password": "submission-credential"},
                "allow_insecure_auth": True,
            },
            "database": {
                "url": f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}",
                "encryption_key": Fernet.generate_key().decode(),
            },
        }
    )
    app = create_app(config)
    sdk = SmtpGatewayClient(
        url="http://gateway",
        username="manager",
        password=config.http.auth.password.get_secret_value(),
        transport=httpx.ASGITransport(app=app),
    )
    async with app.router.lifespan_context(app):
        try:
            assert not (await sdk.get()).configured
            saved = await sdk.put(
                SmtpSettingsWrite(
                    host="smtp.example.com",
                    port=587,
                    username="sender",
                    password=SecretStr("upstream-credential"),
                    tls=TlsMode.STARTTLS,
                )
            )
            assert saved.configured
            assert saved.has_password
            assert "upstream-credential" not in saved.model_dump_json()
            preserved = await sdk.put(
                SmtpSettingsWrite(host="smtp.example.com", port=465, username="sender")
            )
            assert preserved.has_password
            assert (await sdk.get()).port == 465
        finally:
            await sdk.close()

        invalid_value = "incorrect-credential"
        invalid = SmtpGatewayClient(
            url="http://gateway",
            username="manager",
            password=invalid_value,
            transport=httpx.ASGITransport(app=app),
        )
        try:
            with pytest.raises(SmtpGatewayAPIError) as error:
                await invalid.get()
            assert error.value.status_code == 401
        finally:
            await invalid.close()
