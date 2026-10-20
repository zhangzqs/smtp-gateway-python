"""按邮件批量投递，并按收件人恢复和重试持久化任务。"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import aiosmtplib

from smtp_gateway.shared import ClaimedDelivery, Upstream

logger = logging.getLogger(__name__)


class _Sender(Protocol):
    async def send(
        self, upstream: Upstream, delivery: ClaimedDelivery
    ) -> dict[str, str]: ...


class _Relay(Protocol):
    async def upstream(self) -> Upstream | None: ...
    async def claim(
        self, *, max_attempts: int, lease_seconds: float
    ) -> ClaimedDelivery | None: ...
    async def finish(
        self,
        delivery: ClaimedDelivery,
        *,
        errors: dict[str, str],
        max_attempts: int,
        retry_delay: float,
        max_retry_delay: float,
    ) -> None: ...
    async def prune(self, retention_seconds: float) -> None: ...


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

    def __init__(self, *, relay: _Relay, sender: _Sender, policy: RetryPolicy) -> None:
        self._relay = relay
        self._sender = sender
        self._policy = policy

    async def run_once(self) -> bool:
        """批量处理一封邮件的到期收件人；没有任务时返回 False。"""
        upstream = await self._relay.upstream()
        if upstream is None:
            return False
        delivery = await self._relay.claim(
            max_attempts=self._policy.max_attempts,
            lease_seconds=self._policy.lease_seconds,
        )
        if delivery is None:
            return False
        try:
            async with asyncio.timeout(self._policy.delivery_timeout):
                errors = await self._sender.send(upstream, delivery)
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            # 上游响应文本可能包含认证信息或邮件内容，只保存异常类别。
            errors = {
                item.recipient: type(exc).__name__ for item in delivery.recipients
            }
            logger.warning(
                "SMTP delivery failed: message=%s recipients=%s reason=%s",
                delivery.message_id,
                len(delivery.recipients),
                type(exc).__name__,
            )
        await self._relay.finish(
            delivery,
            errors=errors,
            max_attempts=self._policy.max_attempts,
            retry_delay=self._policy.retry_delay,
            max_retry_delay=self._policy.max_retry_delay,
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
                await self._relay.prune(self._policy.retention_seconds)
            except Exception:
                logger.exception("SMTP queue cleanup failed")
            await asyncio.sleep(60)

    async def run(self) -> None:
        """在独立服务中启动有限并发 worker 和保留期清理。"""
        async with asyncio.TaskGroup() as group:
            for _worker in range(self._policy.concurrency):
                group.create_task(self._deliver_loop())
            group.create_task(self._cleanup_loop())


__all__ = ["DeliveryWorker", "RetryPolicy"]
