"""受认证的 SMTP Submission 入口，协议处理复用 aiosmtpd。"""

import asyncio
import logging
import secrets
import ssl
from collections.abc import Callable
from typing import Protocol, cast

from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session
from sqlalchemy.exc import SQLAlchemyError
from typing_extensions import override

from smtp_gateway.shared import (
    MessageTooLargeError,
    NotConfiguredError,
    validate_address,
)

logger = logging.getLogger(__name__)


class _Messages(Protocol):
    async def submit_raw(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str: ...


class SubmissionHandler:
    """只在数据库事务提交成功后发送 SMTP 250 确认。"""

    def __init__(self, *, service: _Messages, max_recipients: int) -> None:
        self._service = service
        self._max_recipients = max_recipients
        self._active: set[asyncio.Task[object]] = set()

    async def handle_RCPT(
        self,
        _server: SMTP,
        _session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        if len(envelope.rcpt_tos) >= self._max_recipients:
            return "452 4.5.3 Too many recipients"
        try:
            validate_address(address)
        except ValueError:
            return "553 5.1.3 Invalid recipient address"
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 2.1.5 Recipient accepted"

    async def handle_DATA(
        self, _server: SMTP, _session: Session, envelope: Envelope
    ) -> str:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            if envelope.mail_from is None or envelope.original_content is None:
                return "451 4.3.0 Message envelope is incomplete"
            identifier = await self._service.submit_raw(
                sender=envelope.mail_from,
                recipients=envelope.rcpt_tos,
                content=envelope.original_content,
            )
        except NotConfiguredError:
            return "451 4.3.5 Upstream SMTP is not configured"
        except MessageTooLargeError:
            return "552 5.3.4 Message is too large"
        except ValueError:
            return "553 5.1.3 Invalid message envelope"
        except SQLAlchemyError:
            logger.exception("SMTP submission could not be persisted")
            return "451 4.3.0 Unable to persist message; retry later"
        finally:
            if task is not None:
                self._active.discard(task)
        return f"250 2.0.0 Queued {identifier}"

    async def drain(self) -> None:
        """等待连接关闭触发的提交协程退出，再关闭数据库。"""
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)


class _TrackedSMTP(SMTP):
    def __init__(
        self,
        *,
        connections: set[asyncio.Transport],
        handler: SubmissionHandler,
        loop: asyncio.AbstractEventLoop,
        authenticate: Callable[[SMTP, Session, Envelope, str, object], AuthResult],
        tls_context: ssl.SSLContext | None,
        max_message_bytes: int,
    ) -> None:
        self._connections = connections
        self._connection: asyncio.Transport | None = None
        super().__init__(
            handler=handler,
            loop=loop,
            hostname="smtp-gateway",
            authenticator=authenticate,
            auth_required=True,
            auth_require_tls=tls_context is not None,
            tls_context=tls_context,
            require_starttls=tls_context is not None,
            decode_data=False,
            enable_SMTPUTF8=False,
            data_size_limit=max_message_bytes,
        )

    @override
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        connection = cast(asyncio.Transport, transport)
        if self._connection is not None:
            self._connections.discard(self._connection)
        self._connection = connection
        self._connections.add(connection)
        super().connection_made(transport)

    @override
    def connection_lost(self, error: Exception | None) -> None:
        if self._connection is not None:
            self._connections.discard(self._connection)
        super().connection_lost(error)


class SmtpListener:
    """将 SMTP socket 生命周期绑定到独立 HTTP 服务的 lifespan。"""

    def __init__(
        self,
        *,
        handler: SubmissionHandler,
        host: str,
        port: int,
        username: str,
        password: str,
        tls_context: ssl.SSLContext | None,
        max_message_bytes: int,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._username = username.encode()
        self._password = password.encode()
        self._tls_context = tls_context
        self._max_message_bytes = max_message_bytes
        self._connections: set[asyncio.Transport] = set()
        self._server: asyncio.Server | None = None

    def _authenticate(
        self,
        _server: SMTP,
        _session: Session,
        _envelope: Envelope,
        _mechanism: str,
        auth_data: object,
    ) -> AuthResult:
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        username_matches = secrets.compare_digest(auth_data.login, self._username)
        password_matches = secrets.compare_digest(auth_data.password, self._password)
        return AuthResult(success=username_matches and password_matches, handled=False)

    async def start(self) -> None:
        """监听非特权 SMTP 端口，认证可要求 STARTTLS。"""
        # aiosmtpd 的 DEBUG 日志包含 DATA 正文，不能继承宿主 DEBUG 级别。
        logging.getLogger("mail.log").setLevel(logging.WARNING)
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: _TrackedSMTP(
                connections=self._connections,
                handler=self._handler,
                loop=loop,
                authenticate=self._authenticate,
                tls_context=self._tls_context,
                max_message_bytes=self._max_message_bytes,
            ),
            self._host,
            self._port,
        )

    async def close(self) -> None:
        """先停止接收并关闭现有连接，确保不会写入已关闭的数据库。"""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for connection in tuple(self._connections):
            connection.close()
        await self._handler.drain()


__all__ = ["SmtpListener", "SubmissionHandler"]
