"""校验 SMTP 提交并持久化原始邮件。"""

from typing import Protocol

from gstack_smtp_gateway.shared import MessageTooLargeError, validate_address


class _Queue(Protocol):
    async def enqueue(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str: ...


class MessageService:
    """校验信封与大小，并在事务提交后才确认接收。"""

    def __init__(
        self, *, relay: _Queue, max_message_bytes: int, max_recipients: int
    ) -> None:
        self._relay = relay
        self._max_message_bytes = max_message_bytes
        self._max_recipients = max_recipients

    async def submit_raw(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str:
        """持久化原始邮件；所有收件人共享正文但独立重试。"""
        if len(content) > self._max_message_bytes:
            raise MessageTooLargeError("Message exceeds the configured size limit")
        recipients = list(dict.fromkeys(validate_address(item) for item in recipients))
        if not recipients or len(recipients) > self._max_recipients:
            raise ValueError("Recipient count exceeds the configured limit")
        return await self._relay.enqueue(
            sender=validate_address(sender), recipients=recipients, content=content
        )


__all__ = ["MessageService"]
