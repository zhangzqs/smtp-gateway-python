"""将两种提交协议统一为持久化邮件事务。"""

from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid

from .database import Database
from .schemas import MessageWrite, validate_address


class MessageTooLargeError(ValueError):
    """生成的邮件超过允许字节数。"""


class MessageService:
    """校验信封与大小，并在事务提交后才确认接收。"""

    def __init__(
        self, *, database: Database, max_message_bytes: int, max_recipients: int
    ) -> None:
        self._database = database
        self._max_message_bytes = max_message_bytes
        self._max_recipients = max_recipients

    async def submit(self, body: MessageWrite) -> str:
        """构造 RFC 邮件，为所有重试保留同一 Message-ID。"""
        message = EmailMessage(policy=SMTP)
        message["From"] = body.sender
        message["To"] = ", ".join(body.recipients)
        message["Subject"] = body.subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = make_msgid(domain="gstack-smtp-gateway")
        if body.text is not None:
            message.set_content(body.text)
            if body.html is not None:
                message.add_alternative(body.html, subtype="html")
        else:
            message.set_content(body.html or "", subtype="html")
        return await self.submit_raw(
            sender=body.sender,
            recipients=body.recipients,
            content=message.as_bytes(),
        )

    async def submit_raw(
        self, *, sender: str, recipients: list[str], content: bytes
    ) -> str:
        """持久化原始邮件；所有收件人共享正文但独立重试。"""
        if len(content) > self._max_message_bytes:
            raise MessageTooLargeError("Message exceeds the configured size limit")
        recipients = list(dict.fromkeys(validate_address(item) for item in recipients))
        if not recipients or len(recipients) > self._max_recipients:
            raise ValueError("Recipient count exceeds the configured limit")
        return await self._database.enqueue(
            sender=validate_address(sender), recipients=recipients, content=content
        )


__all__ = ["MessageService", "MessageTooLargeError"]
