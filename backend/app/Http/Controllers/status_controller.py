"""Готовность сервера и ожидание удалений для вошедшего владельца."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select

from app.Http.dependencies import DatabaseSession, get_removal_owner


# Ждёт изменения аккаунта, не удерживая соединение с базой.
async def wait_for_owner_removals(
    request: Request, owner: Annotated[str, Depends(get_removal_owner)], cursor: str = ""
):
    return await request.app.state.removal_feed.wait(cursor, "owner:" + owner)


# Проверяет доступ к базе и показывает выбранного исполнителя.
def health(request: Request, db: DatabaseSession):
    db.execute(select(1))
    return {"ok": True, "provider": request.app.state.settings.provider}
