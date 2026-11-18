"""Читает порции удалений для службы связи."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import AccountErasure, JobContentRemoval, ResultRemoval, Thread
from app.Services.Chats.erasure_records import removal_payload


def pending_thread_erasures(db: Session) -> list[dict[str, str]]:
    rows = db.scalars(
        select(Thread)
        .where(Thread.erasure_pending.is_(True), Thread.deleted_at.is_not(None))
        .order_by(Thread.id)
        .limit(128)
    )
    return [{"id": row.id, "owner_id": row.user_id} for row in rows]


def pending_content_erasures(db: Session) -> list[dict[str, object]]:
    rows = db.scalars(
        select(JobContentRemoval)
        .order_by(JobContentRemoval.created_at, JobContentRemoval.id)
        .limit(128)
    )
    return [{**removal_payload(row), "owner_id": row.owner_id} for row in rows]


def pending_account_erasures(db: Session) -> list[str]:
    return list(
        db.scalars(select(AccountErasure.owner_id).order_by(AccountErasure.owner_id).limit(100))
    )


def pending_result_removals(db: Session) -> list[dict[str, str | None]]:
    rows = db.scalars(select(ResultRemoval).order_by(ResultRemoval.job_id).limit(100))
    return [
        {"job_id": row.job_id, "owner_id": row.owner_id, "asset_id": row.asset_id} for row in rows
    ]
