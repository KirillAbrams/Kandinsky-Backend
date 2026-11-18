# Выдаёт код восстановления и меняет пароль после проверки этого кода. Работа под блокировкой
# аккаунта не даёт двум одновременным попыткам использовать один код.

"""Выдача кода и смена пароля под блокировкой аккаунта."""

import hmac
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import Asset, PasswordReset, Session, Transfer, UploadGrant
from app.Services.Accounts.auth_limits import check_new_password, limit_attempt
from app.Services.Accounts.email_codes import (
    MAX_ATTEMPTS,
    RESEND_SECONDS,
    VALID_SECONDS,
    code_digest,
)
from app.Services.Accounts.sessions import lock_user, passwords


def issue_reset_code(db: DatabaseSession, email: str, *, secret: str, enabled: bool) -> str | None:
    """Возвращает новый код восстановления после паузы между письмами и сохраняет его отпечаток."""
    limit_attempt(db, "reset_request", email)
    if not enabled:
        raise HTTPException(503, "Отправка кодов пока недоступна")

    user = lock_user(db, email=email.lower())
    if user is None:
        return None

    now = time.time()
    reset = db.get(PasswordReset, user.id)
    can_send = reset is None or now - reset.requested_at >= RESEND_SECONDS
    if not can_send:
        return None

    code = f"{secrets.randbelow(10**8):08d}"
    if reset is None:
        reset = PasswordReset(user_id=user.id)
        db.add(reset)
    reset.digest = code_digest(secret, user.id, code)
    reset.requested_at = now
    reset.attempts = 0
    db.commit()
    return code


def reset_password(
    db: DatabaseSession,
    email: str,
    code: str,
    password: str,
    *,
    secret: str,
    enabled: bool,
) -> None:
    """Меняет пароль по действующему коду и отзывает прежние сеансы и ссылки на файлы."""
    limit_attempt(db, "reset_confirm", email)
    check_new_password(password)
    if not enabled:
        raise HTTPException(503, "Восстановление пока недоступно")

    user = lock_user(db, email=email.lower())
    # Один ответ покрывает неверный код, неизвестный адрес и истёкший срок.
    invalid_code = "Код неверный или больше не действует"
    if user is None:
        raise HTTPException(400, invalid_code)
    reset = db.get(PasswordReset, user.id)
    if (
        reset is None
        or reset.attempts >= MAX_ATTEMPTS
        or time.time() - reset.requested_at >= VALID_SECONDS
    ):
        raise HTTPException(400, invalid_code)
    if not hmac.compare_digest(reset.digest, code_digest(secret, user.id, code)):
        reset.attempts += 1
        db.commit()
        raise HTTPException(400, invalid_code)

    user.password_hash = passwords.hash(password)
    db.delete(reset)
    db.execute(delete(Session).where(Session.user_id == user.id))
    db.execute(delete(UploadGrant).where(UploadGrant.user_id == user.id))
    db.execute(
        delete(Transfer).where(
            Transfer.asset_id.in_(select(Asset.id).where(Asset.user_id == user.id))
        )
    )
    db.commit()
