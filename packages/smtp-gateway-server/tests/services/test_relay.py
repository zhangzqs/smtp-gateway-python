import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
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
        assert [item.recipient for item in delivery.recipients] == [
            "operator@example.com"
        ]
        assert delivery.content.endswith(b"private notification body\r\n")
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_password_and_content_are_encrypted_and_hidden(storage: _Storage) -> None:
    identifier = await _enqueue(storage.relay)
    async with storage.engine.connect() as connection:
        password = await connection.scalar(text("SELECT secret FROM smtp_settings"))
        content = await connection.scalar(text("SELECT content FROM smtp_messages"))
        subject = await connection.scalar(text("SELECT subject FROM smtp_messages"))
    assert isinstance(password, bytes)
    assert isinstance(content, bytes)
    assert isinstance(subject, bytes)
    assert b"upstream-secret" not in password
    assert b"private notification body" not in content
    cipher = Fernet(storage.key.encode())
    assert cipher.decrypt(password) == b"upstream-secret"
    assert b"private notification body" in cipher.decrypt(content)
    assert b"alert" not in subject
    assert cipher.decrypt(subject) == b"alert"
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
    identifier = await _enqueue(
        storage.relay, ["first@example.com", "second@example.com"]
    )
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
        assert {item.recipient for item in winners[0].recipients} == {
            "first@example.com",
            "second@example.com",
        }
        status = await storage.relay.status(identifier)
        assert status is not None
        assert len(status.deliveries) == 2
        assert all(item.attempts == 1 for item in status.deliveries)
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_expired_claim_recovers_and_fences_old_owner(storage: _Storage) -> None:
    identifier = await _enqueue(
        storage.relay, ["first@example.com", "second@example.com"]
    )
    first = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert first is not None
    assert await storage.relay.claim(max_attempts=3, lease_seconds=10) is None
    storage.clock[0] += 10
    second = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert second is not None
    assert {item.id for item in second.recipients} == {
        item.id for item in first.recipients
    }
    assert second.token != first.token
    assert all(item.attempts == 2 for item in second.recipients)

    await storage.relay.finish(
        first, errors={}, max_attempts=3, retry_delay=0, max_retry_delay=0
    )
    status = await storage.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.SENDING for item in status.deliveries)
    assert all(item.attempts == 2 for item in status.deliveries)

    await storage.relay.finish(
        second, errors={}, max_attempts=3, retry_delay=0, max_retry_delay=0
    )
    await storage.relay.finish(
        first,
        errors={item.recipient: "stale failure" for item in first.recipients},
        max_attempts=3,
        retry_delay=0,
        max_retry_delay=0,
    )
    status = await storage.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.DELIVERED for item in status.deliveries)
    assert all(item.last_error is None for item in status.deliveries)


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
        first,
        errors={"operator@example.com": "SMTPException"},
        max_attempts=3,
        retry_delay=100,
        max_retry_delay=100,
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
    assert {item.recipient for item in first.recipients} == {
        "first@example.com",
        "second@example.com",
    }
    assert len(first.recipients) == 2
    await storage.relay.finish(
        first,
        errors={"second@example.com": "SMTPException"},
        max_attempts=3,
        retry_delay=5,
        max_retry_delay=5,
    )
    assert await storage.relay.claim(max_attempts=3, lease_seconds=10) is None
    storage.clock[0] += 5
    retry = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert retry is not None
    assert len(retry.recipients) == 1
    assert retry.recipients[0].recipient == "second@example.com"
    assert retry.recipients[0].attempts == 2
    status = await storage.relay.status(identifier)
    assert status is not None
    assert len(status.deliveries) == 2


@pytest.mark.asyncio
async def test_active_batch_blocks_newly_due_recipients_but_not_other_messages(
    storage: _Storage,
) -> None:
    identifier = await _enqueue(
        storage.relay, ["first@example.com", "later@example.com"]
    )
    async with storage.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE smtp_deliveries SET next_attempt_at = 1010 "
                "WHERE message_id = :identifier AND recipient = :recipient"
            ),
            {"identifier": identifier, "recipient": "later@example.com"},
        )
    first = await storage.relay.claim(max_attempts=3, lease_seconds=30)
    assert first is not None
    assert [item.recipient for item in first.recipients] == ["first@example.com"]

    storage.clock[0] = 1011
    other_identifier = await _enqueue(storage.relay, ["other@example.com"])
    other = await storage.relay.claim(max_attempts=3, lease_seconds=30)
    assert other is not None
    assert other.message_id == other_identifier
    assert await storage.relay.claim(max_attempts=3, lease_seconds=30) is None

    await storage.relay.finish(
        first, errors={}, max_attempts=3, retry_delay=0, max_retry_delay=0
    )
    later = await storage.relay.claim(max_attempts=3, lease_seconds=30)
    assert later is not None
    assert later.message_id == identifier
    assert [item.recipient for item in later.recipients] == ["later@example.com"]
    assert later.recipients[0].attempts == 1


