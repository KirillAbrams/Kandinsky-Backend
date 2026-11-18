"""Связывает сохранённый исходник с чатом, а обложку с оригиналом."""

from sqlalchemy.orm import Session

from app.Models import User
from app.Services.Accounts.sessions import lock_user
from app.Services.Media.source_links import bind_derivative, bind_source


def link_chat_source(db: Session, user: User, thread_id: str, asset_id: str) -> None:
    """Связывает принадлежащий владельцу исходник с чатом и сохраняет связь."""
    lock_user(db, id=user.id)
    bind_source(db, user, thread_id, asset_id)
    db.commit()


def link_asset_preview(db: Session, user: User, parent_id: str, child_id: str) -> None:
    """Сохраняет привязку обложки к оригинальному файлу владельца."""
    lock_user(db, id=user.id)
    bind_derivative(db, user, parent_id, child_id)
    db.commit()
