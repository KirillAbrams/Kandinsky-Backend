"""Учитывает попытку входа и открывает сеанс после проверки пароля."""

from sqlalchemy.orm import Session

from app.Services.Accounts.auth_limits import limit_attempt
from app.Services.Accounts.sessions import authenticate


def login_with_password(
    db: Session, email: str, password: str, session_hours: int
) -> dict[str, str]:
    """Учитывает попытку входа и выдаёт сеанс после проверки пароля."""
    limit_attempt(db, "login", email)
    return authenticate(db, email, password, session_hours)
