"""邮件预览解析遵循 MIME 正文与附件边界。"""

from email import policy
from email.message import EmailMessage

import pytest

from smtp_gateway.services.content import parse_message_content, parse_subject


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"", ""),
        (b"Subject: Alert\r\n", "Alert"),
        (b"Subject: =?utf-8?b?5ZGK6K2m5rWL6K+V?=\r\n", "告警测试"),
        (b"Subject: =?iso-8859-1?q?caf=E9?=\r\n", "café"),
        ("Subject: 告警测试\r\n".encode(), "告警测试"),
    ],
)
def test_subject_is_decoded_consistently_without_a_mime_body(
    header: bytes, expected: str
) -> None:
    content = header + b"\r\nbody"
    assert parse_subject(content) == expected
    assert parse_message_content("message-id", content).subject == expected


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
def test_multipart_alternative_returns_both_body_formats(encoding: str) -> None:
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = "告警测试"
    message.set_content("告警详情", cte=encoding)
    message.add_alternative("<p>告警详情</p>", subtype="html", cte=encoding)

    result = parse_message_content("message-id", message.as_bytes(policy=policy.SMTP))

    assert result.id == "message-id"
    assert result.subject == "告警测试"
    assert result.text == "告警详情\r\n"
    assert result.html == "<p>告警详情</p>\r\n"


@pytest.mark.parametrize(
    ("charset", "payload", "expected"),
    [
        ("gb18030", "你好".encode("gb18030"), "你好"),
        ("iso-8859-1", b"caf\xe9", "café"),
        ("x-unknown", "你好".encode(), "你好"),
        (None, "你好".encode(), "你好"),
        ("utf-8", b"a\xffb", "a�b"),
        ("x-unknown", b"a\xffb", "a�b"),
    ],
)
def test_body_charset_is_respected_or_decoded_with_utf8_replacement(
    charset: str | None, payload: bytes, expected: str
) -> None:
    header = "Content-Type: text/plain"
    if charset:
        header += f"; charset={charset}"
    result = parse_message_content(
        "message-id", header.encode() + b"\r\n\r\n" + payload
    )
    assert result.text == expected
    assert result.html is None


def test_mixed_message_excludes_text_and_multipart_attachments() -> None:
    message = EmailMessage()
    message.set_content("actual body")
    message.add_alternative("<p>actual body</p>", subtype="html")
    message.add_attachment("private attachment", filename="details.txt")
    attachment = EmailMessage()
    attachment.set_content("private attached body")
    attachment.add_alternative("<p>private attached body</p>", subtype="html")
    attachment["Content-Disposition"] = "attachment"
    message.attach(attachment)

    result = parse_message_content("message-id", message.as_bytes())

    assert result.text == "actual body\n"
    assert result.html == "<p>actual body</p>\n"
    assert "private" not in result.model_dump_json()


def test_text_attachment_before_body_is_not_used_for_preview() -> None:
    message = EmailMessage()
    message.make_mixed()
    attachment = EmailMessage()
    attachment.set_content("private attachment")
    attachment["Content-Disposition"] = 'attachment; filename="details.txt"'
    message.attach(attachment)
    body = EmailMessage()
    body.set_content("actual body")
    message.attach(body)

    result = parse_message_content("message-id", message.as_bytes())

    assert result.text == "actual body\n"
    assert result.html is None


def test_nested_rfc822_is_not_used_as_the_enclosing_message_body() -> None:
    content = (
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Subject: private forwarded subject\r\n"
        b"Content-Type: text/plain\r\n\r\nprivate forwarded body\r\n--x--"
    )

    result = parse_message_content("message-id", content)

    assert result.subject == ""
    assert result.text is None
    assert result.html is None
    assert "private" not in result.model_dump_json()


def test_related_body_uses_start_content_id_instead_of_the_first_part() -> None:
    message = EmailMessage()
    message.set_type("multipart/related")
    message.set_param("start", "<body>")
    image = EmailMessage()
    image.set_content(b"image data", maintype="image", subtype="png")
    image["Content-ID"] = "<image>"
    message.attach(image)
    body = EmailMessage()
    body.set_content('<p>body<img src="cid:image"></p>', subtype="html")
    body["Content-ID"] = "<body>"
    message.attach(body)

    result = parse_message_content("message-id", message.as_bytes())

    assert result.text is None
    assert result.html == '<p>body<img src="cid:image"></p>\n'


@pytest.mark.parametrize(
    ("content", "text", "html"),
    [
        (b"", "", None),
        (b"Content-Type: text/html\r\n\r\n<p>body</p>", None, "<p>body</p>"),
        (b"Content-Type: image/png\r\n\r\nimage data", None, None),
    ],
)
def test_preview_handles_empty_and_single_part_messages(
    content: bytes, text: str | None, html: str | None
) -> None:
    result = parse_message_content("message-id", content)
    assert result.text == text
    assert result.html == html
