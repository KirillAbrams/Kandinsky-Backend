# Записывает минимум сведений о том, что нужно стереть на телефонах и вычислительной машине. Текст
# и содержимое не требуются для запрета старых повторов.

"""Запоминает удалённые операции, не сохраняя их содержимое."""

import hashlib
from collections.abc import Iterable, Sequence

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.Models import ErasedGeneration, Job, JobContentRemoval, PendingGpuRemoval


def operation_digest(operation: str) -> str:
    """Хранит отпечаток ключа для распознавания позднего повтора."""
    return hashlib.sha256(operation.encode()).hexdigest()


def removal_payload(row: JobContentRemoval) -> dict[str, str | bool | None]:
    """Собирает номера и признаки удаления для службы связи."""
    return {
        "id": row.id,
        "thread_id": row.thread_id,
        "root_id": row.root_id,
        "member_id": row.member_id,
        "asset_id": row.asset_id,
        "prompt": row.prompt,
        "asset_only": row.asset_only,
        "retire_answer": row.retire_answer,
        "revoke_asset": row.revoke_asset,
    }


def remember_generations(db: Session, jobs: Sequence[Job]) -> None:
    """Оставляет отметки, чтобы повтор старого запроса не создал удалённое задание."""
    if jobs:
        db.execute(
            insert(ErasedGeneration),
            [
                {
                    "id": job.id,
                    "user_id": job.user_id,
                    "thread_id": job.thread_id,
                    "operation_digest": operation_digest(job.operation_key),
                }
                for job in jobs
            ],
        )


def queue_gpu_removals(db: Session, jobs: Iterable[Job]) -> None:
    """Ставит удалённые задания в очередь очистки на вычислительной машине."""
    candidates = {job.id: job for job in jobs if job.provider == "kandinsky"}
    for start in range(0, len(candidates), 128):
        keys = list(candidates)[start : start + 128]
        existing = set(
            db.scalars(select(PendingGpuRemoval.job_id).where(PendingGpuRemoval.job_id.in_(keys)))
        )
        pending = [
            {"job_id": key, "run_id": candidates[key].run_id} for key in keys if key not in existing
        ]
        if pending:
            db.execute(insert(PendingGpuRemoval), pending)
