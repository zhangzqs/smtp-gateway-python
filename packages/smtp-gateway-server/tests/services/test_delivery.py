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
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from smtp_gateway.persistence import Database, RelayRepository
from smtp_gateway.persistence.models import DeliveryRow
from smtp_gateway.services import DeliveryWorker, RelayService, RetryPolicy
from smtp_gateway.shared import (
    ClaimedDelivery,
    ClaimedRecipient,
    DeliveryState,
    Upstream,
)
from smtp_gateway.smtp_client import SmtpSender
from smtp_gateway_client import SmtpSettingsWrite, TlsMode


@dataclass(kw_only=True)
class _Queue:
    relay: RelayService
    database: Database
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
        yield _Queue(relay=relay, database=database, clock=clock)
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
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def send(
        self, upstream: Upstream, delivery: ClaimedDelivery
    ) -> dict[str, str]:
        assert upstream.host == "smtp.example.com"
        self.calls.append(tuple(item.recipient for item in delivery.recipients))
        return {
            item.recipient: "SMTPRecipientRefused"
            for item in delivery.recipients
            if item.recipient in self.failing
        }


@pytest.mark.asyncio
async def test_worker_delivers_batch_and_only_retries_rejected_recipients(
    queue: _Queue,
) -> None:
    identifier = await _enqueue(
        queue.relay, ["first@example.com", "second@example.com"]
    )
    sender = _Sender(failing={"second@example.com"})
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    assert not await worker.run_once()
    assert set(sender.calls[0]) == {"first@example.com", "second@example.com"}
    status = await queue.relay.status(identifier)
    assert status is not None
    states = {item.recipient: item.state for item in status.deliveries}
    assert states == {
        "first@example.com": DeliveryState.DELIVERED,
        "second@example.com": DeliveryState.PENDING,
    }
    queue.clock[0] += 5
    sender.failing.clear()
    assert await worker.run_once()
    assert not await worker.run_once()
    assert len(sender.calls) == 2
    assert sender.calls[1] == ("second@example.com",)
    status = await queue.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.DELIVERED for item in status.deliveries)
    assert {item.recipient: item.attempts for item in status.deliveries} == {
        "first@example.com": 1,
        "second@example.com": 2,
    }


@pytest.mark.asyncio
async def test_worker_sends_all_recipients_in_one_batch(queue: _Queue) -> None:
    recipients = ["first@example.com", "second@example.com", "third@example.com"]
    identifier = await _enqueue(queue.relay, recipients)
    sender = _Sender()
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    assert not await worker.run_once()
    assert len(sender.calls) == 1
    assert set(sender.calls[0]) == set(recipients)
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
    assert delivery.last_error == "SMTPRecipientRefused"
    assert "leaked-password" not in status.model_dump_json()
    assert "private body" not in status.model_dump_json()


@pytest.mark.asyncio
async def test_worker_preserves_individual_attempt_limits_and_backoff(
    queue: _Queue,
) -> None:
    recipients = ["first@example.com", "second@example.com", "third@example.com"]
    identifier = await _enqueue(queue.relay, recipients)
    async with queue.database.session() as session, session.begin():
        for recipient, attempts in zip(recipients, (0, 1, 2), strict=True):
            await session.execute(
                update(DeliveryRow)
                .where(DeliveryRow.recipient == recipient)
                .values(attempts=attempts)
            )
    sender = _Sender(failing=set(recipients))
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    assert set(sender.calls[0]) == set(recipients)
    status = await queue.relay.status(identifier)
    assert status is not None
    assert {
        item.recipient: (item.attempts, item.state) for item in status.deliveries
    } == {
        "first@example.com": (1, DeliveryState.PENDING),
        "second@example.com": (2, DeliveryState.PENDING),
        "third@example.com": (3, DeliveryState.FAILED),
    }
    sender.failing.clear()
    queue.clock[0] += 4
    assert not await worker.run_once()
    queue.clock[0] += 1
    assert await worker.run_once()
    assert sender.calls[1] == ("first@example.com",)
    assert not await worker.run_once()
    queue.clock[0] += 3
    assert await worker.run_once()
    assert sender.calls[2] == ("second@example.com",)
    assert not await worker.run_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        aiosmtplib.SMTPRecipientsRefused(
            [
                aiosmtplib.SMTPRecipientRefused(
                    450, "leaked-password private body", "first@example.com"
                ),
                aiosmtplib.SMTPRecipientRefused(
                    550, "leaked-password private body", "second@example.com"
                ),
            ]
        ),
        aiosmtplib.SMTPDataError(451, "leaked-password private body"),
        aiosmtplib.SMTPAuthenticationError(535, "leaked-password private body"),
        OSError("leaked-password private body"),
    ],
)
async def test_worker_retries_whole_batch_on_transaction_failure_without_leaking(
    error: Exception, queue: _Queue, caplog: pytest.LogCaptureFixture
) -> None:
    recipients = ["first@example.com", "second@example.com"]
    identifier = await _enqueue(queue.relay, recipients)
    sender = Mock(spec=SmtpSender)
    sender.send = AsyncMock(side_effect=error)
    worker = DeliveryWorker(relay=queue.relay, sender=sender, policy=_policy())
    assert await worker.run_once()
    assert not await worker.run_once()
    status = await queue.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.PENDING for item in status.deliveries)
    assert all(item.attempts == 1 for item in status.deliveries)
    assert all(item.last_error == type(error).__name__ for item in status.deliveries)
    assert "leaked-password" not in status.model_dump_json() + caplog.text
    assert "private body" not in status.model_dump_json() + caplog.text
    queue.clock[0] += 5
    sender.send.side_effect = None
    sender.send.return_value = {}
    assert await worker.run_once()
    assert not await worker.run_once()
    assert sender.send.await_args is not None
    retried = sender.send.await_args.args[1]
    assert {item.recipient for item in retried.recipients} == set(recipients)
    status = await queue.relay.status(identifier)
    assert status is not None
    assert all(item.state == DeliveryState.DELIVERED for item in status.deliveries)


