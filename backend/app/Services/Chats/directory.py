# Готовит страницы списка чатов и сверяет известные номера с базой. Для списка читает короткие
# карточки, поэтому открытие боковой панели не загружает все сообщения.

"""Готовит сводку страницы чатов без чтения их истории."""

from collections.abc import Iterable
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DatabaseSession

from app.Models import Job, Thread
from app.Services.Chats.content_removal import thread_summary


def thread_summaries(
    db: DatabaseSession, threads: Iterable[Thread], owner_id: str
) -> list[dict[str, object]]:
    """Собирает карточки и одним запросом отмечает чаты с активными заданиями."""
    rows = list(threads)
    if not rows:
        return []
    active = set(
        db.scalars(
            select(Job.thread_id)
            .where(
                Job.user_id == owner_id,
                Job.thread_id.in_([thread.id for thread in rows]),
                Job.status.in_(("queued", "running")),
            )
            .distinct()
        )
    )
    return [{**thread_summary(thread), "generating": thread.id in active} for thread in rows]


def list_chats(
    db: DatabaseSession,
    owner_id: str,
    *,
    view: Literal["recent", "archive", "all"] | None,
    query: str,
    offset: int,
    limit: int,
) -> list[dict[str, object]]:
    """Читает страницу чатов владельца по разделу и поиску."""
    stmt = select(Thread).where(Thread.user_id == owner_id, Thread.deleted_at.is_(None))
    if view == "recent":
        stmt = stmt.where(Thread.archived_at.is_(None))
    elif view == "archive":
        stmt = stmt.where(Thread.archived_at.is_not(None))
    term = query.strip()
    if term:
        matches = (
            func.casefold(Thread.title).contains(term.casefold(), autoescape=True)
            if db.get_bind().dialect.name == "sqlite"
            else Thread.title.icontains(term, autoescape=True)
        )
        stmt = stmt.where(matches)
    if view is None:
        stmt = stmt.order_by(Thread.created_at.desc(), Thread.id.desc())
    else:
        stmt = stmt.order_by(
            Thread.pinned_at.is_not(None).desc(), Thread.activity_at.desc(), Thread.id.desc()
        )
    return thread_summaries(db, db.scalars(stmt.offset(offset).limit(limit)), owner_id)


def chat_states(
    db: DatabaseSession, owner_id: str, ids: list[UUID]
) -> dict[str, list[dict[str, object]] | list[str]]:
    """Сверяет известные номера владельца и отдельно отдаёт удалённые."""
    rows = list(
        db.scalars(
            select(Thread).where(
                Thread.user_id == owner_id,
                Thread.id.in_([str(value) for value in ids]),
            )
        )
    )
    return {
        "threads": thread_summaries(db, [row for row in rows if row.deleted_at is None], owner_id),
        "deleted": sorted(row.id for row in rows if row.deleted_at is not None),
    }


def deleted_chats(db: DatabaseSession, owner_id: str, ids: list[UUID]) -> list[str]:
    """Возвращает удалённые номера из списка, который прислал телефон."""
    if not ids:
        return []
    return list(
        db.scalars(
            select(Thread.id)
            .where(
                Thread.user_id == owner_id,
                Thread.deleted_at.is_not(None),
                Thread.id.in_([str(value) for value in ids]),
            )
            .order_by(Thread.id)
        )
    )
