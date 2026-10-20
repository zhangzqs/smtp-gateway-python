"""管理上游凭据和持久队列用例的事务边界。"""

from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from smtp_gateway.persistence import Database, RelayRepository
from smtp_gateway.shared import (
    ClaimedDelivery,
    DeliveryState,
    DeliveryStatus,
    MessageStatus,
    NotConfiguredError,
    Upstream,
)
from smtp_gateway_client import SmtpSettings, SmtpSettingsWrite, TlsMode


class RelayService:
    """负责加密、凭据校验与邮件投递状态的业务编排。"""

    def __init__(self, *, database: Database, encryption_key: str) -> None:
        self._database = database
        self._cipher = Fernet(encryption_key.encode("ascii"))

    async def initialize(self) -> None:
        """初始化持久层，并在接收邮件前验证已保存数据的密钥。"""
        await self._database.initialize()
        async with self._database.session() as session:
            repository = RelayRepository(session)
            await self._upstream(repository)
            sample = await repository.sample_content()
            if sample is not None:
                self._decrypt(sample)

    async def close(self) -> None:
        """关闭持久层资源。"""
        await self._database.close()

    async def check(self) -> None:
        """检查持久层连接。"""
        await self._database.check()

    def _decrypt(self, value: bytes) -> bytes:
        try:
            return self._cipher.decrypt(value)
        except InvalidToken as exc:
            raise RuntimeError(
                "Gateway encryption key cannot decrypt stored data"
            ) from exc

    async def _upstream(self, repository: RelayRepository) -> Upstream | None:
        row = await repository.settings()
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
        """读取投递使用的真实上游；仅内部调用方消费密码。"""
        async with self._database.session() as session:
            return await self._upstream(RelayRepository(session))

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
        """保留省略的密码，并通过版本条件防止并发覆盖。"""
        for _attempt in range(5):
            async with self._database.session() as session, session.begin():
                repository = RelayRepository(session)
                row = await repository.settings()
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
                changed = await repository.update_settings(
                    version=row.version,
                    host=body.host,
                    port=body.port,
                    username=body.username,
                    secret=secret,
                    tls=body.tls,
                )
            if changed:
                return await self.get_smtp()
        raise RuntimeError("SMTP settings changed concurrently; retry the update")

    async def enqueue(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str:
        """在同一事务中保存密文正文与所有收件人任务。"""
        identifier = str(uuid4())
        async with self._database.session() as session, session.begin():
            repository = RelayRepository(session)
            if await self._upstream(repository) is None:
                raise NotConfiguredError("Upstream SMTP is not configured")
            await repository.add_message(
                identifier=identifier,
                sender=sender,
                recipients=list(dict.fromkeys(recipients)),
                content=self._cipher.encrypt(content),
                now=await repository.now(),
            )
        return identifier

    async def status(self, identifier: str) -> MessageStatus | None:
        """读取收件人状态，不返回邮件正文。"""
        async with self._database.session() as session:
            repository = RelayRepository(session)
            if await repository.message(identifier) is None:
                return None
            rows = await repository.deliveries(identifier)
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
        """认领到期任务；崩溃中的尝试也计入最大次数。"""
        async with self._database.session() as session, session.begin():
            repository = RelayRepository(session)
            now = await repository.now()
            await repository.fail_exhausted(now=now, max_attempts=max_attempts)
            token = str(uuid4())
            row = await repository.claim(
                now=now,
                max_attempts=max_attempts,
                token=token,
                lease_until=now + lease_seconds,
            )
            if row is None:
                return None
            message = await repository.message(row.message_id)
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
        async with self._database.session() as session, session.begin():
            repository = RelayRepository(session)
            now = await repository.now()
            await repository.finish(
                identifier=delivery.id,
                token=delivery.token,
                state=state,
                error=error,
                next_attempt_at=now + retry_delay,
                finished_at=now if state != DeliveryState.PENDING else None,
            )

    async def prune(self, retention_seconds: float) -> None:
        """清理所有收件人均已结束且超过保留期的邮件。"""
        async with self._database.session() as session, session.begin():
            repository = RelayRepository(session)
            await repository.prune_before(await repository.now() - retention_seconds)


__all__ = ["RelayService"]
