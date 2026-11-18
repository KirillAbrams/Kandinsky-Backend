"""Читает состояние и отметки удаления для владельца."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import JobContentRemoval, User
from app.Policies.ownership import visible_job
from app.Services.Chats.erasure_records import removal_payload
from app.Services.Generation.batches import batch_members
from app.Services.Generation.job_snapshots import JobSnapshot, job_snapshots


def pending_native_removals(
    db: Session, user: User
) -> dict[str, list[dict[str, str | bool | None]]]:
    """Возвращает старейшие отметки удаления владельца для телефона."""
    rows = db.scalars(
        select(JobContentRemoval)
        .where(JobContentRemoval.owner_id == user.id)
        .order_by(JobContentRemoval.created_at, JobContentRemoval.id)
        .limit(32)
    )
    return {"items": [removal_payload(row) for row in rows]}


def generation_batch(db: Session, user: User, job_id: str) -> list[JobSnapshot]:
    """Читает всю группу доступного владельцу задания."""
    return job_snapshots(db, batch_members(db, visible_job(db, job_id, user)), user.id)


def generation_result(db: Session, user: User, job_id: str) -> JobSnapshot:
    """Читает одно видимое владельцу задание вместе с обложкой."""
    return job_snapshots(db, [visible_job(db, job_id, user)], user.id)[0]
