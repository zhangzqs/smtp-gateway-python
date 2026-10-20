"""SMTP 信封校验不会改写持久化的邮件正文。"""

from unittest.mock import AsyncMock

import pytest

from smtp_gateway.services import MessageService
from smtp_gateway.shared import MessageTooLargeError


@pytest.mark.asyncio
async def test_raw_message_is_preserved_and_recipients_are_deduplicated() -> None:
    relay = AsyncMock()
    relay.enqueue.return_value = "persisted-message"
    service = MessageService(relay=relay, max_message_bytes=1024, max_recipients=2)
    content = b"Message-ID: <original@example.com>\r\nSubject: Alert\r\n\r\nbody\r\n"
    identifier = await service.submit_raw(
        sender="sender@example.com",
        recipients=["receiver@example.com", "receiver@example.com"],
        content=content,
    )
    assert identifier == "persisted-message"
    relay.enqueue.assert_awaited_once_with(
        sender="sender@example.com",
        recipients=["receiver@example.com"],
        content=content,
    )


@pytest.mark.parametrize(
    ("sender", "recipients", "error"),
    [
        ("sender@example.com\r\nQUIT", ["receiver@example.com"], "mailbox"),
        ("sender@example.com", ["receiver@example.com\r\nQUIT"], "mailbox"),
        ("sender@example.com", ["invalid@@example.com"], "mailbox"),
        ("sender@example.com", [], "Recipient count"),
        (
            "sender@example.com",
            ["one@example.com", "two@example.com"],
            "Recipient count",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_envelope_is_not_enqueued(
    sender: str, recipients: list[str], error: str
) -> None:
    relay = AsyncMock()
    service = MessageService(relay=relay, max_message_bytes=1024, max_recipients=1)
    with pytest.raises(ValueError, match=error):
        await service.submit_raw(sender=sender, recipients=recipients, content=b"mail")
    relay.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_smtp_message_is_not_enqueued() -> None:
    relay = AsyncMock()
    service = MessageService(relay=relay, max_message_bytes=1024, max_recipients=1)
    with pytest.raises(MessageTooLargeError):
        await service.submit_raw(
            sender="sender@example.com",
            recipients=["receiver@example.com"],
            content=b"x" * 1025,
        )
    relay.enqueue.assert_not_awaited()
