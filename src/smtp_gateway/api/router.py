"""受 Basic Auth 保护的上游配置 HTTP API。"""

import secrets
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from smtp_gateway.shared import SmtpSettings, SmtpSettingsWrite


class _Settings(Protocol):
    async def get_smtp(self) -> SmtpSettings: ...
    async def put_smtp(self, body: SmtpSettingsWrite) -> SmtpSettings: ...


def create_router(*, settings: _Settings, username: str, password: str) -> APIRouter:
    """为服务调用方提供上游配置读取与写入。"""
    basic = HTTPBasic()

    def authenticate(
        credentials: Annotated[HTTPBasicCredentials, Depends(basic)],
    ) -> None:
        user_matches = secrets.compare_digest(
            credentials.username.encode(), username.encode()
        )
        password_matches = secrets.compare_digest(
            credentials.password.encode(), password.encode()
        )
        if not (user_matches and password_matches):
            raise HTTPException(
                status_code=401,
                detail="Invalid gateway credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

    @router.get("/smtp")
    async def get_smtp() -> SmtpSettings:
        """读取上游设置，不回显密码。"""
        return await settings.get_smtp()

    @router.put("/smtp")
    async def put_smtp(body: SmtpSettingsWrite) -> SmtpSettings:
        """保存上游设置，省略密码时保留已保存的值。"""
        try:
            return await settings.put_smtp(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["create_router"]
