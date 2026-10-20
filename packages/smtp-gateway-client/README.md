# SMTP 网关 Python 客户端

`smtp-gateway-client` 提供异步配置客户端和网关共享模型，支持 Python 3.11。

## 使用

```python
import asyncio
import os

from pydantic import SecretStr

from smtp_gateway_client import SmtpGatewayClient, SmtpSettingsWrite, TlsMode


async def main() -> None:
    client = SmtpGatewayClient(
        url=os.environ["SMTP_GATEWAY_URL"],
        username=os.environ["SMTP_GATEWAY_USERNAME"],
        password=os.environ["SMTP_GATEWAY_PASSWORD"],
    )
    try:
        await client.get()
        await client.put(
            SmtpSettingsWrite(
                host="smtp.example.com",
                port=587,
                username=os.environ["SMTP_USERNAME"],
                password=SecretStr(os.environ["SMTP_PASSWORD"]),
                tls=TlsMode.STARTTLS,
            )
        )
    finally:
        await client.close()


asyncio.run(main())
```

`get()` 与 `put()` 分别调用 `GET /v1/smtp` 和 `PUT /v1/smtp`，返回 `SmtpSettings`。
认证使用 HTTP Basic Auth，应配置 HTTPS 或受信任的本机链路。客户端固定使用 15 秒超时，忽略环境代理和证书配置，不跟随重定向。

`SmtpSettingsWrite.password` 使用 `SecretStr`。省略密码时请求不包含该字段，网关保留已保存的密码；显式传入空字符串时仍将空字符串交给网关校验。
响应模型过滤额外字段，不包含上游密码。TLS 模式为 `none`、`starttls` 和 `implicit`，默认 `starttls`。

HTTP 错误抛出 `SmtpGatewayAPIError`，可读取 `status_code`；连接、超时以及无效响应映射为 `502`。
异常文本只包含操作与状态码，调用方应避免记录原始请求、响应和异常链中的敏感数据。

## 本地验证

在成员目录执行 `task check`、`task test` 和 `task build`。构建产物写入 workspace 根目录的 `dist/`。
测试通过构造参数 `transport` 注入 `httpx.MockTransport`，不访问外部网络。
