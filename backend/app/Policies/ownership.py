# Собирает проверки принадлежности строк, чатов и заданий. Общие правила помогают вернуть
# одинаковый отказ при запросе чужих данных из разных обработчиков.

"""Проверки доступа к данным пользователя."""

from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.Models import Asset, ErasedGeneration, Job, Thread, User

OwnedRecord = TypeVar("OwnedRecord", bound=Asset | Job | Thread)


def owned(db: Session, model: type[OwnedRecord], key: str, user: User) -> OwnedRecord:
    """Возвращает запись владельца. Чужой и неизвестный номера дают один ответ."""
    item = db.get(model, key)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "Не найдено")
    return item


def active_thread(db: Session, key: str, user: User) -> Thread:
    """Возвращает чат владельца, если он ещё не удалён."""
    thread = owned(db, Thread, key, user)
    if thread.deleted_at is not None:
        raise HTTPException(410, "Чат удалён")
    return thread


def visible_job(db: Session, job_id: str, user: User) -> Job:
    """Возвращает видимое задание. Отметка удаления учитывается до поиска записи."""
    erased = db.get(ErasedGeneration, job_id)
    if erased and erased.user_id == user.id:
        thread = db.get(Thread, erased.thread_id)
        if thread is None or thread.deleted_at is not None:
            raise HTTPException(404, "Не найдено")
        raise HTTPException(410, "Запрос удалён")
    job = owned(db, Job, job_id, user)
    if job.prompt_removed:
        raise HTTPException(410, "Запрос удалён")
    return job
