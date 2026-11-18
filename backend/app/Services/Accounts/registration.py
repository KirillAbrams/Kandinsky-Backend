# Проверяет почту отдельно от записи аккаунта. После подтверждения создаёт владельца и выдаёт
# вход, поэтому одной отправки письма недостаточно для регистрации.

"""Подтверждение адреса до создания аккаунта."""

import hmac
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import RegistrationCode, User
from app.Services.Accounts import email_codes as mail
from app.Services.Accounts.auth_limits import check_new_password, limit_attempt
from app.Services.Accounts.sessions import issue_session, passwords
from config.studio import Settings
from database.seeders.starter_history import add_starter_history


def request_registration_code(
    db: DatabaseSession, settings: Settings, email: str, sender: mail.CodeSender | None
) -> dict[str, str]:
    """Выдаёт код свободному адресу. Ответ остаётся одинаковым для занятого адреса."""
    limit_attempt(db, "register_mail", email)
    secret = settings.password_reset_secret.get_secret_value()
    if len(secret) < 32 or not (sender or (settings.smtp_host and settings.smtp_from)):
        raise HTTPException(503, "Отправка кодов пока недоступна")
    reply = {"message": "Если адрес свободен, код придёт на почту"}
    now = time.time()
    db.execute(
        delete(RegistrationCode).where(RegistrationCode.requested_at <= now - mail.VALID_SECONDS)
    )
    if db.scalar(select(User.id).where(User.email == email)):
        db.commit()
        return reply
    code = f"{secrets.randbelow(10**8):08d}"
    digest = mail.code_digest(secret, "register:" + email, code)
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    values = dict(digest=digest, requested_at=now, attempts=0)
    changed = db.execute(
        insert(RegistrationCode)
        .values(email=email, **values)
        .on_conflict_do_update(
            index_elements=[RegistrationCode.email],
            set_=values,
            where=RegistrationCode.requested_at <= now - mail.RESEND_SECONDS,
        )
        .returning(RegistrationCode.email)
    ).scalar_one_or_none()
    db.commit()
    if changed is not None:
        try:
            if sender:
                sender(email, code)
            else:
                mail.send_code(settings, email, code, registration=True)
        except (OSError, mail.smtplib.SMTPException, ValueError) as error:
            db.execute(
                delete(RegistrationCode).where(
                    RegistrationCode.email == email, RegistrationCode.digest == digest
                )
            )
            db.commit()
            raise HTTPException(503, "Не удалось отправить письмо. Попробуйте ещё раз.") from error
    return reply


def consume_registration_code(
    db: DatabaseSession, settings: Settings, email: str, code: str
) -> None:
    # Атомарное обновление ограничивает попытки сразу для всех экземпляров сервера.
    # Успешное подтверждение и создание пользователя сохраняются одной транзакцией.
    """Учитывает попытку и списывает верный код в транзакции регистрации."""
    secret = settings.password_reset_secret.get_secret_value()
    if len(secret) < 32:
        raise HTTPException(503, "Подтверждение почты пока недоступно")
    digest = db.execute(
        update(RegistrationCode)
        .where(
            RegistrationCode.email == email,
            RegistrationCode.attempts < mail.MAX_ATTEMPTS,
            RegistrationCode.requested_at > time.time() - mail.VALID_SECONDS,
        )
        .values(attempts=RegistrationCode.attempts + 1)
        .returning(RegistrationCode.digest)
    ).scalar_one_or_none()
    if digest is None or not hmac.compare_digest(
        digest, mail.code_digest(secret, "register:" + email, code)
    ):
        db.commit()
        raise HTTPException(400, "Код неверный или больше не действует")
    db.execute(delete(RegistrationCode).where(RegistrationCode.email == email))


def create_account(
    db: DatabaseSession, settings: Settings, email: str, password: str, code: str
) -> dict[str, str]:
    """Создаёт аккаунт после проверки пароля и кода, затем открывает сеанс."""
    limit_attempt(db, "register", email)
    check_new_password(password)
    email = email.lower()
    consume_registration_code(db, settings, email, code)
    user = User(email=email, password_hash=passwords.hash(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Аккаунт уже существует. Войдите.") from exc
    if settings.starter_history:
        add_starter_history(db, user, settings.media_dir)
    else:
        db.commit()
    return {
        "token": issue_session(db, user, settings.session_hours),
        "user_id": user.id,
        "email": user.email,
    }
