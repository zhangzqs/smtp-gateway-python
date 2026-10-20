"""受 Basic Auth 保护的上游配置与投递记录 HTTP API。"""

import secrets
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from smtp_gateway_client import (
    MessageContent,
    MessagePage,
    SmtpSettings,
    SmtpSettingsWrite,
)


class _Settings(Protocol):
    async def get_smtp(self) -> SmtpSettings: ...
    async def put_smtp(self, body: SmtpSettingsWrite) -> SmtpSettings: ...
    async def list_messages(self, *, page: int, page_size: int) -> MessagePage: ...
    async def get_message_content(self, identifier: str) -> MessageContent | None: ...


def create_router(*, settings: _Settings, username: str, password: str) -> APIRouter:
    """为服务调用方提供上游配置与只读投递记录。"""
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

    @router.get("/messages")
    async def list_messages(
        response: Response,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> MessagePage:
        """分页查询邮件与收件人状态，不返回邮件正文。"""
        response.headers["Cache-Control"] = "no-store"
        return await settings.list_messages(page=page, page_size=page_size)

    @router.get("/messages/{identifier}/content")
    async def get_message_content(
        identifier: str, response: Response
    ) -> MessageContent:
        """读取单封邮件的主题和正文，内容只以 JSON 返回。"""
        headers = {"Cache-Control": "no-store"}
        content = await settings.get_message_content(identifier)
        if content is None:
            raise HTTPException(
                status_code=404, detail="Message not found", headers=headers
            )
        response.headers.update(headers)
        return content

    return router


__all__ = ["create_router"]
