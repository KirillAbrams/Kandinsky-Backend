"""Сохраняет одну жалобу владельца на готовый результат."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.Models import Asset, GenerationReport, Job, User
from app.Policies.ownership import owned
from app.Services.Accounts.sessions import lock_user


def report_result(db: Session, user: User, job_id: str, reason: str) -> GenerationReport:
    """Сохраняет жалобу владельца на готовый результат. Повтор возвращает прежнюю жалобу."""
    lock_user(db, id=user.id)
    item = owned(db, Job, job_id, user)
    existing = db.get(GenerationReport, item.id)
    if existing is not None:
        return existing
    asset = db.get(Asset, item.output_id) if item.output_id else None
    if (
        item.status != "succeeded"
        or asset is None
        or not asset.generated
        or asset.user_id != user.id
    ):
        raise HTTPException(409, "Пожаловаться можно на готовый результат")
    report = GenerationReport(job_id=item.id, reason=reason)
    db.add(report)
    db.commit()
    return report
