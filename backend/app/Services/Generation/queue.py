# Передаёт задания свободному обработчику и хранит срок его права на запись. После истечения срока
# работу может забрать другой обработчик, а старый уже не сможет записать ход выполнения.

"""Условное обновление записи передаёт задание одному обработчику
Очередь остаётся в той же базе, что и история"""

import time
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.Models import ACTIVE_JOB_PREDICATE, Job
from app.Services.Database.notifications import notify


def eligible(now: float) -> ColumnElement[bool]:
    """Отбирает ожидающие задания и работы с истёкшим правом обработки."""
    return and_(Job.status.in_(["queued", "running"]), Job.lease_until <= now)


def next_claim_time(factory: sessionmaker[Session]) -> float | None:
    """Возвращает ближайший срок, когда обработчик сможет забрать задание."""
    with factory() as db:
        return db.scalar(
            select(func.min(Job.lease_until)).where(Job.status.in_(("queued", "running")))
        )


def claim(factory: sessionmaker[Session], lease_seconds: int) -> Job | None:
    """Закрепляет старейшее доступное задание за одним обработчиком."""
    now = time.time()
    with factory() as db:
        db.execute(
            update(Job)
            .where(eligible(now), Job.attempts >= 3)
            .values(status="failed", error="Не удалось создать. Попробуйте ещё раз.")
        )
        db.commit()
        candidate_job_id = db.scalar(
            select(Job.id).where(eligible(now), Job.attempts < 3).order_by(Job.created_at).limit(1)
        )
        if not candidate_job_id:
            return None
        lease_id = str(uuid.uuid4())
        claim_result = db.execute(
            update(Job)
            .where(Job.id == candidate_job_id, eligible(now))
            .values(
                status="running",
                started_at=func.coalesce(Job.started_at, now),
                lease_owner=lease_id,
                progress=None,
                lease_until=now + lease_seconds,
                attempts=Job.attempts + 1,
            )
        )
        db.commit()
        # Пока мы читали очередь, задание мог забрать другой обработчик. Запускаем модель, только если
        # изменили строку сами.
        return db.get(Job, candidate_job_id) if claim_result.rowcount == 1 else None


def record_progress(factory: sessionmaker[Session], job_id: str, owner: str, progress: int) -> None:
    """Записывает рост процента для задания с действующим правом обработки."""
    with factory.begin() as db:
        db.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.lease_owner == owner,
                Job.lease_until >= time.time(),
                or_(Job.progress.is_(None), Job.progress < progress),
            )
            .values(progress=progress)
        )


def release_lease(factory: sessionmaker[Session], job_id: str, owner: str, delay: int = 0) -> None:
    """Возвращает задание в очередь после паузы без потери попытки."""
    with factory.begin() as db:
        released = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.lease_owner == owner)
            .values(
                status="queued",
                lease_owner=None,
                lease_until=time.time() + delay,
                progress=None,
                started_at=None,
                attempts=Job.attempts - 1,
            )
        )

        if released.rowcount:
            notify(db, "queue")


def renew(factory: sessionmaker[Session], job_id: str, owner: str, seconds: int) -> bool:
    """Продлевает право обработки, пока задание закреплено за этим исполнителем."""
    with factory() as db:
        changed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.lease_owner == owner, Job.status == "running")
            .values(lease_until=time.time() + seconds)
        )
        db.commit()
        return changed.rowcount == 1


def check_queue_capacity(db: Session, user_id: str, additional_jobs: int) -> None:
    """Проверяет число заданий под блокировкой владельца, которую уже взял вызывающий код."""
    active_job_count = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user_id, text(ACTIVE_JOB_PREDICATE))
    )
    if active_job_count + additional_jobs > 10:
        raise HTTPException(429, "Сначала дождитесь текущих генераций")
