"""Отменяет активные задания по одному или группой."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.Models import Job, User
from app.Policies.ownership import owned, visible_job
from app.Services.Generation.batches import batch_members
from app.Services.Generation.job_snapshots import JobSnapshot, job_snapshots


def cancel_generation_batch(db: Session, user: User, job_id: str) -> list[JobSnapshot]:
    """Останавливает активные задания группы и оставляет готовые результаты."""
    members = batch_members(db, visible_job(db, job_id, user))
    db.execute(
        update(Job)
        .where(Job.id.in_([member.id for member in members]), Job.status.in_(["queued", "running"]))
        .values(status="cancelled", lease_owner=None)
    )
    db.commit()
    return job_snapshots(db, members, user.id)


def cancel_generation(db: Session, user: User, job_id: str) -> JobSnapshot:
    """Отменяет активное задание владельца и возвращает его текущее состояние."""
    owned(db, Job, job_id, user)
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(["queued", "running"]))
        .values(status="cancelled", lease_owner=None)
    )
    db.commit()
    db.expire_all()
    return job_snapshots(db, [visible_job(db, job_id, user)], user.id)[0]
