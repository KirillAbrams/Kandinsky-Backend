# Сдерживает частые попытки входа и отбрасывает распространённые новые пароли. Общий счётчик
# хранится в базе, поэтому смена экземпляра сервера его не обнулит.

"""Один счётчик попыток входа для всех экземпляров сервера."""

import math
import time
from functools import cache
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.Models import AuthAttempt
from app.Services.Accounts.sessions import digest

WINDOW_SECONDS = 60
MAX_ATTEMPTS = 20


# Один раз читает набор отпечатков распространённых паролей и сохраняет его в памяти процесса.
# Проверки используют один список без повторного чтения файла.
@cache
def blocked_passwords() -> frozenset[str]:
    """Читает и запоминает отпечатки распространённых паролей."""
    return frozenset(Path(__file__).with_name("common_passwords.sha256").read_text().splitlines())


def check_new_password(password: str) -> None:
    """Отклоняет пароль, отпечаток которого есть в списке распространённых."""
    if digest(password) in blocked_passwords():
        raise HTTPException(422, "Этот пароль часто встречается. Выберите другой.")


def limit_attempt(db: Session, operation: str, identity: str) -> None:
    """Сохраняет попытку до проверки пароля и сообщает, когда можно повторить запрос."""
    now = time.time()
    key = digest(operation + "/" + identity.casefold())
    db.execute(delete(AuthAttempt).where(AuthAttempt.expires_at <= now))
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    count = AuthAttempt.attempt_count
    statement = insert(AuthAttempt).values(
        key=key, attempt_count=1, expires_at=now + WINDOW_SECONDS
    )
    statement = statement.on_conflict_do_update(
        index_elements=[AuthAttempt.key],
        set_={"attempt_count": case((count <= MAX_ATTEMPTS, count + 1), else_=count)},
    )
    attempts, expires = db.execute(statement.returning(count, AuthAttempt.expires_at)).one()
    # Неудачный вход тоже расходует попытку. Сохраняем счётчик до проверки пароля.
    db.commit()
    if attempts > MAX_ATTEMPTS:
        raise HTTPException(
            429,
            "Слишком много попыток. Попробуйте позже.",
            headers={"Retry-After": str(max(1, math.ceil(expires - now)))},
        )
