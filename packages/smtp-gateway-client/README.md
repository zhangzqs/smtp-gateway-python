# SMTP 网关 Python 客户端

`smtp-gateway-client` 提供异步管理客户端和网关共享模型，支持 Python 3.11 至 3.15。

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
        page = await client.list_messages(page=1, page_size=20)
        for message in page.items:
            for delivery in message.deliveries:
                print(message.id, message.subject, delivery.recipient, delivery.state)
        if page.items:
            content = await client.get_message_content(page.items[0].id)
            print(content.subject, content.text)
    finally:
        await client.close()


asyncio.run(main())
```

`get()` 与 `put()` 分别调用 `GET /v1/smtp` 和 `PUT /v1/smtp`，返回 `SmtpSettings`。认证使用 HTTP Basic
Auth，通过 HTTPS 或受信任内网访问。客户端固定使用 15 秒超时，忽略环境代理和证书配置，不跟随重定向。

`SmtpSettingsWrite.password` 使用 `SecretStr`。省略密码时请求不包含该字段，网关保留已保存的密码；显式传入空字符串时仍将空字符串交给网关校验。
响应模型过滤额外字段，不包含上游密码。TLS 模式为 `none`、`starttls` 和 `implicit`，默认 `starttls`。

`list_messages(page=1, page_size=20)` 调用 `GET /v1/messages`，返回 `MessagePage`：

- `items`：本页的 `MessageRecord`，包含邮件 `id`、信封发件人 `sender`、解码主题 `subject`、UTC 创建时间 `created_at` 和收件人投递列表 `deliveries`。`subject` 为必填字段，无主题邮件使用空字符串。
- `total`、`page`、`page_size`：记录总数、当前页和每页条数。页码从 1 开始，每页默认 20 条，最多 100 条。
- 每个 `DeliveryStatus` 包含 `recipient`、`state`、`attempts` 和 `last_error`。状态 `DeliveryState` 为 `pending`、`sending`、`delivered` 或 `failed`，错误仅为安全类别。

分页投递记录不包含邮件正文或密码；创建时间在线路上使用带时区的 ISO 8601 格式，在 Python 模型中为 UTC `datetime`。

`get_message_content(identifier)` 调用 `GET /v1/messages/{id}/content`，返回 `MessageContent`：

- `id` 和 `subject`：邮件标识与解码后的主题。
- `text` 和 `html`：解码后的纯文本和 HTML 正文，对应正文不存在时为 `None`。显式标记的附件和 `message/rfc822` 转发邮件不作为当前邮件正文返回。

内容查询使用相同的 Basic Auth，响应禁用缓存；邮件已清理或标识不存在时返回 `404`。接口不会获取 HTML 引用的远程资源。
`html` 是不可信的邮件原文，网页调用方应在禁用脚本、表单和外部资源的隔离预览中呈现，避免直接插入应用页面。
不要记录邮件正文或原始响应。

`DeliveryState`、`DeliveryStatus`、`MessageRecord`、`MessagePage` 和 `MessageContent` 均可从 `smtp_gateway_client` 公共入口导入。
HTTP 管理接口用于配置和查询邮件，邮件通过 SMTP 提交。

HTTP 错误抛出 `SmtpGatewayAPIError`，可读取 `status_code`；连接、超时以及无效响应映射为 `502`。
异常文本只包含操作与状态码，调用方应避免记录原始请求、响应和异常链中的敏感数据。

## 本地验证

在成员目录执行 `task check`、`task test` 和 `task build`。构建产物写入 workspace 根目录的 `dist/`。workspace 开发环境使用 Python 3.15；构建后在仓库根目录运行 `task test-client-wheel`，会用隔离的 Python 3.11 环境测试客户端成品。
测试通过构造参数 `transport` 注入 `httpx2.MockTransport`，不访问外部网络。
