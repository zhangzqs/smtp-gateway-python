"""解析邮件主题和 MIME 正文，不返回附件内容。"""

from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from typing import cast

from smtp_gateway_client import MessageContent


def parse_subject(content: bytes) -> str:
    """只解析邮件头并解码主题，不解析 MIME 正文。"""
    message = BytesHeaderParser(policy=policy.default).parsebytes(content)
    return str(message.get("Subject", ""))


def _body(message: EmailMessage, subtype: str) -> str | None:
    part = message.get_body(preferencelist=(subtype,))
    if part is None:
        return None
    payload = cast(bytes, part.get_payload(decode=True))
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_message_content(identifier: str, content: bytes) -> MessageContent:
    """解码正文；MIME 的附件部分与转发邮件不作为当前邮件正文。"""
    message = BytesParser(_class=EmailMessage, policy=policy.default).parsebytes(
        content
    )
    return MessageContent(
        id=identifier,
        subject=str(message.get("Subject", "")),
        text=_body(message, "plain"),
        html=_body(message, "html"),
    )


__all__ = ["parse_message_content", "parse_subject"]
