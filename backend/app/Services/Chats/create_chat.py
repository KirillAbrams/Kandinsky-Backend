# Создаёт чат с сохранённым номером операции и принимает безопасный повтор.

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import Thread, User
from app.Policies.ownership import active_thread
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.directory import thread_summaries


def create_chat(db: DatabaseSession, user: User, chat_id: uuid.UUID | None) -> dict[str, object]:
    """Создаёт чат или возвращает уже созданный с этим номером для того же владельца."""
    lock_user(db, id=user.id)
    key = str(chat_id) if chat_id is not None else str(uuid.uuid4())
    existing = db.get(Thread, key)
    if existing:
        return thread_summaries(db, [active_thread(db, key, user)], user.id)[0]
    item = Thread(id=key, user_id=user.id)
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        item = active_thread(db, key, user)
    return thread_summaries(db, [item], user.id)[0]
