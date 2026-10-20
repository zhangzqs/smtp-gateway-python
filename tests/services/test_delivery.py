import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import aiosmtplib
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from gstack_smtp_gateway.persistence import Database, RelayRepository
from gstack_smtp_gateway.services import DeliveryWorker, RelayService, RetryPolicy
from gstack_smtp_gateway.shared import (
    ClaimedDelivery,
    DeliveryState,
    SmtpSettingsWrite,
    TlsMode,
    Upstream,
)
from gstack_smtp_gateway.smtp_client import SmtpSender


@dataclass(kw_only=True)
class _Queue:
    relay: RelayService
    clock: list[float]


@pytest_asyncio.fixture
async def queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Queue]:
    clock = [1000.0]

    async def current_time(_repository: RelayRepository) -> float:
        return clock[0]

    monkeypatch.setattr(RelayRepository, "now", current_time)
    database = Database(
        engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}"),
    )
    relay = RelayService(
        database=database,
        encryption_key=Fernet.generate_key().decode(),
    )
    await relay.initialize()
    await relay.put_smtp(SmtpSettingsWrite(host="smtp.example.com", port=587))
    try:
        yield _Queue(relay=relay, clock=clock)
    finally:
        await relay.close()


def _policy(*, max_attempts: int = 3, delivery_timeout: float = 10) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        retry_delay=5,
        max_retry_delay=8,
        delivery_timeout=delivery_timeout,
        lease_seconds=30,
        poll_interval=1,
        concurrency=1,
        retention_seconds=100,
    )


async def _enqueue(relay: RelayService, recipients: list[str]) -> str:
    return await relay.enqueue(
        sender="grafana@example.com",
        recipients=recipients,
        content=b"Subject: alert\r\n\r\nprivate body\r\n",
    )


@dataclass(kw_only=True)
class _Sender:
    failing: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def send(self, upstream: Upstream, delivery: ClaimedDelivery) -> None:
        assert upstream.host == "smtp.example.com"
        self.calls.append(delivery.recipient)
        if delivery.recipient in self.failing:
            raise aiosmtplib.SMTPException("upstream leaked-password private body")


@pytest.mark.asyncio
async def test_worker_delivers_each_recipient_without_repeating_success(
    queue: _Queue,
) -> None:
    identifier = await _enqueue(
        queue.relay, ["first@example.com", "second@example.com"]
    )
    sender = _Sender(failing={"second@example.com"})
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    assert await worker.run_once()
    assert not await worker.run_once()
    queue.clock[0] += 5
    sender.failing.clear()
    assert await worker.run_once()
    assert not await worker.run_once()
    assert sender.calls.count("first@example.com") == 1
    assert sender.calls.count("second@example.com") == 2
    status = await queue.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.DELIVERED for item in status.deliveries)


@pytest.mark.asyncio
async def test_worker_retries_with_capped_backoff_until_max_attempts(
    queue: _Queue,
) -> None:
    identifier = await _enqueue(queue.relay, ["operator@example.com"])
    sender = _Sender(failing={"operator@example.com"})
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    queue.clock[0] += 4
    assert not await worker.run_once()
    queue.clock[0] += 1
    assert await worker.run_once()
    queue.clock[0] += 7
    assert not await worker.run_once()
    queue.clock[0] += 1
    assert await worker.run_once()
    queue.clock[0] += 100
    assert not await worker.run_once()
    assert len(sender.calls) == 3
    status = await queue.relay.status(identifier)
    assert status is not None
    delivery = status.deliveries[0]
    assert delivery.state == DeliveryState.FAILED
    assert delivery.attempts == 3
    assert delivery.last_error == "SMTPException"
    assert "leaked-password" not in status.model_dump_json()
    assert "private body" not in status.model_dump_json()


