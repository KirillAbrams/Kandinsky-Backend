# Меняет свойства чата под блокировкой владельца и уведомляет устройства.

import time

from fastapi import HTTPException
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import User
from app.Policies.ownership import active_thread
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.directory import thread_summaries
from app.Services.Database.notifications import notify


def update_chat(
    db: DatabaseSession,
    user: User,
    thread_id: str,
    *,
    title: str | None,
    pinned: bool | None,
    archived: bool | None,
) -> dict[str, object]:
    """Меняет свойства чата. При переносе в архив снимает закрепление."""
    lock_user(db, id=user.id)
    thread = active_thread(db, thread_id, user)
    if pinned is True and (
        archived is True or (archived is None and thread.archived_at is not None)
    ):
        raise HTTPException(409, "Сначала верните чат из архива")
    if title is not None:
        thread.title = title
        thread.custom_title = True
    if archived is True:
        thread.archived_at = thread.archived_at or time.time()
        thread.pinned_at = None
    elif archived is False:
        thread.archived_at = None
    if pinned is True:
        thread.pinned_at = thread.pinned_at or time.time()
    elif pinned is False:
        thread.pinned_at = None
    # Уведомление нужно, только если другой телефон увидит изменение.
    if db.is_modified(thread, include_collections=False):
        notify(db, "owner:" + user.id)
    db.commit()
    return thread_summaries(db, [thread], user.id)[0]
