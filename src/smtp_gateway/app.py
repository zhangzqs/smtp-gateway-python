"""组装独立 HTTP 服务、SMTP 监听器和持久化投递 worker。"""

import asyncio
import logging
import ssl
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import create_router
from .config import GatewayConfig
from .persistence import Database
from .services import DeliveryWorker, MessageService, RelayService, RetryPolicy
from .smtp_client import SmtpSender
from .smtp_server import SmtpListener, SubmissionHandler

logger = logging.getLogger(__name__)


class _BodyLimit:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "PUT":
            await self._app(scope, receive, send)
            return
        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(buffered) + len(chunk) > self._max_bytes:
                await JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )(scope, receive, send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break
        body = bytes(buffered)

        async def replay() -> Message:
            nonlocal body
            message = {"type": "http.request", "body": body}
            body = b""
            return message

        await self._app(scope, replay, send)


def create_app(config: GatewayConfig) -> FastAPI:
    """启动时完成数据库与监听端口检查，退出时先停止接收再关闭连接。"""
    database = Database(
        engine=create_async_engine(
            config.database.url.get_secret_value(), pool_pre_ping=True
        ),
    )
    relay = RelayService(
        database=database,
        encryption_key=config.database.encryption_key.get_secret_value(),
    )
    messages = MessageService(
        relay=relay,
        max_message_bytes=config.limits.max_message_bytes,
        max_recipients=config.limits.max_recipients,
    )
    worker = DeliveryWorker(
        relay=relay,
        sender=SmtpSender(),
        policy=RetryPolicy(
            max_attempts=config.delivery.max_attempts,
            retry_delay=config.delivery.retry_delay,
            max_retry_delay=config.delivery.max_retry_delay,
            delivery_timeout=config.delivery.timeout,
            lease_seconds=config.delivery.lease_seconds,
            poll_interval=config.delivery.poll_interval,
            concurrency=config.delivery.concurrency,
            retention_seconds=config.delivery.retention_seconds,
        ),
    )
    tls_context = None
    if config.smtp.tls.cert_file is not None and config.smtp.tls.key_file is not None:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(config.smtp.tls.cert_file, config.smtp.tls.key_file)
    listener = SmtpListener(
        handler=SubmissionHandler(
            service=messages, max_recipients=config.limits.max_recipients
        ),
        host=config.smtp.host,
        port=config.smtp.port,
        username=config.smtp.auth.username,
        password=config.smtp.auth.password.get_secret_value(),
        tls_context=tls_context,
        max_message_bytes=config.limits.max_message_bytes,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        task = None

        async def stop_worker() -> None:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        async with AsyncExitStack() as stack:
            stack.push_async_callback(database.close)
            stack.push_async_callback(stop_worker)
            stack.push_async_callback(listener.close)
            await relay.initialize()
            await listener.start()
            task = asyncio.create_task(worker.run())
            yield

    app = FastAPI(title="SMTP Gateway", lifespan=lifespan)
    app.add_middleware(_BodyLimit, max_bytes=65536)
    app.include_router(
        create_router(
            settings=relay,
            username=config.http.auth.username,
            password=config.http.auth.password.get_secret_value(),
        )
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        """探测数据库，不暴露内部配置。"""
        await database.check()
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 验证错误不能将请求中的明文密码回显给调用方。
        errors = [
            {"loc": item["loc"], "type": item["type"], "msg": item["msg"]}
            for item in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        logger.error("SMTP gateway database operation failed")
        return JSONResponse(
            status_code=503, content={"detail": "Gateway storage is unavailable"}
        )

    return app


__all__ = ["create_app"]
