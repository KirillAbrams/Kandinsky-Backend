"""Находит недавние загрузки и файлы по действующему разрешению."""

import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import Asset, Transfer, UploadGrant, User
from app.Services.Accounts.sessions import digest


def recent_uploads(db: Session, user: User) -> list[Asset]:
    """Возвращает недавние файлы, загруженные по разрешениям этого владельца."""
    rows = db.execute(
        select(Asset)
        .join(UploadGrant, UploadGrant.asset_id == Asset.id)
        .where(UploadGrant.user_id == user.id)
        .order_by(Asset.created_at.desc())
        .limit(10)
    ).scalars()
    return list(rows)


def asset_for_transfer(db: Session, ticket: str) -> Asset:
    """Находит файл, пока разрешение на скачивание действительно."""
    transfer = db.get(Transfer, digest(ticket))
    if not transfer or transfer.expires_at <= time.time():
        raise HTTPException(401, "Ссылка для скачивания истекла")
    asset = db.get(Asset, transfer.asset_id)
    if asset is None:
        raise HTTPException(401, "Ссылка для скачивания истекла")
    return asset
