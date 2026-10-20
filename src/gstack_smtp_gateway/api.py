"""受 Basic Auth 保护的管理与邮件提交 HTTP API。"""

import secrets
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .schemas import (
    MessageAccepted,
    MessageStatus,
    MessageWrite,
    SmtpSettings,
    SmtpSettingsWrite,
)
from .service import MessageTooLargeError


class _Settings(Protocol):
    async def get_smtp(self) -> SmtpSettings: ...
    async def put_smtp(self, body: SmtpSettingsWrite) -> SmtpSettings: ...
    async def status(self, identifier: str) -> MessageStatus | None: ...


class _Messages(Protocol):
    async def submit(self, body: MessageWrite) -> str: ...


def create_router(
    *, settings: _Settings, messages: _Messages, username: str, password: str
) -> APIRouter:
    """为服务调用方提供安全响应与事务提交确认。"""
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

    @router.post("/messages", status_code=202)
    async def submit_message(body: MessageWrite) -> MessageAccepted:
        """事务持久化邮件后确认接收。"""
        try:
            return MessageAccepted(id=await messages.submit(body))
        except MessageTooLargeError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/messages/{identifier}")
    async def message_status(identifier: str) -> MessageStatus:
        """查询各收件人的投递状态。"""
        result = await settings.status(identifier)
        if result is None:
            raise HTTPException(status_code=404, detail="Message was not found")
        return result

    return router


__all__ = ["create_router"]
