"""网关独立的配置与按收件人持久化队列。"""

from dataclasses import dataclass, field
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    and_,
    delete,
    event,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import ConnectionPoolEntry

from .schemas import (
    DeliveryState,
    DeliveryStatus,
    MessageStatus,
    SmtpSettings,
    SmtpSettingsWrite,
    TlsMode,
)


class GatewayBase(DeclarativeBase):
    """网关表独立于 GPUStack 和 GStack 插件数据库。"""


class _SmtpRow(GatewayBase):
    __tablename__ = "gstack_smtp_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=0)
    host: Mapped[str | None] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(default=587)
    username: Mapped[str] = mapped_column(String(320), default="")
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    tls: Mapped[str] = mapped_column(String(16), default=TlsMode.STARTTLS)


class _MessageRow(GatewayBase):
    __tablename__ = "gstack_smtp_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sender: Mapped[str] = mapped_column(String(320))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[float] = mapped_column(Float)


class _DeliveryRow(GatewayBase):
    __tablename__ = "gstack_smtp_deliveries"
    __table_args__ = (Index("ix_gstack_smtp_due", "state", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("gstack_smtp_messages.id", ondelete="CASCADE"), index=True
    )
    recipient: Mapped[str] = mapped_column(String(320))
    state: Mapped[str] = mapped_column(String(16), default=DeliveryState.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float | None] = mapped_column(Float)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[float | None] = mapped_column(Float)


@dataclass(frozen=True, slots=True, kw_only=True)
class Upstream:
    host: str
    port: int
    username: str
    password: SecretStr | None = field(repr=False)
    tls: TlsMode


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedDelivery:
    id: str
    message_id: str
    token: str
    sender: str
    recipient: str
    content: bytes = field(repr=False)
    attempts: int


class NotConfiguredError(Exception):
    """尚未配置上游，不能确认接收邮件。"""


class Database:
    """在短事务中保存配置、提交邮件以及认领和完成投递任务。"""

    def __init__(self, *, engine: AsyncEngine, encryption_key: str) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._cipher = Fernet(encryption_key.encode("ascii"))
        if engine.dialect.name == "sqlite":

            @event.listens_for(engine.sync_engine, "connect")
            def configure_sqlite(
                connection: DBAPIConnection, _record: ConnectionPoolEntry
            ) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    async def initialize(self) -> None:
        """创建自有表，并尽早检查持久化密钥能否解密已有数据。"""
        async with self._engine.begin() as connection:
            if self._engine.dialect.name == "postgresql":
                await connection.execute(select(func.pg_advisory_xact_lock(717024846)))
            else:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(GatewayBase.metadata.create_all)
        try:
            async with self._sessions.begin() as session:
                if await session.get(_SmtpRow, 1) is None:
                    session.add(_SmtpRow(id=1))
        except IntegrityError:
            # 其他实例可能已提交相同的单例设置行。
            async with self._sessions() as session:
                if await session.get(_SmtpRow, 1) is None:
                    raise
        async with self._sessions() as session:
            await self._upstream(session)
            sample = await session.scalar(select(_MessageRow.content).limit(1))
            if sample is not None:
                self._decrypt(sample)

    async def close(self) -> None:
        """关闭数据库连接池。"""
        await self._engine.dispose()

    async def check(self) -> None:
        """检查服务的数据库连接。"""
        async with self._sessions() as session:
            await session.execute(select(1))

    def _decrypt(self, value: bytes) -> bytes:
        try:
            return self._cipher.decrypt(value)
        except InvalidToken as exc:
            raise RuntimeError(
                "Gateway encryption key cannot decrypt stored data"
            ) from exc

    async def _upstream(self, session: AsyncSession) -> Upstream | None:
        row = await session.get(_SmtpRow, 1)
        if row is None or row.host is None:
            return None
        return Upstream(
            host=row.host,
            port=row.port,
            username=row.username,
            password=SecretStr(self._decrypt(row.secret).decode())
            if row.secret is not None
            else None,
            tls=TlsMode(row.tls),
        )

    async def upstream(self) -> Upstream | None:
        """读取投递使用的真实上游；仅服务内部消费密码。"""
        async with self._sessions() as session:
            return await self._upstream(session)

    async def get_smtp(self) -> SmtpSettings:
        """返回可用于展示的设置，隐藏已保存密码。"""
        upstream = await self.upstream()
        if upstream is None:
            return SmtpSettings(configured=False)
        return SmtpSettings(
            configured=True,
            host=upstream.host,
            port=upstream.port,
            username=upstream.username,
            tls=upstream.tls,
            has_password=upstream.password is not None,
        )

    async def put_smtp(self, body: SmtpSettingsWrite) -> SmtpSettings:
        """以版本条件更新设置，保留省略的密码并防止并发覆盖。"""
        for _attempt in range(5):
            async with self._sessions.begin() as session:
                row = await session.get(_SmtpRow, 1)
                if row is None:
                    raise RuntimeError("SMTP settings have not been initialized")
                secret = row.secret
                if body.username:
                    if body.password is not None:
                        password = body.password.get_secret_value()
                        if not password:
                            raise ValueError(
                                "SMTP password must not be empty with a username"
                            )
                        secret = self._cipher.encrypt(password.encode())
                    elif body.username != row.username or secret is None:
                        raise ValueError("SMTP password is required for this username")
                else:
                    secret = None
                changed = await session.scalar(
                    update(_SmtpRow)
                    .where(_SmtpRow.id == 1, _SmtpRow.version == row.version)
                    .values(
                        version=row.version + 1,
                        host=body.host,
                        port=body.port,
                        username=body.username,
                        secret=secret,
                        tls=body.tls,
                    )
                    .returning(_SmtpRow.id)
                )
            if changed is not None:
                return await self.get_smtp()
        raise RuntimeError("SMTP settings changed concurrently; retry the update")

    @staticmethod
    async def _now(session: AsyncSession) -> float:
        value = await session.scalar(
            select(func.extract("epoch", func.current_timestamp()))
        )
        if value is None:
            raise RuntimeError("Database did not return its current time")
        return float(value)

    async def enqueue(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str:
        """原子提交正文与所有收件人任务；返回时事务已持久化。"""
        identifier = str(uuid4())
        async with self._sessions.begin() as session:
            if await self._upstream(session) is None:
                raise NotConfiguredError("Upstream SMTP is not configured")
            now = await self._now(session)
            session.add(
                _MessageRow(
                    id=identifier,
                    sender=sender,
                    content=self._cipher.encrypt(content),
                    created_at=now,
                )
            )
            await session.flush()
            session.add_all(
                _DeliveryRow(
                    id=str(uuid4()),
                    message_id=identifier,
                    recipient=recipient,
                    next_attempt_at=now,
                )
                for recipient in dict.fromkeys(recipients)
            )
        return identifier

    async def status(self, identifier: str) -> MessageStatus | None:
        """返回收件人状态，不返回邮件正文。"""
        async with self._sessions() as session:
            if await session.get(_MessageRow, identifier) is None:
                return None
            rows = await session.scalars(
                select(_DeliveryRow).where(_DeliveryRow.message_id == identifier)
            )
            return MessageStatus(
                id=identifier,
                deliveries=[
                    DeliveryStatus(
                        recipient=row.recipient,
                        state=DeliveryState(row.state),
                        attempts=row.attempts,
                        last_error=row.last_error,
                    )
                    for row in rows
                ],
            )

    async def claim(
        self, *, max_attempts: int, lease_seconds: float
    ) -> ClaimedDelivery | None:
        """以 CAS 抢占到期任务；崩溃中的尝试也计入最大次数。"""
        async with self._sessions.begin() as session:
            now = await self._now(session)
            expired = and_(
                _DeliveryRow.state == DeliveryState.SENDING,
                _DeliveryRow.lease_until <= now,
            )
            await session.execute(
                update(_DeliveryRow)
                .where(
                    or_(expired, _DeliveryRow.state == DeliveryState.PENDING),
                    _DeliveryRow.attempts >= max_attempts,
                )
                .values(
                    state=DeliveryState.FAILED,
                    claim_token=None,
                    lease_until=None,
                    finished_at=now,
                    last_error="Maximum delivery attempts reached",
                )
            )
            eligible = and_(
                _DeliveryRow.attempts < max_attempts,
                or_(
                    and_(
                        _DeliveryRow.state == DeliveryState.PENDING,
                        _DeliveryRow.next_attempt_at <= now,
                    ),
                    expired,
                ),
            )
            identifier = await session.scalar(
                select(_DeliveryRow.id)
                .where(eligible)
                .order_by(_DeliveryRow.next_attempt_at, _DeliveryRow.id)
                .limit(1)
            )
            if identifier is None:
                return None
            token = str(uuid4())
            row = await session.scalar(
                update(_DeliveryRow)
                .where(_DeliveryRow.id == identifier, eligible)
                .values(
                    state=DeliveryState.SENDING,
                    attempts=_DeliveryRow.attempts + 1,
                    lease_until=now + lease_seconds,
                    claim_token=token,
                )
                .returning(_DeliveryRow)
            )
            if row is None:
                return None
            message = await session.get(_MessageRow, row.message_id)
            if message is None:
                raise RuntimeError("Queued delivery has no message")
            return ClaimedDelivery(
                id=row.id,
                message_id=message.id,
                token=token,
                sender=message.sender,
                recipient=row.recipient,
                content=self._decrypt(message.content),
                attempts=row.attempts,
            )

    async def finish(
        self,
        delivery: ClaimedDelivery,
        *,
        error: str | None,
        max_attempts: int,
        retry_delay: float,
    ) -> None:
        """仅当前认领者可记录结果，成功收件人不会再次排队。"""
        if error is None:
            state = DeliveryState.DELIVERED
        elif delivery.attempts >= max_attempts:
            state = DeliveryState.FAILED
        else:
            state = DeliveryState.PENDING
        async with self._sessions.begin() as session:
            now = await self._now(session)
            await session.execute(
                update(_DeliveryRow)
                .where(
                    _DeliveryRow.id == delivery.id,
                    _DeliveryRow.claim_token == delivery.token,
                )
                .values(
                    state=state,
                    last_error=error,
                    next_attempt_at=now + retry_delay,
                    lease_until=None,
                    claim_token=None,
                    finished_at=now if state != DeliveryState.PENDING else None,
                )
            )

    async def prune(self, retention_seconds: float) -> None:
        """清理所有收件人均已终止且超过保留期的邮件。"""
        async with self._sessions.begin() as session:
            now = await self._now(session)
            unfinished = select(_DeliveryRow.message_id).where(
                or_(
                    _DeliveryRow.finished_at.is_(None),
                    _DeliveryRow.finished_at > now - retention_seconds,
                )
            )
            await session.execute(
                delete(_MessageRow).where(_MessageRow.id.not_in(unfinished))
            )


__all__ = ["ClaimedDelivery", "Database", "NotConfiguredError", "Upstream"]
