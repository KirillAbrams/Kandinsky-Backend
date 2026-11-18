"""Выдаёт ограниченные по сроку разрешения на отправку и скачивание."""

import secrets
import time
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.Models import Asset, Transfer, UploadGrant, User
from app.Policies.ownership import active_thread, owned
from app.Services.Accounts.sessions import digest, lock_user

UPLOAD_TICKET_SECONDS = 3600
TRANSFER_TICKET_SECONDS = 1800


def issue_upload_ticket(db: Session, user: User, thread_id: UUID | None) -> str:
    """Выдаёт разрешение на загрузку в пределах лимита незавершённых отправок."""
    lock_user(db, id=user.id)
    if thread_id:
        active_thread(db, str(thread_id), user)
    db.execute(
        delete(UploadGrant).where(
            UploadGrant.expires_at < time.time(), UploadGrant.asset_id.is_(None)
        )
    )
    count = db.scalar(
        select(func.count())
        .select_from(UploadGrant)
        .where(UploadGrant.user_id == user.id, UploadGrant.asset_id.is_(None))
    )
    if count >= 20:
        raise HTTPException(429, "Слишком много незавершённых загрузок")
    token = secrets.token_urlsafe(32)
    db.add(
        UploadGrant(
            digest=digest(token),
            user_id=user.id,
            thread_id=str(thread_id) if thread_id else None,
            expires_at=time.time() + UPLOAD_TICKET_SECONDS,
        )
    )
    db.commit()
    return token


def issue_transfer_ticket(db: Session, user: User, asset_id: str) -> str:
    """Выдаёт владельцу временное разрешение скачать выбранный файл."""
    lock_user(db, id=user.id)
    asset = owned(db, Asset, asset_id, user)
    token = secrets.token_urlsafe(32)
    db.execute(delete(Transfer).where(Transfer.expires_at < time.time()))
    db.add(
        Transfer(
            digest=digest(token),
            asset_id=asset.id,
            expires_at=time.time() + TRANSFER_TICKET_SECONDS,
        )
    )
    db.commit()
    return token
