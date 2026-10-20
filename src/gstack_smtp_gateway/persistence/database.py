"""数据库连接、session 工厂与并发安全的启动初始化。"""

from sqlalchemy import event, func, select
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from .models import GatewayBase
from .repository import RelayRepository


class Database:
    """管理数据库资源，不处理凭据、邮件加密和投递规则。"""

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        if engine.dialect.name == "sqlite":

            @event.listens_for(engine.sync_engine, "connect")
            def configure_sqlite(
                connection: DBAPIConnection, _record: ConnectionPoolEntry
            ) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    def session(self) -> AsyncSession:
        """创建由调用方关闭并控制事务的 session。"""
        return self._sessions()

    async def initialize(self) -> None:
        """串行创建网关自有表，并补齐单例设置行。"""
        async with self._engine.begin() as connection:
            if self._engine.dialect.name == "postgresql":
                await connection.execute(select(func.pg_advisory_xact_lock(717024846)))
            else:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(GatewayBase.metadata.create_all)
        try:
            async with self.session() as session, session.begin():
                await RelayRepository(session).ensure_settings()
        except IntegrityError:
            # 并发副本可以抢先提交单例行，其他约束错误仍需抛出。
            async with self.session() as session:
                if await RelayRepository(session).settings() is None:
                    raise

    async def check(self) -> None:
        """检查服务的数据库连接。"""
        async with self.session() as session:
            await RelayRepository(session).check()

    async def close(self) -> None:
        """关闭数据库连接池。"""
        await self._engine.dispose()


__all__ = ["Database"]
