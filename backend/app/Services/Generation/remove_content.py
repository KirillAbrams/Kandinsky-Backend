"""Удаляет содержимое запроса или отдельного результата."""

from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import ErasedGeneration, Job, User
from app.Policies.ownership import active_thread, owned
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.content_removal import remove_generation_content


def remove_job_content(
    db: Session,
    user: User,
    job_id: str,
    target: Literal["prompt", "result"],
    blob: UUID | None,
    media_dir: Path,
) -> None:
    """Удаляет запрос или результат владельца. Повтор удалённого запроса уже считается выполненным."""
    if (target == "result") != (blob is not None):
        raise HTTPException(422, "Выберите запрос или конкретный результат")
    if lock_user(db, id=user.id) is None:
        raise HTTPException(401, "Войдите в аккаунт")
    erased = db.get(ErasedGeneration, job_id)
    if erased and erased.user_id == user.id:
        return
    job = owned(db, Job, job_id, user)
    thread = active_thread(db, job.thread_id, user)
    if job.batch_id:
        members = list(
            db.scalars(
                select(Job)
                .where(Job.user_id == user.id, Job.batch_id == job.batch_id)
                .order_by(Job.batch_index)
                .with_for_update()
            )
        )
    else:
        members = [db.scalar(select(Job).where(Job.id == job.id).with_for_update())]
    if not remove_generation_content(
        db, thread, members, target, str(blob) if blob else None, media_dir
    ):
        raise HTTPException(404, "Результат не найден")
