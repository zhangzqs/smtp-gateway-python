"""SMTP 提交遵守事务确认、大小限制和认证失败响应。"""

import asyncio
import logging
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session
from sqlalchemy.exc import OperationalError

from smtp_gateway.services import MessageService
from smtp_gateway.shared import MessageTooLargeError, NotConfiguredError
from smtp_gateway.smtp_server import SmtpListener, SubmissionHandler


@pytest.mark.asyncio
async def test_smtp_ack_waits_for_persistence() -> None:
    commit = asyncio.Event()
    started = asyncio.Event()

    async def submit(**_kwargs: object) -> str:
        started.set()
        await commit.wait()
        return "persisted-message"

    service = AsyncMock()
    service.submit_raw.side_effect = submit
    handler = SubmissionHandler(service=service, max_recipients=10)
    envelope = Envelope()
    envelope.mail_from = "sender@example.com"
    envelope.rcpt_tos = ["receiver@example.com"]
    envelope.original_content = b"Subject: alert\r\n\r\nbody\r\n"
    pending = asyncio.create_task(
        handler.handle_DATA(cast(SMTP, None), cast(Session, None), envelope)
    )
    await started.wait()
    assert not pending.done()
    commit.set()
    assert await pending == "250 2.0.0 Queued persisted-message"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotConfiguredError(), "451"),
        (OperationalError("insert", {}, RuntimeError()), "451"),
        (MessageTooLargeError(), "552"),
        (ValueError(), "553"),
    ],
)
@pytest.mark.asyncio
async def test_failed_submission_never_acknowledges(
    error: Exception, expected: str
) -> None:
    service = AsyncMock()
    service.submit_raw.side_effect = error
    handler = SubmissionHandler(service=service, max_recipients=10)
    envelope = Envelope()
    envelope.mail_from = "sender@example.com"
    envelope.original_content = b"mail"
    assert (
        await handler.handle_DATA(cast(SMTP, None), cast(Session, None), envelope)
    ).startswith(expected)


@pytest.mark.asyncio
async def test_recipient_limit_and_validation() -> None:
    handler = SubmissionHandler(service=AsyncMock(), max_recipients=1)
    envelope = Envelope()
    assert (
        await handler.handle_RCPT(
            cast(SMTP, None), cast(Session, None), envelope, "invalid", []
        )
    ).startswith("553")
    assert (
        await handler.handle_RCPT(
            cast(SMTP, None), cast(Session, None), envelope, "valid@example.com", []
        )
    ).startswith("250")
    assert (
        await handler.handle_RCPT(
            cast(SMTP, None), cast(Session, None), envelope, "other@example.com", []
        )
    ).startswith("452")


@pytest.mark.asyncio
async def test_smtp_credentials_are_checked_and_failures_are_unhandled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        @staticmethod
        def close() -> None:
            return None

        @staticmethod
        async def wait_closed() -> None:
            return None

    async def create_server(factory: object, *_args: object) -> FakeServer:
        captured["factory"] = factory
        return FakeServer()

    listener = SmtpListener(
        handler=SubmissionHandler(
            service=cast(MessageService, AsyncMock()), max_recipients=1
        ),
        host="127.0.0.1",
        port=1025,
        username="grafana",
        password="test-smtp-credential",  # noqa: S106 - 仅用于认证单元测试
        tls_context=None,
        max_message_bytes=1024,
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "create_server", create_server)

    def protocol(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("smtp_gateway.smtp_server.listener._TrackedSMTP", protocol)
    await listener.start()
    assert not logging.getLogger("mail.log").isEnabledFor(logging.DEBUG)
    factory = captured["factory"]
    assert callable(factory)
    factory()
    authenticate = captured["authenticate"]
    assert callable(authenticate)
    success = authenticate(
        None, None, None, "PLAIN", LoginPassword(b"grafana", b"test-smtp-credential")
    )
    assert isinstance(success, AuthResult)
    assert success.success
    failure = authenticate(
        None, None, None, "LOGIN", LoginPassword(b"grafana", b"wrong")
    )
    assert isinstance(failure, AuthResult)
    assert not failure.success
    assert not failure.handled
    await listener.close()


@pytest.mark.asyncio
async def test_plain_submission_advertises_login_and_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocols: list[SMTP] = []

    async def create_server(factory: object, *_args: object) -> AsyncMock:
        assert callable(factory)
        protocol = factory()
        assert isinstance(protocol, SMTP)
        protocols.append(protocol)
        return AsyncMock(close=Mock())

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_server", create_server)
    listener = SmtpListener(
        handler=SubmissionHandler(service=AsyncMock(), max_recipients=1),
        host="127.0.0.1",
        port=1025,
        username="grafana",
        password="test-smtp-credential",  # noqa: S106 - 仅用于认证行为测试
        tls_context=None,
        max_message_bytes=1024,
    )
    await listener.start()
    protocol = protocols[0]
    protocol.session = Session(loop)
    push = AsyncMock()
    monkeypatch.setattr(protocol, "push", push)
    try:
        await protocol.smtp_EHLO("grafana")
        responses = [call.args[0] for call in push.await_args_list]
        assert "250-AUTH LOGIN" in responses
        assert "250-STARTTLS" not in responses

        push.reset_mock()
        await protocol.smtp_MAIL("FROM:<sender@example.com>")
        push.assert_awaited_once_with("530 5.7.0 Authentication required")

        challenge = AsyncMock(
            side_effect=[b"grafana", b"test-smtp-credential"],
        )
        monkeypatch.setattr(protocol, "challenge_auth", challenge)
        push.reset_mock()
        await protocol.smtp_AUTH("LOGIN")
        assert [call.args[0] for call in challenge.await_args_list] == [
            "Username:",
            "Password:",
        ]
        push.assert_awaited_once_with("235 2.7.0 Authentication successful")
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_starttls_transport_replacement_does_not_retain_raw_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocols: list[SMTP] = []

    async def create_server(factory: object, *_args: object) -> AsyncMock:
        assert callable(factory)
        protocol = factory()
        assert isinstance(protocol, SMTP)
        protocols.append(protocol)
        return AsyncMock(close=Mock())

    monkeypatch.setattr(asyncio.get_running_loop(), "create_server", create_server)
    monkeypatch.setattr(SMTP, "connection_made", lambda *_args: None)
    listener = SmtpListener(
        handler=SubmissionHandler(service=AsyncMock(), max_recipients=1),
        host="127.0.0.1",
        port=1025,
        username="grafana",
        password="test-smtp-credential",  # noqa: S106 - 仅用于协议生命周期测试
        tls_context=None,
        max_message_bytes=1024,
    )
    await listener.start()
    raw_transport = Mock(spec=asyncio.Transport)
    tls_transport = Mock(spec=asyncio.Transport)
    protocols[0].connection_made(raw_transport)
    protocols[0].connection_made(tls_transport)
    await listener.close()
    raw_transport.close.assert_not_called()
    tls_transport.close.assert_called_once()
