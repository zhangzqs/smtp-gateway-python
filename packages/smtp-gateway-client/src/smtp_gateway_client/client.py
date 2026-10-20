"""通过 Basic Auth 管理 SMTP 网关。"""

from http import HTTPMethod, HTTPStatus
from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from .models import MessageContent, MessagePage, SmtpSettings, SmtpSettingsWrite

_Response = TypeVar("_Response", bound=BaseModel)


class SmtpGatewayAPIError(Exception):
    """调用失败，异常文本不包含凭据或原始响应。"""

    def __init__(self, status_code: int, operation: str) -> None:
        self.status_code = status_code
        super().__init__(f"SMTP gateway {operation} failed with HTTP {status_code}")


class SmtpGatewayClient:
    """使用服务凭据访问管理接口，并明确控制 HTTP 连接生命周期。"""

    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if transport is None:
            transport = httpx.AsyncHTTPTransport(trust_env=False)
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            auth=httpx.BasicAuth(username, password),
            transport=transport,
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        )

    async def _request(
        self,
        method: HTTPMethod,
        path: str,
        response_model: type[_Response],
        *,
        payload: dict[str, object] | None = None,
        params: dict[str, int] | None = None,
    ) -> _Response:
        operation = f"{method} {path}"
        try:
            response = await self._client.request(
                method, path, json=payload, params=params
            )
        except httpx.HTTPError as err:
            raise SmtpGatewayAPIError(HTTPStatus.BAD_GATEWAY, operation) from err
        if response.status_code != HTTPStatus.OK:
            raise SmtpGatewayAPIError(response.status_code, operation)
        try:
            return response_model.model_validate(response.json())
        except ValueError as err:
            raise SmtpGatewayAPIError(HTTPStatus.BAD_GATEWAY, operation) from err

    async def get(self) -> SmtpSettings:
        """读取隐藏上游密码的 SMTP 设置。"""
        return await self._request(HTTPMethod.GET, "/v1/smtp", SmtpSettings)

    async def put(self, body: SmtpSettingsWrite) -> SmtpSettings:
        """提交完整设置，仅在显式提供密码时发送明文值。"""
        payload = body.model_dump(mode="json", exclude={"password"})
        if body.password is not None:
            payload["password"] = body.password.get_secret_value()
        return await self._request(
            HTTPMethod.PUT, "/v1/smtp", SmtpSettings, payload=payload
        )

    async def list_messages(self, *, page: int = 1, page_size: int = 20) -> MessagePage:
        """分页读取邮件信封及各收件人的投递状态，不返回正文。"""
        if page < 1:
            raise ValueError("Message page must be at least 1")
        if not 1 <= page_size <= 100:
            raise ValueError("Message page size must be between 1 and 100")
        return await self._request(
            HTTPMethod.GET,
            "/v1/messages",
            MessagePage,
            params={"page": page, "page_size": page_size},
        )

    async def get_message_content(self, identifier: str) -> MessageContent:
        """读取指定邮件的主题与纯文本、HTML 正文，不返回附件。"""
        segment = quote(identifier, safe="").replace(".", "%2E")
        return await self._request(
            HTTPMethod.GET,
            f"/v1/messages/{segment}/content",
            MessageContent,
        )

    async def close(self) -> None:
        """释放 HTTP 连接池及注入的 transport。"""
        await self._client.aclose()


__all__ = ["SmtpGatewayAPIError", "SmtpGatewayClient"]
