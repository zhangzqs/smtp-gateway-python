"""验证客户端的认证、序列化和安全错误边界。"""

import base64
import json
from collections.abc import Callable
from http import HTTPMethod
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from pydantic import SecretStr
from typing_extensions import override

from smtp_gateway_client import (
    DeliveryState,
    MessageContent,
    MessagePage,
    SmtpGatewayAPIError,
    SmtpGatewayClient,
    SmtpSettingsWrite,
    TlsMode,
)

_API_VALUE = "private-api-credential"
_SMTP_VALUE = "private-upstream-credential"


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "configured": True,
            "host": "smtp.example.com",
            "port": 587,
            "username": "mailer",
            "tls": "starttls",
            "has_password": True,
            "password": "private-response-credential",
        },
    )


@pytest.mark.asyncio
async def test_basic_auth_paths_and_password_serialization() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response()

    client = SmtpGatewayClient(
        url="https://gateway.example.com/",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    body = SmtpSettingsWrite(host="smtp.example.com", port=587, username="mailer")
    try:
        result = await client.get()
        await client.put(body)
        await client.put(body.model_copy(update={"password": SecretStr(_SMTP_VALUE)}))
        await client.put(body.model_copy(update={"password": SecretStr("")}))
    finally:
        await client.close()

    authorization = base64.b64encode(f"operator:{_API_VALUE}".encode()).decode()
    assert [request.method for request in requests] == ["GET", "PUT", "PUT", "PUT"]
    assert all(request.url.path == "/v1/smtp" for request in requests)
    assert all(
        request.headers["authorization"] == f"Basic {authorization}"
        for request in requests
    )
    assert json.loads(requests[1].content) == {
        "host": "smtp.example.com",
        "port": 587,
        "username": "mailer",
        "tls": "starttls",
    }
    assert json.loads(requests[2].content)["password"] == _SMTP_VALUE
    assert json.loads(requests[3].content)["password"] == ""
    assert result.tls == TlsMode.STARTTLS
    assert "password" not in result.model_dump()
    assert "private-response-credential" not in result.model_dump_json()
    assert "private-response-credential" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [HTTPMethod.GET, HTTPMethod.PUT])
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (httpx.Response(401, text=_API_VALUE), 401),
        (httpx.Response(500, text=_SMTP_VALUE), 500),
        (httpx.Response(200, content=_SMTP_VALUE.encode()), 502),
        (httpx.Response(200, json={"password": _SMTP_VALUE}), 502),
        (httpx.Response(200, json={"configured": True, "port": 0}), 502),
        (httpx.Response(200, json={"configured": True, "tls": _SMTP_VALUE}), 502),
        (httpx.ConnectError(_API_VALUE), 502),
        (httpx.ReadTimeout(_SMTP_VALUE), 502),
    ],
)
async def test_errors_map_status_without_exposing_credentials_or_response(
    method: HTTPMethod,
    outcome: httpx.Response | httpx.HTTPError,
    expected_status: int,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        operation = (
            client.get()
            if method == HTTPMethod.GET
            else client.put(
                SmtpSettingsWrite(
                    host="smtp.example.com",
                    port=587,
                    password=SecretStr(_SMTP_VALUE),
                )
            )
        )
        with pytest.raises(SmtpGatewayAPIError) as error:
            await operation
    finally:
        await client.close()
    assert error.value.status_code == expected_status
    assert str(error.value) == (
        f"SMTP gateway {method} /v1/smtp failed with HTTP {expected_status}"
    )
    assert _API_VALUE not in str(error.value)
    assert _SMTP_VALUE not in str(error.value)
    assert _API_VALUE not in repr(error.value)
    assert _SMTP_VALUE not in repr(error.value)


@pytest.mark.asyncio
async def test_redirect_does_not_forward_credentials_to_another_origin() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307, headers={"Location": "https://other.example.com/v1/smtp"}
        )

    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(SmtpGatewayAPIError) as error:
            await client.get()
    finally:
        await client.close()
    assert error.value.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.com"


@pytest.mark.asyncio
async def test_default_http_configuration_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(lambda _request: _response())
    transport_constructor = Mock(return_value=transport)
    client_constructor = Mock(wraps=httpx.AsyncClient)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_constructor)
    monkeypatch.setattr(httpx, "AsyncClient", client_constructor)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/missing/ca.pem")
    client = SmtpGatewayClient(
        url="https://gateway.example.com", username="operator", password=_API_VALUE
    )
    try:
        assert (await client.get()).configured
    finally:
        await client.close()
    transport_constructor.assert_called_once_with(trust_env=False)
    options = client_constructor.call_args.kwargs
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"] == 15


class _ClosingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.closed = False

    @override
    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


@pytest.mark.asyncio
async def test_close_releases_the_injected_transport() -> None:
    transport = _ClosingTransport(lambda _request: _response())
    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=transport,
    )
    await client.get()
    assert not transport.closed
    await client.close()
    assert transport.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"page": 3, "page_size": 100}])
