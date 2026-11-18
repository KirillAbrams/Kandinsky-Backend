"""Готовит аккаунт для локальной проверки и сохраняет его при повторном запуске."""

import os

from app.Models import User
from app.Services.Accounts.auth_limits import check_new_password
from app.Services.Accounts.sessions import passwords
from app.Services.Database.connection import database
from config.studio import Settings
from database.seeders.starter_history import add_starter_history
from sqlalchemy import func, select

REVIEW_EMAIL = "reviewer@example.com"


def main() -> None:
    settings = Settings()
    if settings.provider != "kandinsky" or not settings.starter_history:
        raise RuntimeError("Для проверочного аккаунта нужен стенд со стартовыми чатами")

    password = os.environ["KANDINSKY_REVIEW_PASSWORD"]
    check_new_password(password)
    engine, sessions = database(settings.database_url)
    try:
        with sessions() as db:
            # Два запуска на одной базе должны подготовить только один комплект истории.
            db.execute(select(func.pg_advisory_xact_lock(func.hashtext(REVIEW_EMAIL))))
            user = db.scalar(select(User).where(User.email == REVIEW_EMAIL))
            if user is None:
                user = User(email=REVIEW_EMAIL, password_hash=passwords.hash(password))
                db.add(user)
                db.flush()
                add_starter_history(db, user, settings.media_dir)
            elif not passwords.verify(password, user.password_hash):
                raise RuntimeError(
                    "Пароль в .env.review отличается от пароля в местной базе"
                )
    finally:
        engine.dispose()

    print(f"Почта для входа {REVIEW_EMAIL}")
    print(f"Пароль для входа {password}")


if __name__ == "__main__":
    main()
