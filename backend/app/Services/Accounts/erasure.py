# Проверяет право владельца на удаление аккаунта, отзывает его ключи, убирает записи и ставит
# файлы в очередь очистки. Служба связи получает отметку, чтобы удалить свои копии.

"""Удаление аккаунта и запись отметок для службы связи."""

import time
from logging import Logger
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import (
    AccountErasure,
    Asset,
    ErasedSession,
    Job,
    PasswordReset,
    PendingMediaRemoval,
    Session,
    Thread,
    Transfer,
    UploadGrant,
    User,
)
from app.Services.Accounts.auth_limits import limit_attempt
from app.Services.Accounts.sessions import lock_user, passwords
from app.Services.Chats.content_removal import clear_pending_media
from app.Services.Database.notifications import notify


def erase_account(db: DatabaseSession, user: User) -> None:
    """Под блокировкой удаляет аккаунт, отзывает сеансы и оставляет отметки очистки одной транзакцией."""
    owner = user.id
    owned_assets = select(Asset.id).where(Asset.user_id == owner)
    filenames = select(Asset.filename).where(Asset.user_id == owner)
    db.execute(insert(PendingMediaRemoval).from_select(["filename"], filenames))
    db.execute(delete(Transfer).where(Transfer.asset_id.in_(owned_assets)))
    db.execute(delete(UploadGrant).where(UploadGrant.user_id == owner))
    db.execute(delete(Job).where(Job.user_id == owner))
    db.execute(delete(Thread).where(Thread.user_id == owner))
    db.execute(delete(Asset).where(Asset.user_id == owner))
    db.execute(delete(PasswordReset).where(PasswordReset.user_id == owner))
    now = time.time()
    db.execute(delete(ErasedSession).where(ErasedSession.expires_at <= now))
    active_sessions = select(Session.digest, Session.expires_at).where(
        Session.user_id == owner, Session.expires_at > now
    )
    db.execute(insert(ErasedSession).from_select(["digest", "expires_at"], active_sessions))
    db.execute(delete(Session).where(Session.user_id == owner))
    db.add(AccountErasure(owner_id=owner))
    notify(db, "accounts")
    db.delete(user)
    db.commit()


def delete_owned_account(
    db: DatabaseSession, owner_id: str, password: str, media_dir: Path, log: Logger
) -> None:
    """Проверяет пароль владельца и удаляет аккаунт. При сбое очистка файлов остаётся в очереди."""
    limit_attempt(db, "delete", owner_id)
    owner = lock_user(db, id=owner_id)
    if owner is None:
        raise HTTPException(401, "Войдите снова")
    if not passwords.verify(password, owner.password_hash):
        raise HTTPException(403, "Неверный пароль")
    erase_account(db, owner)
    try:
        clear_pending_media(db, media_dir)
    except SQLAlchemyError as error:
        log.warning("Account file cleanup deferred (%s)", type(error).__name__)