@pytest.mark.parametrize("subject", ["", "告警测试"])
async def test_list_messages_uses_basic_auth_and_pagination_query(
    options: dict[str, int],
    subject: str,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "message-id",
                        "sender": "sender@example.com",
                        "subject": subject,
                        "created_at": "2026-10-20T04:30:00Z",
                        "content": "private-email-body",
                        "deliveries": [
                            {
                                "recipient": "recipient@example.com",
                                "state": "pending",
                                "attempts": 1,
                                "last_error": "SMTPDataError",
                            }
                        ],
                    }
                ],
                "total": 201,
                "page": int(request.url.params["page"]),
                "page_size": int(request.url.params["page_size"]),
            },
        )

    client = SmtpGatewayClient(
        url="https://gateway.example.com/",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await client.list_messages(**options)
    finally:
        await client.close()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/messages"
    assert dict(request.url.params) == {
        "page": str(options.get("page", 1)),
        "page_size": str(options.get("page_size", 20)),
    }
    authorization = base64.b64encode(f"operator:{_API_VALUE}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {authorization}"
    assert isinstance(result, MessagePage)
    assert result.total == 201
    assert result.page == options.get("page", 1)
    assert result.page_size == options.get("page_size", 20)
    assert result.items[0].deliveries[0].state == DeliveryState.PENDING
    assert result.items[0].subject == subject
    assert "private-email-body" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (httpx.Response(401, text=_API_VALUE), 401),
        (httpx.Response(500, text=_SMTP_VALUE), 500),
        (
            httpx.Response(
                307, headers={"Location": "https://other.example.com/v1/messages"}
            ),
            307,
        ),
        (httpx.Response(200, content=_SMTP_VALUE.encode()), 502),
        (httpx.Response(200, json={"items": _SMTP_VALUE, "total": 0}), 502),
        (
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "message-id",
                            "sender": "sender@example.com",
                            "created_at": "2026-10-20T04:30:00Z",
                            "deliveries": [],
                            "content": _SMTP_VALUE,
                            "password": _API_VALUE,
                        }
                    ],
                    "total": 1,
                },
            ),
            502,
        ),
        (httpx.ConnectError(_API_VALUE), 502),
        (httpx.ReadTimeout(_SMTP_VALUE), 502),
    ],
)
async def test_list_messages_errors_are_safe_and_do_not_follow_redirects(
    outcome: httpx.Response | httpx.HTTPError, expected_status: int
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(SmtpGatewayAPIError) as error:
            await client.list_messages()
    finally:
        await client.close()
    assert error.value.status_code == expected_status
    assert str(error.value) == (
        f"SMTP gateway GET /v1/messages failed with HTTP {expected_status}"
    )
    assert _API_VALUE not in str(error.value)
    assert _SMTP_VALUE not in str(error.value)
    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "page_size"), [(0, 20), (-1, 20), (1, 0), (1, 101)])
async def test_list_messages_rejects_invalid_pagination_before_request(
    page: int, page_size: int
) -> None:
    respond = Mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(ValueError, match="Message page"):
            await client.list_messages(page=page, page_size=page_size)
    finally:
        await client.close()
    respond.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "segment"),
    [
        ("message-id", "message-id"),
        ("a/b?c#d%", "a%2Fb%3Fc%23d%25"),
        ("..", "%2E%2E"),
    ],
)
async def test_get_message_content_uses_basic_auth_and_encodes_the_identifier(
    identifier: str, segment: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": identifier,
                "subject": "告警测试",
                "text": "Alert details",
                "html": "<p>Alert details</p>",
                "password": _SMTP_VALUE,
            },
        )

    client = SmtpGatewayClient(
        url="https://gateway.example.com/",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await client.get_message_content(identifier)
    finally:
        await client.close()

    assert isinstance(result, MessageContent)
    assert result.id == identifier
    assert result.subject == "告警测试"
    assert result.text == "Alert details"
    assert result.html == "<p>Alert details</p>"
    assert _SMTP_VALUE not in result.model_dump_json()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.raw_path == f"/v1/messages/{segment}/content".encode()
    authorization = base64.b64encode(f"operator:{_API_VALUE}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {authorization}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (httpx.Response(401, text=_API_VALUE), 401),
        (httpx.Response(404, text="private-email-body"), 404),
        (httpx.Response(500, text="private-email-body"), 500),
        (
            httpx.Response(
                307,
                headers={
                    "Location": "https://other.example.com/v1/messages/id/content"
                },
            ),
            307,
        ),
        (httpx.Response(200, content=b"private-email-body"), 502),
        (httpx.Response(200, json={"id": "private-email-body"}), 502),
        (httpx.ConnectError(_API_VALUE), 502),
    ],
)
async def test_message_content_errors_hide_response_content_and_credentials(
    outcome: httpx.Response | httpx.HTTPError, expected_status: int
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    client = SmtpGatewayClient(
        url="https://gateway.example.com",
        username="operator",
        password=_API_VALUE,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(SmtpGatewayAPIError) as error:
            await client.get_message_content("message-id")
    finally:
        await client.close()

    assert error.value.status_code == expected_status
    assert str(error.value) == (
        "SMTP gateway GET /v1/messages/message-id/content "
        f"failed with HTTP {expected_status}"
    )
    assert _API_VALUE not in repr(error.value)
    assert _SMTP_VALUE not in repr(error.value)
    assert "private-email-body" not in repr(error.value)
    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.com"
