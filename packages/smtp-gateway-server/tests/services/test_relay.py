import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from smtp_gateway.persistence import Database, RelayRepository
from smtp_gateway.services.relay import RelayService
from smtp_gateway.shared import DeliveryState
from smtp_gateway_client import SmtpSettingsWrite, TlsMode


@dataclass(kw_only=True)
class _Storage:
    relay: RelayService
    engine: AsyncEngine
    url: str
    key: str
    clock: list[float]


@pytest_asyncio.fixture
async def storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Storage]:
    clock = [1000.0]

    async def current_time(_repository: RelayRepository) -> float:
        return clock[0]

    monkeypatch.setattr(RelayRepository, "now", current_time)
    url = f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}"
    key = Fernet.generate_key().decode()
    engine = create_async_engine(url)
    relay = RelayService(database=Database(engine=engine), encryption_key=key)
    await relay.initialize()
    await relay.put_smtp(
        SmtpSettingsWrite(
            host="smtp.example.com",
            port=587,
            username="relay@example.com",
            password=SecretStr("upstream-secret"),
            tls=TlsMode.STARTTLS,
        )
    )
    try:
        yield _Storage(relay=relay, engine=engine, url=url, key=key, clock=clock)
    finally:
        await relay.close()


async def _enqueue(relay: RelayService, recipients: list[str] | None = None) -> str:
    return await relay.enqueue(
        sender="grafana@example.com",
        recipients=recipients or ["operator@example.com"],
        content=b"Subject: alert\r\n\r\nprivate notification body\r\n",
    )


