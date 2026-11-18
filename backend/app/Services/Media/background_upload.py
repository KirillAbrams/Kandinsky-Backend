# Принимает файл по временному разрешению даже после разрыва обычного соединения. Повтор с тем же
# содержимым возвращает закреплённый файл без создания копии.

"""Принимает исходник по разрешению на фоновую загрузку."""

import hashlib
import time
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.Models import Asset, UploadGrant
from app.Policies.ownership import active_thread
from app.Services.Accounts.sessions import digest, lock_user
from app.Services.Media.files import store_upload
from app.Services.Media.source_links import bind_source


def accept_background_upload(
    db: Session,
    ticket: str,
    source: BinaryIO,
    content_type: str | None,
    *,
    max_upload_bytes: int,
    media_dir: Path,
) -> Asset:
    """Сохраняет файл по разрешению. Повтор с теми же байтами возвращает прежний файл."""
    grant = db.get(UploadGrant, digest(ticket))
    if not grant or grant.expires_at <= time.time():
        raise HTTPException(401, "Разрешение на загрузку истекло")
    user = lock_user(db, id=grant.user_id)
    if user is None:
        raise HTTPException(401, "Разрешение на загрузку истекло")
    if grant.thread_id:
        active_thread(db, grant.thread_id, user)
    content = source.read(max_upload_bytes + 1)
    if len(content) > max_upload_bytes:
        raise HTTPException(413, "Файл больше 20 МБ")
    fingerprint = hashlib.sha256(content).hexdigest()
    if grant.asset_id:
        if grant.fingerprint != fingerprint:
            raise HTTPException(409, "Разрешение уже использовано для другого файла")
        asset = db.get(Asset, grant.asset_id)
        return asset
    path, mime = store_upload(content, content_type or "", media_dir)
    asset = Asset(user_id=grant.user_id, mime=mime, size=path.stat().st_size, filename=path.name)
    try:
        db.add(asset)
        db.flush()
        # Условная запись защищает от создания двух файлов при повторе.
        changed = db.execute(
            update(UploadGrant)
            .where(UploadGrant.digest == grant.digest, UploadGrant.asset_id.is_(None))
            .values(asset_id=asset.id, fingerprint=fingerprint)
        )
        if changed.rowcount:
            if grant.thread_id:
                bind_source(db, user, grant.thread_id, asset.id)
            db.commit()
        else:
            db.rollback()
            path.unlink(missing_ok=True)
            db.expire_all()
            grant = db.get(UploadGrant, digest(ticket))
            if grant.fingerprint != fingerprint:
                raise HTTPException(409, "Конфликт загрузки")
            asset = db.get(Asset, grant.asset_id)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return asset
