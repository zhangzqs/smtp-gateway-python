"""HTTP 提交只有持久化完成后才能返回接收成功。"""

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
from gstack_smtp_gateway.database import Database
from gstack_smtp_gateway.delivery import DeliveryWorker

_AUTH = ("manager", "test-management-credential")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    async def idle(_self: DeliveryWorker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(DeliveryWorker, "run", idle)
    config = GatewayConfig(
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}"),
        encryption_key=SecretStr(Fernet.generate_key().decode()),
        api_username=_AUTH[0],
        api_password=SecretStr(_AUTH[1]),
        smtp_enabled=False,
        max_message_bytes=1024,
    )
    with TestClient(create_app(config)) as result:
        yield result


@pytest.mark.parametrize("path", ["/v1/smtp", "/v1/messages/unknown"])
def test_basic_auth_is_required(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    invalid = client.get(path, auth=("invalid", "credential"))
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


def test_http_submission_creates_durable_recipient_statuses(client: TestClient) -> None:
    value = {
        "sender": "alerts@example.com",
        "recipients": ["one@example.com", "two@example.com"],
        "subject": "Node alert",
        "text": "A worker is offline",
    }
    assert client.post("/v1/messages", auth=_AUTH, json=value).status_code == 409
    assert (
        client.put(
            "/v1/smtp",
            auth=_AUTH,
            json={"host": "mailpit", "port": 1025, "tls": "none"},
        ).status_code
        == 200
    )
    accepted = client.post("/v1/messages", auth=_AUTH, json=value)
    assert accepted.status_code == 202
    identifier = accepted.json()["id"]
    status = client.get(f"/v1/messages/{identifier}", auth=_AUTH)
    assert status.status_code == 200
    assert {item["recipient"] for item in status.json()["deliveries"]} == set(
        value["recipients"]
    )
    assert all(item["state"] == "pending" for item in status.json()["deliveries"])
    assert "A worker is offline" not in status.text
    assert client.get("/v1/messages/unknown", auth=_AUTH).status_code == 404


def test_failed_storage_never_returns_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Database,
        "enqueue",
        AsyncMock(
            side_effect=OperationalError("insert", {}, RuntimeError("unavailable"))
        ),
    )
    result = client.post(
        "/v1/messages",
        auth=_AUTH,
        json={
            "sender": "alerts@example.com",
            "recipients": ["receiver@example.com"],
            "subject": "Alert",
            "text": "private-message-body",
        },
    )
    assert result.status_code == 503
    assert "private-message-body" not in result.text


def test_invalid_inputs_do_not_echo_passwords(client: TestClient) -> None:
    response = client.put(
        "/v1/smtp",
        auth=_AUTH,
        json={"host": {}, "port": 587, "password": "do-not-echo"},
    )
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert "input" not in response.text


def test_message_and_request_body_limits(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        auth=_AUTH,
        json={
            "sender": "sender@example.com",
            "recipients": ["receiver@example.com"],
            "subject": "Alert",
            "text": "x" * 2048,
        },
    )
    assert response.status_code == 413
    oversized = client.put(
        "/v1/smtp",
        auth=_AUTH,
        content=b"x" * 100000,
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


@pytest.mark.parametrize("field", ["sender", "recipients", "subject"])
def test_header_and_envelope_injection_is_rejected(
    client: TestClient, field: str
) -> None:
    value: dict[str, object] = {
        "sender": "sender@example.com",
        "recipients": ["receiver@example.com"],
        "subject": "Alert",
        "text": "body",
    }
    value[field] = (
        ["recipient@example.com\r\nQUIT"]
        if field == "recipients"
        else "bad\r\nBcc: attacker@example.com"
    )
    assert client.post("/v1/messages", auth=_AUTH, json=value).status_code == 422
