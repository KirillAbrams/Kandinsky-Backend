"""Читает текущее состояние генераций и маленькие обложки результатов."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.Models import Asset, Job


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job: Job
    preview: str | None


def job_snapshots(db: Session, rows: Sequence[Job], owner: str) -> list[JobSnapshot]:
    """Читает только маленькие кадры результатов, принадлежащих владельцу."""
    output_ids = {row.output_id for row in rows if row.output_id}
    previews = {}
    if output_ids:
        tiny_preview = Asset.preview_info["tiny"].as_string()
        previews = dict(
            db.execute(
                select(Asset.id, tiny_preview).where(
                    Asset.user_id == owner, Asset.id.in_(output_ids)
                )
            ).all()
        )
    return [JobSnapshot(row, previews.get(row.output_id)) for row in rows]
