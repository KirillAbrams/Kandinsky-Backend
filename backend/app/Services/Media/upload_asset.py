"""Сохраняет исходник и при необходимости привязывает его к оригиналу."""

from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.Models import Asset, User
from app.Policies.ownership import owned
from app.Services.Accounts.sessions import lock_user
from app.Services.Media.files import store_upload
from app.Services.Media.source_links import bind_derivative


def upload_asset(
    db: Session,
    user: User,
    source: BinaryIO,
    content_type: str | None,
    parent: UUID | None,
    *,
    media_dir: Path,
    max_upload_bytes: int,
) -> Asset:
    """Сохраняет файл владельца и убирает его с диска при сбое записи в базу."""
    lock_user(db, id=user.id)
    if parent:
        owned(db, Asset, str(parent), user)
    # Читаем на байт больше предела, чтобы распознать слишком большой файл без загрузки его целиком.
    content = source.read(max_upload_bytes + 1)
    if len(content) > max_upload_bytes:
        raise HTTPException(413, "Файл больше 20 МБ")
    path, mime = store_upload(content, content_type or "", media_dir)
    item = Asset(user_id=user.id, mime=mime, size=path.stat().st_size, filename=path.name)
    db.add(item)
    try:
        db.flush()
        if parent:
            bind_derivative(db, user, str(parent), item.id)
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return item
