# Проверяет пароль и хранит сведения о действующих входах. В базе остаются отпечатки ключей, срок
# действия и время последнего действия.

"""Долгая проверка пароля замедляет подбор
Для случайного ключа сессии достаточно хранить короткий отпечаток"""

import hashlib
import secrets
import time

from fastapi import HTTPException
from pwdlib import PasswordHash
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import ErasedSession, Session, User

passwords = PasswordHash.recommended()
# По времени ответа сложнее угадать, зарегистрирована ли почта.
DUMMY_HASH = passwords.hash("not-a-real-user-password")


def digest(token: str) -> str:
    """Вычисляет отпечаток ключа для поиска сеанса в базе."""
    return hashlib.sha256(token.encode()).hexdigest()


# Берёт блокировку записи и перечитывает владельца. В местной базе сначала начинает запись, чтобы
# вход, смена пароля и удаление не опирались на устаревшее состояние.
def lock_user(db: DatabaseSession, **identity: str) -> User | None:
    """Упорядочивает изменения одного аккаунта и читает свежие данные"""
    if db.get_bind().dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    return db.scalar(
        select(User)
        .filter_by(**identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


# Создаёт случайный ключ, сохраняет срок и отпечаток, затем отдаёт ключ клиенту. Подтверждение
# записи до ответа не даёт получить ключ к ещё не сохранённому сеансу.
def issue_session(db: DatabaseSession, user: User, hours: int) -> str:
    """Сохраняет отпечаток нового ключа с точным сроком действия"""
    token = secrets.token_urlsafe(32)
    db.add(Session(digest=digest(token), user_id=user.id, expires_at=time.time() + hours * 3600))
    db.commit()
    return token


def authenticate(
    db: DatabaseSession, email: str, password: str, session_hours: int
) -> dict[str, str]:
    """Проверяет пароль и выдаёт ключ нового сеанса."""
    user = lock_user(db, email=email.lower())
    valid = passwords.verify(password, user.password_hash if user else DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(401, "Неверная почта или пароль")
    return {
        "token": issue_session(db, user, session_hours),
        "user_id": user.id,
        "email": user.email,
    }


def record_activity(db: DatabaseSession, token: str, idle_minutes: int) -> None:
    """Продлевает активность только у действующего сеанса."""
    now = time.time()
    changed = db.execute(
        update(Session)
        .where(
            Session.digest == digest(token),
            Session.expires_at > now,
            Session.last_activity_at > now - idle_minutes * 60,
        )
        .values(last_activity_at=now)
    )
    db.commit()
    if changed.rowcount != 1:
        deleted = db.get(ErasedSession, digest(token))
        reason = "account_deleted" if deleted and deleted.expires_at > now else "Войдите в аккаунт"
        raise HTTPException(401, reason)


def revoke_session(db: DatabaseSession, token: str) -> None:
    """Удаляет сеанс по отпечатку переданного ключа."""
    db.execute(delete(Session).where(Session.digest == digest(token)))
    db.commit()
