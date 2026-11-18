# Читает ячейки одной группы владельца в порядке, заданном при отправке.

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import Job


def batch_members(db: Session, job: Job) -> list[Job]:
    """Возвращает ячейки одной отправки в том порядке, в котором их создали."""
    if job.batch_id is None:
        return [job]
    return list(
        db.scalars(
            select(Job)
            .where(Job.user_id == job.user_id, Job.batch_id == job.batch_id)
            .order_by(Job.batch_index)
        )
    )
