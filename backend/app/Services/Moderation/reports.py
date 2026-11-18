# Позволяет уполномоченному оператору открыть и решить жалобу из служебной команды. При отзыве
# результата убирает доступ к файлу и записывает отметки для телефонов.

"""Рассмотрение жалоб на генерации из доверенной служебной командной строки"""

import time
from typing import get_args

from sqlalchemy import delete, select, update

from app.Enums.moderation import BLOCKED_GENERATION, ReportDecision
from app.Models import (
    Asset,
    GenerationReport,
    Job,
    PendingMediaRemoval,
    ResultRemoval,
    Transfer,
    UploadGrant,
)
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.content_removal import clear_pending_media
from app.Services.Database.notifications import notify


# Сверяет решение и оператора, блокирует аккаунт и применяет решение один раз. Не разрешает
# заменить уже принятое решение другим, а после отзыва пробует закончить очистку файлов.
def resolve_report(db, media_dir, job_id, decision, reviewer):
    reviewer = reviewer.strip()
    if decision not in get_args(ReportDecision) or not 1 <= len(reviewer) <= 80:
        raise ValueError("Укажите решение и имя оператора")
    owner = db.scalar(select(Job.user_id).where(Job.id == job_id))
    user = lock_user(db, id=owner) if owner else None
    report = db.get(GenerationReport, job_id) if user else None
    if report is None:
        raise ValueError("Жалоба не найдена")
    if report.decision is not None and report.decision != decision:
        raise ValueError("По жалобе уже принято другое решение")

    if report.decision is None:
        if decision != "keep":
            revoke_result(db, db.get(Job, job_id))
        report.reviewed_at = time.time()
        report.reviewer = reviewer
        report.decision = decision
        if decision == "remove-and-suspend":
            user.generation_suspended_at = report.reviewed_at
            block_active_jobs(db, owner)
    db.commit()
    if decision != "keep":
        clear_pending_media(db, media_dir)
    return report


# Останавливает ещё активные работы владельца или работы с одним исходником и записывает отметки
# отзыва. Устройства должны узнать о блокировке, иначе сохранили бы старое ожидание.
def block_active_jobs(db, owner, *, input_id=None):
    rows = update(Job).where(Job.user_id == owner, Job.status.in_(("queued", "running")))
    if input_id is not None:
        rows = rows.where(Job.input_id == input_id).values(input_id=None)
    blocked = db.scalars(
        rows.values(
            status="failed", moderated=True, error=BLOCKED_GENERATION, lease_owner=None
        ).returning(Job.id)
    ).all()
    db.add_all(ResultRemoval(job_id=job, owner_id=owner) for job in blocked)
    if blocked:
        notify(db, "results")


# Проверяет готовый файл, скрывает ссылающиеся ответы, отзывает доступ и ставит файл в очередь
# удаления. Задания с этим файлом как исходником тоже теряют доступ, чтобы он не использовался
# после отзыва.
def revoke_result(db, job):
    asset = db.get(Asset, job.output_id) if job.output_id else None
    if asset is None and job.moderated:
        return
    if asset is None or asset.user_id != job.user_id or not asset.generated:
        raise ValueError("Готовый файл жалобы не найден")
    ids = db.scalars(
        update(Job)
        .where(Job.user_id == job.user_id, Job.output_id == asset.id)
        .values(status="failed", error=BLOCKED_GENERATION, moderated=True, output_id=None)
        .returning(Job.id)
    ).all()
    db.add_all(ResultRemoval(job_id=id, owner_id=job.user_id, asset_id=asset.id) for id in ids)
    notify(db, "results")
    block_active_jobs(db, job.user_id, input_id=asset.id)
    db.execute(
        update(Job)
        .where(Job.user_id == job.user_id, Job.input_id == asset.id)
        .values(input_id=None)
    )
    db.execute(delete(Transfer).where(Transfer.asset_id == asset.id))
    db.execute(
        delete(UploadGrant).where(
            UploadGrant.user_id == job.user_id, UploadGrant.asset_id == asset.id
        )
    )
    db.add(PendingMediaRemoval(filename=asset.filename))
    db.delete(asset)
