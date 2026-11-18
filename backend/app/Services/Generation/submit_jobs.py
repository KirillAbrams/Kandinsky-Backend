# Принимает новое задание или группу за одну транзакцию. Прежний ключ отправки
# позволяет вернуть результаты после обрыва связи.

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, replace
from math import gcd

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.Models import Asset, ErasedGeneration, Job, User
from app.Policies.ownership import active_thread, owned
from app.Services.Accounts.sessions import lock_user
from app.Services.Chats.erasure_records import operation_digest
from app.Services.Database.notifications import notify
from app.Services.Generation.batches import batch_members
from app.Services.Generation.job_snapshots import JobSnapshot, job_snapshots
from app.Services.Generation.queue import check_queue_capacity
from app.Services.Media.files import preview_metadata
from config.studio import Settings


@dataclass(frozen=True, slots=True)
class GenerationParameters:
    """Проверенные параметры генерации для постановки в очередь."""

    thread_id: str
    prompt: str
    model: str
    aspect: str
    seed: int
    input_id: str | None
    quality: str
    resolution: str
    duration_seconds: int
    negative_prompt: str
    guidance_scale: float | None


def fingerprint_payload(parameters: GenerationParameters) -> dict[str, object]:
    """Готовит поля для отпечатка с учётом прежних значений по умолчанию."""
    value = asdict(parameters)
    for key, default in {
        "quality": "standard",
        "resolution": "standard",
        "duration_seconds": 5,
        "negative_prompt": "",
        "guidance_scale": None,
    }.items():
        if value[key] == default:
            del value[key]
    return value


def submit_jobs(
    db: Session,
    settings: Settings,
    user: User,
    parameters: GenerationParameters,
    operation: str,
    amount: int = 1,
) -> list[JobSnapshot]:
    """Сохраняет группу заданий за одну транзакцию.

    Повтор с тем же ключом и содержимым возвращает прежние задания.
    Удалённая отправка повторно не ставится в очередь.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", operation):
        raise HTTPException(422, "Нужен стабильный ключ операции, 16–100 символов")
    locked_user = lock_user(db, id=user.id)
    if locked_user is None:
        raise HTTPException(401, "Войдите в аккаунт")
    thread = active_thread(db, parameters.thread_id, user)
    if db.scalar(
        select(ErasedGeneration.id).where(
            ErasedGeneration.user_id == user.id,
            ErasedGeneration.operation_digest == operation_digest(operation),
        )
    ):
        raise HTTPException(410, "Запрос удалён")
    job_requests = [
        replace(parameters, seed=(parameters.seed + index) % (2**31)) for index in range(amount)
    ]
    fingerprints = [
        hashlib.sha256(
            json.dumps(fingerprint_payload(item), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        for item in job_requests
    ]

    # Ищет прежнюю отправку по владельцу и ключу, затем сверяет отпечатки всех результатов по
    # порядку. Один ключ нельзя использовать для подмены запроса или части группы.
    def find_existing_batch() -> list[JobSnapshot] | None:
        previous = db.scalar(
            select(Job).where(Job.user_id == user.id, Job.operation_key == operation)
        )
        if previous is None:
            return None
        if previous.prompt_removed:
            raise HTTPException(410, "Запрос удалён")
        existing_jobs = batch_members(db, previous)
        if (
            previous.batch_index not in (None, 0)
            or [member.fingerprint for member in existing_jobs] != fingerprints
        ):
            raise HTTPException(409, "Этот ключ уже относится к другому запросу")
        return job_snapshots(db, existing_jobs, user.id)

    if (existing_jobs := find_existing_batch()) is not None:
        return existing_jobs
    if locked_user.generation_suspended_at is not None:
        raise HTTPException(403, "Доступ к генерации приостановлен")
    aspect = parameters.aspect
    if parameters.model == "video_sr" and (
        settings.provider != "kandinsky" or not settings.video_sr_enabled
    ):
        raise HTTPException(503, "Повышение разрешения сейчас недоступно")
    if parameters.input_id:
        asset = owned(db, Asset, parameters.input_id, user)
        if parameters.model == "video_sr":
            if asset.mime != "video/mp4" or asset.size > settings.max_upload_bytes:
                raise HTTPException(422, "Для повышения разрешения нужен MP4")
            info = asset.preview_info or preview_metadata(
                settings.media_dir / asset.filename, asset.mime
            )
            width, height = info["width"], info["height"]
            if not (
                1 <= info["duration_seconds"] <= 5.1
                and 128 <= min(width, height)
                and max(width, height) <= 960
                and width * height <= 960 * 540
            ):
                raise HTTPException(422, "Нужен MP4 от 1 до 5 секунд, до 960 × 540 по площади")
            asset.preview_info = info
            divisor = gcd(width, height)
            aspect = f"{width // divisor}:{height // divisor}"
        elif parameters.model != "video" or not asset.mime.startswith("image/"):
            raise HTTPException(422, "Фото можно оживить моделью Video Lite")
    check_queue_capacity(db, user.id, amount)
    batch_id = str(uuid.uuid4()) if amount > 1 else None
    created_at = time.time()
    jobs = [
        Job(
            **(asdict(item) | {"aspect": aspect}),
            id=batch_id if batch_id and index == 0 else str(uuid.uuid4()),
            batch_id=batch_id,
            batch_index=index if batch_id else None,
            user_id=user.id,
            operation_key=operation
            if index == 0
            else str(uuid.uuid5(uuid.UUID(operation), f"result:{index}")),
            fingerprint=fingerprints[index],
            provider=settings.provider,
            created_at=created_at + index / 1_000_000,
        )
        for index, item in enumerate(job_requests)
    ]
    if not thread.custom_title and thread.title == "Новый чат":
        thread.title = parameters.prompt[:80]
    thread.activity_at = created_at
    db.add_all(jobs)
    try:
        notify(db, "queue")
        notify(db, "owner:" + user.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        if (existing_jobs := find_existing_batch()) is not None:
            return existing_jobs
        raise HTTPException(409, "Конфликт операции") from None
    return job_snapshots(db, jobs, user.id)
