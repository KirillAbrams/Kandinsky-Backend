# Читает историю порциями по времени и номеру задания. Одинаковый состав ключа нужен при чтении в
# обе стороны, иначе строки с совпавшим временем можно пропустить.

"""История отсортирована по времени создания и номеру задания. Номер разрешает совпадения по времени."""

from typing import TypedDict
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.sql.elements import ColumnElement

from app.Models import Job, User
from app.Policies.ownership import active_thread, owned
from app.Services.Generation.job_snapshots import JobSnapshot, job_snapshots


class HistoryPageData(TypedDict):
    messages: list[JobSnapshot]
    has_older: bool
    has_newer: bool


def relative_to_anchor(anchor: Job, *, older: bool) -> ColumnElement[bool]:
    """Сравнивает время и номер задания, чтобы страница не теряла строки с одинаковым временем."""
    boundary = tuple_(anchor.created_at, anchor.id)
    position = tuple_(Job.created_at, Job.id)
    return position < boundary if older else position > boundary


def history_page(
    db: DatabaseSession,
    thread: str,
    owner: str,
    *,
    limit: int = 64,
    before: Job | None = None,
    after: Job | None = None,
    around: Job | None = None,
    media_only: bool = False,
    grouped: bool = False,
) -> HistoryPageData:
    """Читает страницу до, после или вокруг опоры и сообщает, есть ли продолжение."""
    history_query = select(Job).where(Job.thread_id == thread, Job.prompt_removed.is_(False))
    if grouped:
        history_query = history_query.where(or_(Job.batch_index.is_(None), Job.batch_index == 0))
    if media_only:
        history_query = history_query.where(Job.status == "succeeded", Job.output_id.is_not(None))
    matching_job_ids = history_query.with_only_columns(Job.id)
    newest_first = (Job.created_at.desc(), Job.id.desc())
    oldest_first = (Job.created_at, Job.id)
    if around:
        older_page_size = limit // 2
        older_jobs = list(
            db.scalars(
                history_query.where(relative_to_anchor(around, older=True))
                .order_by(*newest_first)
                .limit(older_page_size)
            )
        )
        newer_jobs = list(
            db.scalars(
                history_query.where(relative_to_anchor(around, older=False))
                .order_by(*oldest_first)
                .limit(limit - len(older_jobs) - 1)
            )
        )
        rows = [*reversed(older_jobs), around, *newer_jobs]
    elif after:
        rows = list(
            db.scalars(
                history_query.where(relative_to_anchor(after, older=False))
                .order_by(*oldest_first)
                .limit(limit)
            )
        )
    else:
        if before:
            history_query = history_query.where(relative_to_anchor(before, older=True))
        rows = list(db.scalars(history_query.order_by(*newest_first).limit(limit)))
        rows.reverse()
    return {
        "messages": job_snapshots(db, rows, owner),
        "has_older": bool(
            rows
            and db.scalar(matching_job_ids.where(relative_to_anchor(rows[0], older=True)).limit(1))
        ),
        "has_newer": bool(
            rows
            and db.scalar(
                matching_job_ids.where(relative_to_anchor(rows[-1], older=False)).limit(1)
            )
        ),
    }


def recent_jobs(db: DatabaseSession, user: User, thread_id: str) -> list[JobSnapshot]:
    """Возвращает последние задания владельца в порядке от старых к новым."""
    active_thread(db, thread_id, user)
    rows = list(
        db.scalars(
            select(Job)
            .where(Job.thread_id == thread_id, Job.prompt_removed.is_(False))
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(200)
        )
    )
    return job_snapshots(db, rows[::-1], user.id)


def chat_history(
    db: DatabaseSession,
    user: User,
    thread_id: str,
    *,
    limit: int,
    before: UUID | None,
    after: UUID | None,
    around: UUID | None,
    media_only: bool,
    grouped: bool,
) -> HistoryPageData:
    """Проверяет владельца и границу выбранного чата перед чтением страницы."""
    active_thread(db, thread_id, user)
    if sum(value is not None for value in (before, after, around)) > 1:
        raise HTTPException(422, "Выберите одну границу страницы")
    anchors = {}
    for name, value in (("before", before), ("after", after), ("around", around)):
        if value is not None:
            anchor = owned(db, Job, str(value), user)
            if (
                anchor.thread_id != thread_id
                or (grouped and anchor.batch_index not in (None, 0))
                or (
                    media_only
                    and name == "around"
                    and (anchor.status != "succeeded" or anchor.output_id is None)
                )
            ):
                raise HTTPException(404, "Не найдено")
            anchors[name] = anchor
    return history_page(
        db, thread_id, user.id, limit=limit, media_only=media_only, grouped=grouped, **anchors
    )