@pytest.mark.asyncio
async def test_delivery_timeout_consumes_attempt_and_retries(queue: _Queue) -> None:
    identifier = await _enqueue(
        queue.relay, ["first@example.com", "second@example.com"]
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_send(
        _upstream: Upstream, _delivery: ClaimedDelivery
    ) -> dict[str, str]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

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
    assert all(item.attempts == 1 for item in status.deliveries)
    assert all(item.state == DeliveryState.FAILED for item in status.deliveries)
    assert all(item.last_error == "TimeoutError" for item in status.deliveries)


def _delivery() -> ClaimedDelivery:
    return ClaimedDelivery(
        message_id="message-id",
        token=str(uuid4()),
        sender="grafana@example.com",
        recipients=(
            ClaimedRecipient(id="first-id", recipient="first@example.com", attempts=1),
            ClaimedRecipient(
                id="second-id", recipient="second@example.com", attempts=2
            ),
        ),
        content=b"Subject: alert\r\n\r\nbody\r\n",
    )


@pytest.fixture
def smtp_constructor(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(spec=aiosmtplib.SMTP)
    client.connect = AsyncMock()
    client.login = AsyncMock()
    client.sendmail = AsyncMock(return_value=({}, "accepted"))
    constructor = Mock(return_value=client)
    monkeypatch.setattr("smtp_gateway.smtp_client.client.aiosmtplib.SMTP", constructor)
    return constructor


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
    smtp_constructor: Mock,
) -> None:
    client = smtp_constructor.return_value
    upstream = Upstream(
        host="smtp.example.com",
        port=465 if use_tls else 587,
        username="relay@example.com",
        password=SecretStr("upstream-secret"),
        tls=tls,
    )
    delivery = _delivery()
    assert await SmtpSender.send(upstream, delivery) == {}
    smtp_constructor.assert_called_once_with(
        hostname=upstream.host,
        port=upstream.port,
        use_tls=use_tls,
        start_tls=start_tls,
        validate_certs=True,
    )
    client.connect.assert_awaited_once()
    client.login.assert_awaited_once_with("relay@example.com", "upstream-secret")
    client.sendmail.assert_awaited_once_with(
        delivery.sender,
        [item.recipient for item in delivery.recipients],
        delivery.content,
    )
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_sender_returns_only_refused_recipients_without_upstream_response_text(
    smtp_constructor: Mock,
) -> None:
    client = smtp_constructor.return_value
    client.sendmail = AsyncMock(
        return_value=(
            {
                "second@example.com": aiosmtplib.SMTPResponse(
                    450, "leaked-password private body"
                )
            },
            "accepted",
        )
    )
    upstream = Upstream(
        host="smtp.example.com",
        port=25,
        username="",
        password=None,
        tls=TlsMode.NONE,
    )
    assert await SmtpSender.send(upstream, _delivery()) == {
        "second@example.com": "SMTPRecipientRefused"
    }
    client.login.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        aiosmtplib.SMTPRecipientsRefused(
            [
                aiosmtplib.SMTPRecipientRefused(450, "try later", "first@example.com"),
                aiosmtplib.SMTPRecipientRefused(
                    550, "no mailbox", "second@example.com"
                ),
            ]
        ),
        aiosmtplib.SMTPDataError(451, "try later"),
    ],
)
async def test_sender_propagates_transaction_errors_and_closes_connection(
    error: aiosmtplib.SMTPException, smtp_constructor: Mock
) -> None:
    client = smtp_constructor.return_value
    client.sendmail = AsyncMock(side_effect=error)
    upstream = Upstream(
        host="smtp.example.com",
        port=25,
        username="",
        password=None,
        tls=TlsMode.NONE,
    )
    with pytest.raises(type(error)):
        await SmtpSender.send(upstream, _delivery())
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_sender_closes_connection_when_cancelled(
    smtp_constructor: Mock,
) -> None:
    started = asyncio.Event()

    async def blocked_sendmail(
        _sender: str, _recipients: list[str], _content: bytes
    ) -> tuple[dict[str, aiosmtplib.SMTPResponse], str]:
        started.set()
        await asyncio.Event().wait()
        return {}, "accepted"

    client = smtp_constructor.return_value
    client.sendmail = blocked_sendmail
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