@pytest.mark.asyncio
async def test_delivery_timeout_consumes_attempt_and_retries(queue: _Queue) -> None:
    identifier = await _enqueue(queue.relay, ["operator@example.com"])
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_send(_upstream: Upstream, _delivery: ClaimedDelivery) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sender = Mock(spec=SmtpSender)
    sender.send = blocked_send
    worker = DeliveryWorker(
        relay=queue.relay,
        sender=sender,
        policy=_policy(max_attempts=1, delivery_timeout=0.01),
    )
    assert await worker.run_once()
    assert started.is_set()
    assert cancelled.is_set()
    assert not await worker.run_once()
    status = await queue.relay.status(identifier)
    assert status is not None
    assert status.deliveries[0].attempts == 1
    assert status.deliveries[0].state == DeliveryState.FAILED
    assert status.deliveries[0].last_error == "TimeoutError"


def _delivery() -> ClaimedDelivery:
    return ClaimedDelivery(
        id="delivery-id",
        message_id="message-id",
        token=str(uuid4()),
        sender="grafana@example.com",
        recipient="operator@example.com",
        content=b"Subject: alert\r\n\r\nbody\r\n",
        attempts=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tls", "use_tls", "start_tls"),
    [
        (TlsMode.NONE, False, False),
        (TlsMode.STARTTLS, False, True),
        (TlsMode.IMPLICIT, True, False),
    ],
)
async def test_sender_uses_exact_tls_modes_and_verifies_certificates(
    tls: TlsMode,
    use_tls: bool,
    start_tls: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock(spec=aiosmtplib.SMTP)
    client.connect = AsyncMock()
    client.login = AsyncMock()
    client.sendmail = AsyncMock(return_value=({}, "accepted"))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(
        "gstack_smtp_gateway.smtp_client.client.aiosmtplib.SMTP", constructor
    )
    upstream = Upstream(
        host="smtp.example.com",
        port=465 if use_tls else 587,
        username="relay@example.com",
        password=SecretStr("upstream-secret"),
        tls=tls,
    )
    delivery = _delivery()
    await SmtpSender.send(upstream, delivery)
    constructor.assert_called_once_with(
        hostname=upstream.host,
        port=upstream.port,
        use_tls=use_tls,
        start_tls=start_tls,
        validate_certs=True,
    )
    client.connect.assert_awaited_once()
    client.login.assert_awaited_once_with("relay@example.com", "upstream-secret")
    client.sendmail.assert_awaited_once_with(
        delivery.sender, [delivery.recipient], delivery.content
    )
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_sender_closes_connection_after_recipient_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock(spec=aiosmtplib.SMTP)
    client.connect = AsyncMock()
    client.sendmail = AsyncMock(
        return_value=({"operator@example.com": (450, "try later")}, "accepted")
    )
    monkeypatch.setattr(
        "gstack_smtp_gateway.smtp_client.client.aiosmtplib.SMTP",
        Mock(return_value=client),
    )
    upstream = Upstream(
        host="smtp.example.com",
        port=25,
        username="",
        password=None,
        tls=TlsMode.NONE,
    )
    with pytest.raises(aiosmtplib.SMTPException, match="recipient was rejected"):
        await SmtpSender.send(upstream, _delivery())
    client.login.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_sender_closes_connection_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def blocked_sendmail(
        _sender: str, _recipients: list[str], _content: bytes
    ) -> tuple[dict[str, tuple[int, str]], str]:
        started.set()
        await asyncio.Event().wait()
        return {}, "accepted"

    client = Mock(spec=aiosmtplib.SMTP)
    client.connect = AsyncMock()
    client.sendmail = blocked_sendmail
    monkeypatch.setattr(
        "gstack_smtp_gateway.smtp_client.client.aiosmtplib.SMTP",
        Mock(return_value=client),
    )
    upstream = Upstream(
        host="smtp.example.com",
        port=25,
        username="",
        password=None,
        tls=TlsMode.NONE,
    )
    task = asyncio.create_task(SmtpSender.send(upstream, _delivery()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.close.assert_called_once()
