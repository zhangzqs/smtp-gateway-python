"""HTTP API 只管理 SMTP 配置，并保护认证和持久化错误边界。"""

import asyncio
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from gstack_smtp_gateway import GatewayConfig, create_app
from gstack_smtp_gateway.services import DeliveryWorker, RelayService

_AUTH = ("manager", "test-management-credential")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    async def idle(_self: DeliveryWorker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(DeliveryWorker, "run", idle)
    monkeypatch.setattr("gstack_smtp_gateway.app.SmtpListener.start", AsyncMock())
    monkeypatch.setattr("gstack_smtp_gateway.app.SmtpListener.close", AsyncMock())
    config = GatewayConfig(
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}"),
        encryption_key=SecretStr(Fernet.generate_key().decode()),
        api_username=_AUTH[0],
        api_password=SecretStr(_AUTH[1]),
        smtp_password=SecretStr("test-submission-credential"),
        smtp_allow_insecure_auth=True,
        max_message_bytes=1024,
    )
    with TestClient(create_app(config)) as result:
        yield result


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_basic_auth_is_required(client: TestClient, method: str) -> None:
    assert client.request(method, "/v1/smtp").status_code == 401
    invalid = client.request(method, "/v1/smtp", auth=("invalid", "credential"))
    assert invalid.status_code == 401
    assert invalid.headers["www-authenticate"] == "Basic"
    assert client.get("/healthz").status_code == 200


def test_smtp_settings_hide_secrets_and_validate_password_changes(
    client: TestClient,
) -> None:
    assert client.get("/v1/smtp", auth=_AUTH).json()["configured"] is False
    value = {"host": "smtp.example.com", "port": 587, "username": "sender"}
    assert client.put("/v1/smtp", auth=_AUTH, json=value).status_code == 400
    saved = client.put(
        "/v1/smtp",
        auth=_AUTH,
        json={**value, "password": "private-upstream-credential"},
    )
    assert saved.status_code == 200
    assert saved.json()["has_password"] is True
    assert "private-upstream-credential" not in saved.text
    preserved = client.put("/v1/smtp", auth=_AUTH, json={**value, "port": 465})
    assert preserved.status_code == 200
    assert preserved.json()["has_password"] is True
    changed = client.put("/v1/smtp", auth=_AUTH, json={**value, "username": "new"})
    assert changed.status_code == 400
    assert client.get("/v1/smtp", auth=_AUTH).json()["username"] == "sender"


def test_message_submission_and_status_are_not_http_endpoints(
    client: TestClient,
) -> None:
    assert client.post("/v1/messages", auth=_AUTH, json={}).status_code == 404
    assert client.get("/v1/messages/unknown", auth=_AUTH).status_code == 404


def test_openapi_contains_only_configuration_and_health(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert set(document["paths"]) == {"/healthz", "/v1/smtp"}
    assert set(document["paths"]["/v1/smtp"]) == {"get", "put"}
    assert not any(
        name.startswith("Message") for name in document["components"]["schemas"]
    )


def test_failed_settings_storage_returns_unavailable_without_secrets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RelayService,
        "put_smtp",
        AsyncMock(
            side_effect=OperationalError("update", {}, RuntimeError("unavailable"))
        ),
    )
    result = client.put(
        "/v1/smtp",
        auth=_AUTH,
        json={
            "host": "smtp.example.com",
            "port": 587,
            "username": "sender",
            "password": "private-upstream-credential",
        },
    )
    assert result.status_code == 503
    assert "private-upstream-credential" not in result.text


@pytest.mark.parametrize(
    "value",
    [
        {"host": {}, "port": 587, "password": "do-not-echo"},
        {"host": "do-not-echo/invalid", "port": 587},
        {"host": "mailpit", "port": 1025, "password": ["do-not-echo"]},
    ],
)
def test_invalid_inputs_do_not_echo_passwords(client: TestClient, value: dict) -> None:
    response = client.put(
        "/v1/smtp",
        auth=_AUTH,
        json=value,
    )
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert "input" not in response.text


def test_settings_request_body_limit_is_64_kib(client: TestClient) -> None:
    value = b'{"host":"mailpit","port":1025,"tls":"none"}'
    boundary = value + b" " * (64 * 1024 - len(value))
    allowed = client.put(
        "/v1/smtp",
        auth=_AUTH,
        content=boundary,
        headers={"Content-Type": "application/json"},
    )
    assert allowed.status_code == 200
    oversized = client.put(
        "/v1/smtp",
        auth=_AUTH,
        content=boundary + b" ",
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
