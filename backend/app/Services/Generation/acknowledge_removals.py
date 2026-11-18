"""Закрывает доставленные телефону отметки удаления."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.Models import JobContentRemoval, User


def acknowledge_native_removals(db: Session, user: User, ids: list[UUID]) -> None:
    """Закрывает подтверждённые телефоном отметки удаления только для его владельца."""
    if not 1 <= len(ids) <= 32:
        raise HTTPException(422, "Неверное число отметок удаления")
    db.execute(
        delete(JobContentRemoval).where(
            JobContentRemoval.owner_id == user.id,
            JobContentRemoval.id.in_([str(value) for value in ids]),
        )
    )
    db.commit()
