"""接收调用方 session，执行配置与队列的原子 SQL。"""

from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtp_gateway.shared import DeliveryState

from .models import DeliveryRow, MessageRow, SmtpSettingsRow


class RelayRepository:
    """数据库查询不创建事务，也不接触明文密码或邮件正文。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check(self) -> None:
        """检查当前 session 的数据库连接。"""
        await self._session.execute(select(1))

    async def settings(self) -> SmtpSettingsRow | None:
        """读取单例上游设置。"""
        return await self._session.get(SmtpSettingsRow, 1)

    async def ensure_settings(self) -> None:
        """为空数据库添加单例行，由调用方提交事务。"""
        if await self.settings() is None:
            self._session.add(SmtpSettingsRow(id=1))

    async def update_settings(
        self,
        *,
        version: int,
        host: str,
        port: int,
        username: str,
        secret: bytes | None,
        tls: str,
    ) -> bool:
        """仅更新仍处于预期版本的上游设置。"""
        changed = await self._session.scalar(
            update(SmtpSettingsRow)
            .where(SmtpSettingsRow.id == 1, SmtpSettingsRow.version == version)
            .values(
                version=version + 1,
                host=host,
                port=port,
                username=username,
                secret=secret,
                tls=tls,
            )
            .returning(SmtpSettingsRow.id)
        )
        return changed is not None

    async def sample_content(self) -> bytes | None:
        """读取一份密文供启动时验证密钥。"""
        return await self._session.scalar(select(MessageRow.content).limit(1))

    async def now(self) -> float:
        """读取数据库时间，让多个副本采用同一时钟。"""
        value = await self._session.scalar(
            select(func.extract("epoch", func.current_timestamp()))
        )
        if value is None:
            raise RuntimeError("Database did not return its current time")
        return float(value)

    async def add_message(
        self,
        *,
        identifier: str,
        sender: str,
        recipients: list[str],
        content: bytes,
        now: float,
    ) -> None:
        """插入密文邮件和收件人行，原子性由调用方事务保证。"""
        self._session.add(
            MessageRow(id=identifier, sender=sender, content=content, created_at=now)
        )
        await self._session.flush()
        self._session.add_all(
            DeliveryRow(
                id=str(uuid4()),
                message_id=identifier,
                recipient=recipient,
                next_attempt_at=now,
            )
            for recipient in recipients
        )

    async def message(self, identifier: str) -> MessageRow | None:
        """按标识读取密文邮件。"""
        return await self._session.get(MessageRow, identifier)

    async def deliveries(self, identifier: str) -> list[DeliveryRow]:
        """读取一封邮件的所有收件人投递状态。"""
        rows = await self._session.scalars(
            select(DeliveryRow).where(DeliveryRow.message_id == identifier)
        )
        return list(rows)

    async def fail_exhausted(self, *, now: float, max_attempts: int) -> None:
        """终结已用尽次数的待处理或租约过期行。"""
        expired = and_(
            DeliveryRow.state == DeliveryState.SENDING,
            DeliveryRow.lease_until <= now,
        )
        await self._session.execute(
            update(DeliveryRow)
            .where(
                or_(expired, DeliveryRow.state == DeliveryState.PENDING),
                DeliveryRow.attempts >= max_attempts,
            )
            .values(
                state=DeliveryState.FAILED,
                claim_token=None,
                lease_until=None,
                finished_at=now,
                last_error="Maximum delivery attempts reached",
            )
        )

    async def claim(
        self,
        *,
        now: float,
        max_attempts: int,
        token: str,
        lease_until: float,
    ) -> DeliveryRow | None:
        """查询候选行，并在 CAS 更新中再次确认其资格。"""
        eligible = and_(
            DeliveryRow.attempts < max_attempts,
            or_(
                and_(
                    DeliveryRow.state == DeliveryState.PENDING,
                    DeliveryRow.next_attempt_at <= now,
                ),
                and_(
                    DeliveryRow.state == DeliveryState.SENDING,
                    DeliveryRow.lease_until <= now,
                ),
            ),
        )
        identifier = await self._session.scalar(
            select(DeliveryRow.id)
            .where(eligible)
            .order_by(DeliveryRow.next_attempt_at, DeliveryRow.id)
            .limit(1)
        )
        if identifier is None:
            return None
        return await self._session.scalar(
            update(DeliveryRow)
            .where(DeliveryRow.id == identifier, eligible)
            .values(
                state=DeliveryState.SENDING,
                attempts=DeliveryRow.attempts + 1,
                lease_until=lease_until,
                claim_token=token,
            )
            .returning(DeliveryRow)
        )

    async def finish(
        self,
        *,
        identifier: str,
        token: str,
        state: DeliveryState,
        error: str | None,
        next_attempt_at: float,
        finished_at: float | None,
    ) -> None:
        """通过认领 token 防止旧 worker 覆盖新结果。"""
        await self._session.execute(
            update(DeliveryRow)
            .where(DeliveryRow.id == identifier, DeliveryRow.claim_token == token)
            .values(
                state=state,
                last_error=error,
                next_attempt_at=next_attempt_at,
                lease_until=None,
                claim_token=None,
                finished_at=finished_at,
            )
        )

    async def prune_before(self, cutoff: float) -> None:
        """删除所有收件人均在截止时间前结束的邮件。"""
        unfinished = select(DeliveryRow.message_id).where(
            or_(DeliveryRow.finished_at.is_(None), DeliveryRow.finished_at > cutoff)
        )
        await self._session.execute(
            delete(MessageRow).where(MessageRow.id.not_in(unfinished))
        )


__all__ = ["RelayRepository"]
