# Возвращает в очередь выбранный результат. Одному номеру повтора соответствует
# одна попытка.

import time
import uuid

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.Models import Job, User
from app.Policies.ownership import active_thread, visible_job
from app.Services.Accounts.sessions import lock_user
from app.Services.Database.notifications import notify
from app.Services.Generation.job_snapshots import JobSnapshot, job_snapshots
from app.Services.Generation.queue import check_queue_capacity


def retry_generation(
    db: Session, user: User, job_id: str, operation: uuid.UUID, expected_run: uuid.UUID | None
) -> JobSnapshot:
    """Ставит выбранное задание в очередь. Повтор команды возвращает ту же попытку."""
    locked_user = lock_user(db, id=user.id)
    if locked_user is None:
        raise HTTPException(401, "Войдите в аккаунт")
    job = visible_job(db, job_id, user)
    thread = active_thread(db, job.thread_id, user)
    retry_run_id = str(uuid.uuid5(uuid.UUID(job.id), str(operation)))
    if job.run_id == retry_run_id:
        return job_snapshots(db, [job], user.id)[0]
    if locked_user.generation_suspended_at is not None:
        raise HTTPException(403, "Доступ к генерации приостановлен")
    if job.status != "failed" or job.moderated or job.output_id or job.removed_output_id:
        raise HTTPException(409, "Эту генерацию сейчас нельзя повторить")
    expected_run_id = str(expected_run) if expected_run else None
    if job.run_id != expected_run_id:
        raise HTTPException(409, "Состояние генерации уже изменилось")
    check_queue_capacity(db, user.id, 1)
    retry_result = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "failed", Job.run_id == expected_run_id)
        .values(
            run_id=retry_run_id,
            status="queued",
            error=None,
            progress=None,
            started_at=None,
            attempts=0,
            lease_owner=None,
            lease_until=0,
        )
    )
    if not retry_result.rowcount:
        raise HTTPException(409, "Состояние генерации уже изменилось")
    thread.activity_at = time.time()
    notify(db, "queue")
    notify(db, "owner:" + user.id)
    db.commit()
    return job_snapshots(db, [job], user.id)[0]
