"""通过 Basic Auth 管理 SMTP 网关。"""

from http import HTTPMethod, HTTPStatus

import httpx

from .models import SmtpSettings, SmtpSettingsWrite


class SmtpGatewayAPIError(Exception):
    """调用失败，异常文本不包含凭据或原始响应。"""

    def __init__(self, status_code: int, operation: str) -> None:
        self.status_code = status_code
        super().__init__(f"SMTP gateway {operation} failed with HTTP {status_code}")


class SmtpGatewayClient:
    """使用服务凭据访问配置接口，并明确控制 HTTP 连接生命周期。"""

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
        self, method: HTTPMethod, payload: dict[str, object] | None = None
    ) -> SmtpSettings:
        operation = f"{method} /v1/smtp"
        try:
            response = await self._client.request(method, "/v1/smtp", json=payload)
        except httpx.HTTPError as err:
            raise SmtpGatewayAPIError(HTTPStatus.BAD_GATEWAY, operation) from err
        if response.status_code != HTTPStatus.OK:
            raise SmtpGatewayAPIError(response.status_code, operation)
        try:
            return SmtpSettings.model_validate(response.json())
        except ValueError as err:
            raise SmtpGatewayAPIError(HTTPStatus.BAD_GATEWAY, operation) from err

    async def get(self) -> SmtpSettings:
        """读取隐藏上游密码的 SMTP 设置。"""
        return await self._request(HTTPMethod.GET)

    async def put(self, body: SmtpSettingsWrite) -> SmtpSettings:
        """提交完整设置，仅在显式提供密码时发送明文值。"""
        payload = body.model_dump(mode="json", exclude={"password"})
        if body.password is not None:
            payload["password"] = body.password.get_secret_value()
        return await self._request(HTTPMethod.PUT, payload)

    async def close(self) -> None:
        """释放 HTTP 连接池及注入的 transport。"""
        await self._client.aclose()


__all__ = ["SmtpGatewayAPIError", "SmtpGatewayClient"]