@pytest.mark.asyncio
async def test_concurrent_initialization_of_empty_database(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty-gateway.db'}"
    key = Fernet.generate_key().decode()
    instances = [
        RelayService(
            database=Database(engine=create_async_engine(url)), encryption_key=key
        )
        for _ in range(8)
    ]
    try:
        await asyncio.gather(*(database.initialize() for database in instances))
        settings = await asyncio.gather(
            *(database.get_smtp() for database in instances)
        )
        assert len(settings) == 8
        assert all(not item.configured for item in settings)
        await instances[0].put_smtp(
            SmtpSettingsWrite(host="smtp.example.com", port=587)
        )
        assert all(
            item.configured
            for item in await asyncio.gather(
                *(database.get_smtp() for database in instances)
            )
        )
    finally:
        await asyncio.gather(*(database.close() for database in instances))


@pytest.mark.asyncio
async def test_queue_and_settings_survive_database_restart(storage: _Storage) -> None:
    identifier = await _enqueue(storage.relay)
    await storage.relay.close()
    restored = RelayService(
        database=Database(engine=create_async_engine(storage.url)),
        encryption_key=storage.key,
    )
    try:
        await restored.initialize()
        settings = await restored.get_smtp()
        assert settings.configured
        assert settings.host == "smtp.example.com"
        assert settings.has_password
        upstream = await restored.upstream()
        assert upstream is not None
        assert upstream.password is not None
        assert upstream.password.get_secret_value() == "upstream-secret"
        delivery = await restored.claim(max_attempts=3, lease_seconds=10)
        assert delivery is not None
        assert delivery.message_id == identifier
        assert delivery.recipient == "operator@example.com"
        assert delivery.content.endswith(b"private notification body\r\n")
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_password_and_content_are_encrypted_and_hidden(storage: _Storage) -> None:
    identifier = await _enqueue(storage.relay)
    async with storage.engine.connect() as connection:
        password = await connection.scalar(text("SELECT secret FROM smtp_settings"))
        content = await connection.scalar(text("SELECT content FROM smtp_messages"))
    assert isinstance(password, bytes)
    assert isinstance(content, bytes)
    assert b"upstream-secret" not in password
    assert b"private notification body" not in content
    cipher = Fernet(storage.key.encode())
    assert cipher.decrypt(password) == b"upstream-secret"
    assert b"private notification body" in cipher.decrypt(content)
    settings = (await storage.relay.get_smtp()).model_dump_json()
    status = await storage.relay.status(identifier)
    assert status is not None
    assert "password" not in settings.replace("has_password", "")
    assert "upstream-secret" not in settings
    assert "content" not in status.model_dump()
    assert "private notification body" not in status.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_password", [False, True])
async def test_wrong_encryption_key_fails_during_startup(
    storage: _Storage, with_password: bool
) -> None:
    await _enqueue(storage.relay)
    if not with_password:
        await storage.relay.put_smtp(
            SmtpSettingsWrite(host="smtp.example.com", port=587)
        )
    other = RelayService(
        database=Database(engine=create_async_engine(storage.url)),
        encryption_key=Fernet.generate_key().decode(),
    )
    try:
        with pytest.raises(RuntimeError, match="cannot decrypt stored data"):
            await other.initialize()
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_concurrent_claims_across_database_instances_have_one_owner(
    storage: _Storage,
) -> None:
    identifier = await _enqueue(storage.relay)
    other = RelayService(
        database=Database(engine=create_async_engine(storage.url)),
        encryption_key=storage.key,
    )
    try:
        await other.initialize()
        claims = await asyncio.gather(
            storage.relay.claim(max_attempts=3, lease_seconds=10),
            other.claim(max_attempts=3, lease_seconds=10),
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        assert winners[0].message_id == identifier
        status = await storage.relay.status(identifier)
        assert status is not None
        assert status.deliveries[0].attempts == 1
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_expired_claim_recovers_and_fences_old_owner(storage: _Storage) -> None:
    identifier = await _enqueue(storage.relay)
    first = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert first is not None
    assert await storage.relay.claim(max_attempts=3, lease_seconds=10) is None
    storage.clock[0] += 10
    second = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert second is not None
    assert second.id == first.id
    assert second.token != first.token
    assert second.attempts == 2

    await storage.relay.finish(first, error=None, max_attempts=3, retry_delay=0)
    status = await storage.relay.status(identifier)
    assert status is not None
    assert status.deliveries[0].state == DeliveryState.SENDING
    assert status.deliveries[0].attempts == 2

    await storage.relay.finish(second, error=None, max_attempts=3, retry_delay=0)
    await storage.relay.finish(
        first, error="stale failure", max_attempts=3, retry_delay=0
    )
    status = await storage.relay.status(identifier)
    assert status is not None
    assert status.deliveries[0].state == DeliveryState.DELIVERED
    assert status.deliveries[0].last_error is None


@pytest.mark.asyncio
async def test_crash_during_final_attempt_becomes_terminal(storage: _Storage) -> None:
    identifier = await _enqueue(storage.relay)
    claim = await storage.relay.claim(max_attempts=1, lease_seconds=10)
    assert claim is not None
    storage.clock[0] += 10
    assert await storage.relay.claim(max_attempts=1, lease_seconds=10) is None
    status = await storage.relay.status(identifier)
    assert status is not None
    assert status.deliveries[0].state == DeliveryState.FAILED
    assert status.deliveries[0].attempts == 1
    assert status.deliveries[0].last_error is not None
    await storage.relay.prune(retention_seconds=5)
    assert await storage.relay.status(identifier) is not None
    storage.clock[0] += 6
    await storage.relay.prune(retention_seconds=5)
    assert await storage.relay.status(identifier) is None


@pytest.mark.asyncio
async def test_lowering_attempt_limit_terminates_already_exhausted_pending_delivery(
    storage: _Storage,
) -> None:
    identifier = await _enqueue(storage.relay)
    first = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert first is not None
    await storage.relay.finish(
        first, error="SMTPException", max_attempts=3, retry_delay=100
    )
    assert await storage.relay.claim(max_attempts=1, lease_seconds=10) is None
    status = await storage.relay.status(identifier)
    assert status is not None
    assert status.deliveries[0].state == DeliveryState.FAILED
    assert status.deliveries[0].attempts == 1
    storage.clock[0] += 11
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is None


@pytest.mark.asyncio
async def test_successful_recipients_are_not_reclaimed(storage: _Storage) -> None:
    identifier = await _enqueue(
        storage.relay,
        ["first@example.com", "second@example.com", "first@example.com"],
    )
    first = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert first is not None
    await storage.relay.finish(first, error=None, max_attempts=3, retry_delay=0)
    second = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert second is not None
    assert second.recipient != first.recipient
    await storage.relay.finish(
        second, error="SMTPException", max_attempts=3, retry_delay=5
    )
    assert await storage.relay.claim(max_attempts=3, lease_seconds=10) is None
    storage.clock[0] += 5
    retry = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert retry is not None
    assert retry.recipient == second.recipient
    assert retry.attempts == 2
    status = await storage.relay.status(identifier)
    assert status is not None
    assert len(status.deliveries) == 2


@pytest.mark.asyncio
async def test_retention_preserves_pending_and_active_messages(
    storage: _Storage,
) -> None:
    pending = await _enqueue(storage.relay)
    sending = await _enqueue(storage.relay)
    finished = await _enqueue(storage.relay)
    claims = [
        await storage.relay.claim(max_attempts=3, lease_seconds=10) for _ in range(3)
    ]
    by_message = {claim.message_id: claim for claim in claims if claim is not None}
    await storage.relay.finish(
        by_message[pending], error="SMTPException", max_attempts=3, retry_delay=100
    )
    await storage.relay.finish(
        by_message[finished], error=None, max_attempts=3, retry_delay=0
    )
    storage.clock[0] += 20
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(pending) is not None
    assert await storage.relay.status(sending) is not None
    assert await storage.relay.status(finished) is None


@pytest.mark.asyncio
async def test_retention_waits_for_every_recipient_to_finish(storage: _Storage) -> None:
    identifier = await _enqueue(
        storage.relay, ["first@example.com", "second@example.com"]
    )
    first = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert first is not None
    await storage.relay.finish(first, error=None, max_attempts=3, retry_delay=0)
    storage.clock[0] += 20
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is not None
    second = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert second is not None
    await storage.relay.finish(second, error=None, max_attempts=3, retry_delay=0)
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is not None
    storage.clock[0] += 11
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is None
