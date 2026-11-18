"""Закрывает удаление после того, как служба связи его подтвердила."""

import time
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.Models import AccountErasure, ErasedSession, JobContentRemoval, ResultRemoval, Thread


def acknowledge_thread_erasures(db: Session, ids: list[UUID]) -> None:
    if not 1 <= len(ids) <= 128:
        raise HTTPException(422, "Неверное число отметок")
    db.execute(
        update(Thread)
        .where(Thread.id.in_([str(value) for value in ids]), Thread.deleted_at.is_not(None))
        .values(erasure_pending=False)
    )
    db.commit()


def acknowledge_content_erasures(db: Session, ids: list[UUID]) -> None:
    if not 1 <= len(ids) <= 128:
        raise HTTPException(422, "Неверное число отметок")
    db.execute(
        delete(JobContentRemoval).where(JobContentRemoval.id.in_([str(value) for value in ids]))
    )
    db.commit()


def acknowledge_account_erasures(db: Session, owners: list[UUID]) -> None:
    db.execute(
        delete(AccountErasure).where(AccountErasure.owner_id.in_([str(owner) for owner in owners]))
    )
    db.execute(delete(ErasedSession).where(ErasedSession.expires_at <= time.time()))
    db.commit()


def acknowledge_result_removals(db: Session, jobs: list[UUID]) -> None:
    db.execute(delete(ResultRemoval).where(ResultRemoval.job_id.in_([str(job) for job in jobs])))
    db.commit()
