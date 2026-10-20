"""只通过指定上游发送邮件，后台按收件人恢复和重试。"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, assert_never

import aiosmtplib

from .database import ClaimedDelivery, Database, Upstream
from .schemas import TlsMode

logger = logging.getLogger(__name__)


class _Sender(Protocol):
    async def send(self, upstream: Upstream, delivery: ClaimedDelivery) -> None: ...


class SmtpSender:
    """通过成熟客户端处理认证和三种上游 TLS 模式。"""

    @staticmethod
    async def send(upstream: Upstream, delivery: ClaimedDelivery) -> None:
        match upstream.tls:
            case TlsMode.NONE:
                use_tls, start_tls = False, False
            case TlsMode.STARTTLS:
                use_tls, start_tls = False, True
            case TlsMode.IMPLICIT:
                use_tls, start_tls = True, False
            case _ as unexpected:
                assert_never(unexpected)
        client = aiosmtplib.SMTP(
            hostname=upstream.host,
            port=upstream.port,
            use_tls=use_tls,
            start_tls=start_tls,
            validate_certs=True,
        )
        try:
            await client.connect()
            if upstream.username:
                if upstream.password is None:
                    raise RuntimeError("Authenticated SMTP upstream has no password")
                await client.login(
                    upstream.username, upstream.password.get_secret_value()
                )
            refused, _response = await client.sendmail(
                delivery.sender, [delivery.recipient], delivery.content
            )
            if refused:
                raise aiosmtplib.SMTPException("SMTP recipient was rejected")
        finally:
            # DATA 确认即表示上游已接收；关闭连接不能将已确认邮件变成重试。
            client.close()


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    max_attempts: int
    retry_delay: float
    max_retry_delay: float
    delivery_timeout: float
    lease_seconds: float
    poll_interval: float
    concurrency: int
    retention_seconds: float


class DeliveryWorker:
    """认领任务后在事务外投递，并以认领 token 记录结果。"""

    def __init__(
        self, *, database: Database, sender: _Sender, policy: RetryPolicy
    ) -> None:
        self._database = database
        self._sender = sender
        self._policy = policy

    async def run_once(self) -> bool:
        """处理一个收件人；没有到期任务时返回 False。"""
        upstream = await self._database.upstream()
        if upstream is None:
            return False
        delivery = await self._database.claim(
            max_attempts=self._policy.max_attempts,
            lease_seconds=self._policy.lease_seconds,
        )
        if delivery is None:
            return False
        error = None
        try:
            async with asyncio.timeout(self._policy.delivery_timeout):
                await self._sender.send(upstream, delivery)
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            # 上游响应文本可能包含认证信息或邮件内容，只保存异常类别。
            error = type(exc).__name__
            logger.warning(
                "SMTP delivery failed: delivery=%s attempt=%s reason=%s",
                delivery.id,
                delivery.attempts,
                error,
            )
        await self._database.finish(
            delivery,
            error=error,
            max_attempts=self._policy.max_attempts,
            retry_delay=min(
                self._policy.retry_delay * 2 ** (delivery.attempts - 1),
                self._policy.max_retry_delay,
            ),
        )
        return True

    async def _deliver_loop(self) -> None:
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("SMTP queue processing failed")
            await asyncio.sleep(self._policy.poll_interval)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await self._database.prune(self._policy.retention_seconds)
            except Exception:
                logger.exception("SMTP queue cleanup failed")
            await asyncio.sleep(60)

    async def run(self) -> None:
        """在独立服务中启动有限并发 worker 和保留期清理。"""
        async with asyncio.TaskGroup() as group:
            for _worker in range(self._policy.concurrency):
                group.create_task(self._deliver_loop())
            group.create_task(self._cleanup_loop())


__all__ = ["DeliveryWorker", "RetryPolicy", "SmtpSender"]
