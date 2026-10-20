"""通过指定上游 SMTP 发送邮件，处理认证和 TLS 协议。"""

from typing import assert_never

import aiosmtplib

from smtp_gateway.shared import ClaimedDelivery, Upstream
from smtp_gateway_client import TlsMode


class SmtpSender:
    """通过成熟客户端处理认证和三种上游 TLS 模式。"""

    @staticmethod
    async def send(upstream: Upstream, delivery: ClaimedDelivery) -> dict[str, str]:
        match upstream.tls:
            case TlsMode.NONE:
                use_tls, start_tls = False, False
            case TlsMode.STARTTLS:
                use_tls, start_tls = False, True
            case TlsMode.IMPLICIT:
                use_tls, start_tls = True, False
            case _ as unexpected:
                assert_never(unexpected)
        client = aiosmtplib.SMTP(
            hostname=upstream.host,
            port=upstream.port,
            use_tls=use_tls,
            start_tls=start_tls,
            validate_certs=True,
        )
        try:
            await client.connect()
            if upstream.username:
                if upstream.password is None:
                    raise RuntimeError("Authenticated SMTP upstream has no password")
                await client.login(
                    upstream.username, upstream.password.get_secret_value()
                )
            refused, _response = await client.sendmail(
                delivery.sender,
                [item.recipient for item in delivery.recipients],
                delivery.content,
            )
            # 仅 DATA 被确认后，未出现在拒收结果中的收件人才算成功。
            return dict.fromkeys(refused, "SMTPRecipientRefused")
        finally:
            # DATA 确认即表示上游已接收；关闭连接不能将已确认邮件变成重试。
            client.close()


__all__ = ["SmtpSender"]