@pytest.mark.asyncio
async def test_batch_uses_each_recipients_attempt_limit_and_retry_delay(
    storage: _Storage,
) -> None:
    identifier = await _enqueue(
        storage.relay,
        ["retry@example.com", "exhausted@example.com", "success@example.com"],
    )
    async with storage.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE smtp_deliveries SET attempts = :attempts "
                "WHERE message_id = :identifier AND recipient = :recipient"
            ),
            [
                {
                    "identifier": identifier,
                    "recipient": "retry@example.com",
                    "attempts": 1,
                },
                {
                    "identifier": identifier,
                    "recipient": "exhausted@example.com",
                    "attempts": 2,
                },
            ],
        )
    batch = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert batch is not None
    assert {item.recipient: item.attempts for item in batch.recipients} == {
        "retry@example.com": 2,
        "exhausted@example.com": 3,
        "success@example.com": 1,
    }
    await storage.relay.finish(
        batch,
        errors={
            "retry@example.com": "SMTPRecipientRefused:450",
            "exhausted@example.com": "SMTPRecipientRefused:450",
        },
        max_attempts=3,
        retry_delay=10,
        max_retry_delay=15,
    )
    status = await storage.relay.status(identifier)
    assert status is not None
    assert {item.recipient: item.state for item in status.deliveries} == {
        "retry@example.com": DeliveryState.PENDING,
        "exhausted@example.com": DeliveryState.FAILED,
        "success@example.com": DeliveryState.DELIVERED,
    }
    storage.clock[0] += 14
    assert await storage.relay.claim(max_attempts=3, lease_seconds=10) is None
    storage.clock[0] += 1
    retry = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert retry is not None
    assert len(retry.recipients) == 1
    assert retry.recipients[0].recipient == "retry@example.com"
    assert retry.recipients[0].attempts == 3


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
        by_message[pending],
        errors={"operator@example.com": "SMTPException"},
        max_attempts=3,
        retry_delay=100,
        max_retry_delay=100,
    )
    await storage.relay.finish(
        by_message[finished],
        errors={},
        max_attempts=3,
        retry_delay=0,
        max_retry_delay=0,
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
    await storage.relay.finish(
        first,
        errors={"second@example.com": "SMTPException"},
        max_attempts=3,
        retry_delay=0,
        max_retry_delay=0,
    )
    storage.clock[0] += 20
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is not None
    second = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert second is not None
    assert [item.recipient for item in second.recipients] == ["second@example.com"]
    await storage.relay.finish(
        second, errors={}, max_attempts=3, retry_delay=0, max_retry_delay=0
    )
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is not None
    storage.clock[0] += 11
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.status(identifier) is None


@pytest.mark.asyncio
async def test_message_pages_have_stable_order_and_nested_deliveries(
    storage: _Storage,
) -> None:
    identifiers = [
        await _enqueue(storage.relay, [f"receiver-{index}@example.com"])
        for index in range(4)
    ]
    storage.clock[0] += 1
    newest = await _enqueue(
        storage.relay, ["second@example.com", "first@example.com", "first@example.com"]
    )
    pages = [
        await storage.relay.list_messages(page=index, page_size=2)
        for index in range(1, 5)
    ]
    assert [record.id for page in pages for record in page.items] == [
        newest,
        *sorted(identifiers, reverse=True),
    ]
    assert [len(page.items) for page in pages] == [2, 2, 1, 0]
    assert all(page.total == 5 and page.page_size == 2 for page in pages)
    record = pages[0].items[0]
    assert record.sender == "grafana@example.com"
    assert record.subject == "alert"
    assert record.created_at == datetime.fromtimestamp(storage.clock[0], UTC)
    assert [item.recipient for item in record.deliveries] == [
        "first@example.com",
        "second@example.com",
    ]
    assert all(item.state == DeliveryState.PENDING for item in record.deliveries)
    assert all(item.attempts == 0 for item in record.deliveries)


@pytest.mark.asyncio
async def test_message_records_show_partial_results_without_loading_content(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = await _enqueue(
        storage.relay, ["first@example.com", "second@example.com"]
    )
    batch = await storage.relay.claim(max_attempts=3, lease_seconds=10)
    assert batch is not None
    await storage.relay.finish(
        batch,
        errors={"second@example.com": "SMTPRecipientRefused"},
        max_attempts=3,
        retry_delay=5,
        max_retry_delay=10,
    )

    async with storage.engine.connect() as connection:
        body = await connection.scalar(text("SELECT content FROM smtp_messages"))
    cipher = Fernet(storage.key.encode())

    def reject_body_decryption(value: bytes) -> bytes:
        if value == body:
            raise AssertionError("Listing messages must not decrypt the body")
        return cipher.decrypt(value)

    monkeypatch.setattr(storage.relay, "_decrypt", reject_body_decryption)
    records = await storage.relay.list_messages()
    assert records.items[0].id == identifier
    assert records.items[0].subject == "alert"
    statuses = {item.recipient: item for item in records.items[0].deliveries}
    assert statuses["first@example.com"].state == DeliveryState.DELIVERED
    assert statuses["first@example.com"].last_error is None
    assert statuses["second@example.com"].state == DeliveryState.PENDING
    assert statuses["second@example.com"].last_error == "SMTPRecipientRefused"
    assert all(item.attempts == 1 for item in statuses.values())
    response = records.model_dump_json()
    assert "private notification body" not in response
    assert "upstream-secret" not in response
    assert "content" not in response
    assert batch.token not in response
    assert all(item.id not in response for item in batch.recipients)


@pytest.mark.asyncio
async def test_content_is_decrypted_on_demand_and_removed_with_retained_message(
    storage: _Storage,
) -> None:
    identifier = await _enqueue(storage.relay)
    content = await storage.relay.get_message_content(identifier)
    assert content is not None
    assert content.id == identifier
    assert content.subject == "alert"
    assert content.text == "private notification body\r\n"
    assert content.html is None
    assert await storage.relay.get_message_content("missing") is None
    batch = await storage.relay.claim(max_attempts=1, lease_seconds=10)
    assert batch is not None
    await storage.relay.finish(
        batch, errors={}, max_attempts=1, retry_delay=0, max_retry_delay=0
    )
    storage.clock[0] += 20
    await storage.relay.prune(retention_seconds=10)
    assert await storage.relay.get_message_content(identifier) is None


@pytest.mark.parametrize("instances", [1, 4])
@pytest.mark.asyncio
async def test_existing_database_upgrades_and_backfills_encrypted_subjects(
    tmp_path: Path, instances: int
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    key = Fernet.generate_key().decode()
    cipher = Fernet(key.encode())
    content = b"Subject: =?utf-8?b?5rWL6K+V?=\r\n\r\nlegacy body\r\n"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE smtp_messages (id VARCHAR(36) PRIMARY KEY, "
                "sender VARCHAR(320) NOT NULL, content BLOB NOT NULL, "
                "created_at FLOAT NOT NULL)"
            )
        )
        await connection.execute(
            text("INSERT INTO smtp_messages VALUES (:id, :sender, :content, :created)"),
            [
                {
                    "id": f"legacy-{index}",
                    "sender": "sender@example.com",
                    "content": cipher.encrypt(content),
                    "created": 1000 + index,
                }
                for index in range(25)
            ],
        )
    relays = [
        RelayService(
            database=Database(engine=create_async_engine(url)), encryption_key=key
        )
        for _ in range(instances)
    ]
    try:
        await asyncio.gather(*(relay.initialize() for relay in relays))
        records = await relays[0].list_messages(page_size=100)
        assert records.total == 25
        assert all(message.subject == "测试" for message in records.items)
        preview = await relays[0].get_message_content("legacy-0")
        assert preview is not None
        assert preview.subject == "测试"
        assert preview.text == "legacy body\r\n"
        async with engine.connect() as connection:
            stored = list(
                (
                    await connection.execute(text("SELECT subject FROM smtp_messages"))
                ).scalars()
            )
        assert all(isinstance(value, bytes) for value in stored)
        assert all(cipher.decrypt(value).decode() == "测试" for value in stored)
        await relays[0].initialize()
        async with engine.connect() as connection:
            repeated = list(
                (
                    await connection.execute(text("SELECT subject FROM smtp_messages"))
                ).scalars()
            )
        assert repeated == stored
    finally:
        await asyncio.gather(*(relay.close() for relay in relays))
        await engine.dispose()
