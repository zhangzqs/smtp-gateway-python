"""验证客户端的认证、序列化和安全错误边界。"""

import base64
import json
from collections.abc import Callable
from http import HTTPMethod
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from typing_extensions import override

from smtp_gateway_client import (
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
    assert result.read_only is False
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
