# Передаёт принадлежащий владельцу чат общей операции очистки.

from pathlib import Path

from sqlalchemy.orm import Session as DatabaseSession

from app.Models import Thread, User
from app.Policies.ownership import owned
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.content_removal import erase_thread


def delete_chat(db: DatabaseSession, user: User, thread_id: str, media_dir: Path) -> None:
    """Удаляет чат владельца. Блокировка исключает запись нового задания во время очистки."""
    lock_user(db, id=user.id)
    erase_thread(db, owned(db, Thread, thread_id, user), media_dir)
