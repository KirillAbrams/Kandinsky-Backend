# Описывает владельцев, сеансы, почтовые коды, счётчики и отметки удаления. Раздельные таблицы
# позволяют заканчивать сеансы и удалять просроченные коды без изменения профиля.

import time

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.Models.base import Base, new_id


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    generation_suspended_at: Mapped[float | None] = mapped_column(Float)


class Session(Base):
    __tablename__ = "sessions"
    # В базе хранится только отпечаток ключа. Сам ключ остаётся у клиента.
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)

    last_activity_at: Mapped[float] = mapped_column(Float, default=time.time)


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class ErasedSession(Base):
    __tablename__ = "erased_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class AccountErasure(Base):
    __tablename__ = "account_erasures"
    owner_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class RegistrationCode(Base):
    __tablename__ = "registration_codes"
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[float] = mapped_column(Float, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
