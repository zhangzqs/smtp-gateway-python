"""HTTP API 管理配置与投递记录，保护认证和持久化错误边界。"""

import asyncio
import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from smtp_gateway import GatewayConfig, create_app
from smtp_gateway.services import DeliveryWorker, RelayService
from smtp_gateway_client import (
    DeliveryState,
    DeliveryStatus,
    MessageContent,
    MessagePage,
    MessageRecord,
)

_AUTH = ("manager", "test-management-credential")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
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
            "database": {
                "url": f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}",
                "encryption_key": Fernet.generate_key().decode(),
            },
            "http": {"auth": {"username": _AUTH[0], "password": _AUTH[1]}},
            "smtp": {
                "auth": {"password": "test-submission-credential"},
                "allow_insecure_auth": True,
            },
            "limits": {"max_message_bytes": 1024},
        }
    )
    with TestClient(create_app(config)) as result:
        yield result


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/smtp"),
        ("PUT", "/v1/smtp"),
        ("GET", "/v1/messages"),
        ("GET", "/v1/messages/message-id/content"),
    ],
)
def test_basic_auth_is_required(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401
    invalid = client.request(method, path, auth=("invalid", "credential"))
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


def test_http_does_not_submit_messages_or_return_raw_content(
    client: TestClient,
) -> None:
    assert client.post("/v1/messages", auth=_AUTH, json={}).status_code == 405
    assert client.get("/v1/messages/unknown", auth=_AUTH).status_code == 404


def test_openapi_exposes_configuration_read_only_records_and_health(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()
    assert set(document["paths"]) == {
        "/healthz",
        "/v1/smtp",
        "/v1/messages",
        "/v1/messages/{identifier}/content",
    }
    assert set(document["paths"]["/v1/smtp"]) == {"get", "put"}
    assert set(document["paths"]["/v1/messages"]) == {"get"}
    assert set(document["paths"]["/v1/messages/{identifier}/content"]) == {"get"}


def test_empty_message_page(client: TestClient) -> None:
    response = client.get("/v1/messages", auth=_AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_message_page_passes_pagination_and_returns_recipient_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = AsyncMock(
        return_value=MessagePage(
            items=[
                MessageRecord(
                    id="message-id",
                    sender="sender@example.com",
                    created_at=datetime(2026, 10, 20, 8, tzinfo=UTC),
                    deliveries=[
                        DeliveryStatus(
                            recipient="receiver@example.com",
                            state=DeliveryState.PENDING,
                            attempts=1,
                            last_error="SMTPRecipientRefused",
                        )
                    ],
                )
            ],
            total=11,
            page=2,
            page_size=10,
        )
    )
    monkeypatch.setattr(RelayService, "list_messages", records)
    response = client.get("/v1/messages?page=2&page_size=10", auth=_AUTH)
    assert response.status_code == 200
    records.assert_awaited_once_with(page=2, page_size=10)
    item = response.json()["items"][0]
    assert set(item) == {"id", "sender", "subject", "created_at", "deliveries"}
    assert item["created_at"] == "2026-10-20T08:00:00Z"
    assert item["deliveries"] == [
        {
            "recipient": "receiver@example.com",
            "state": "pending",
            "attempts": 1,
            "last_error": "SMTPRecipientRefused",
        }
    ]


@pytest.mark.parametrize(
    "query",
    ["page=0", "page=-1", "page=invalid", "page_size=0", "page_size=101"],
)
def test_message_page_rejects_invalid_pagination(
    client: TestClient, query: str
) -> None:
    assert client.get(f"/v1/messages?{query}", auth=_AUTH).status_code == 422


def test_failed_record_storage_returns_unavailable_without_internal_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RelayService,
        "list_messages",
        AsyncMock(side_effect=OperationalError("select", {}, RuntimeError("private"))),
    )
    response = client.get("/v1/messages", auth=_AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "Gateway storage is unavailable"}


def test_content_is_returned_as_json_without_rendering_html(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = MessageContent(
        id="message-id",
        subject="Alert",
        text="Private notification body",
        html="<script>alert('untrusted')</script><p>Notification</p>",
    )
    load = AsyncMock(return_value=content)
    monkeypatch.setattr(RelayService, "get_message_content", load)
    response = client.get("/v1/messages/message-id/content", auth=_AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == content.model_dump()
    load.assert_awaited_once_with("message-id")


def test_missing_or_expired_message_content_returns_not_found(
    client: TestClient,
) -> None:
    response = client.get("/v1/messages/unknown/content", auth=_AUTH)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Message not found"}


def test_content_storage_error_does_not_echo_body_or_database_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RelayService,
        "get_message_content",
        AsyncMock(
            side_effect=OperationalError(
                "select", {}, RuntimeError("private notification body")
            )
        ),
    )
    response = client.get("/v1/messages/message-id/content", auth=_AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "Gateway storage is unavailable"}


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
