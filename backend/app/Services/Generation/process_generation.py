"""Выполняет одну генерацию, продлевает право на обработку и сохраняет результат."""

import asyncio
import logging
import subprocess
import uuid

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.Contracts.generation import Provider
from app.Enums.moderation import BLOCKED_GENERATION
from app.Exceptions.generation import GpuUnavailable
from app.Exceptions.moderation import ContentDenied, FilterUnavailable
from app.Models import Asset, Job
from app.Providers.generation import make_provider
from app.Services.Generation.queue import claim, record_progress, release_lease, renew
from app.Services.Gpu.provider import GpuProvider
from app.Services.Media.files import preview_metadata
from app.Services.Moderation.content_filter import ContentFilter
from config.studio import Settings

log = logging.getLogger("studio.worker")


async def process_one(
    factory: sessionmaker[Session], settings: Settings, provider: Provider | None = None
) -> bool:
    """Обрабатывает одно задание и сохраняет файл только при действующем праве на запись."""
    job = claim(factory, settings.lease_seconds)
    if not job:
        return False
    provider = provider or make_provider(settings)
    filter_service = ContentFilter(settings) if settings.moderation_mode == "required" else None
    suffix = ".png" if job.model == "image" else ".mp4"
    path = settings.media_dir / (str(uuid.uuid4()) + suffix)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    with factory() as db:
        reference_asset = db.get(Asset, job.input_id) if job.input_id else None
        reference = settings.media_dir / reference_asset.filename if reference_asset else None

    # Проверяет настройки и наличие исходника, при необходимости отправляет содержимое на
    # проверку, затем просит исполнителя записать файл. Готовый файл проверяет ещё раз и пробует
    # сделать обложку, чтобы не выдать запрещённое или пустое содержимое.
    async def generate() -> tuple[str, dict[str, object] | None]:
        if job.provider != settings.provider:
            raise ValueError("Queued provider differs from worker configuration")
        if job.input_id and reference_asset is None:
            raise ValueError("Generation source is unavailable")
        if filter_service:
            await filter_service.check_input(
                job.prompt,
                job.negative_prompt,
                reference,
                reference_asset.mime if reference_asset else None,
            )
        if isinstance(provider, GpuProvider):
            mime = await provider.generate(
                job,
                path,
                reference,
                on_progress=lambda value: record_progress(factory, job.id, job.lease_owner, value),
            )
        else:
            mime = await provider.generate(job, path, reference)
        if not path.exists() or path.stat().st_size == 0:
            raise ValueError("Provider returned empty output")
        if filter_service:
            await filter_service.check_output(path, mime)
        try:
            preview = await asyncio.to_thread(preview_metadata, path, mime)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            log.warning("job=%s preview=%s", job.id, type(exc).__name__)
            preview = None
        return mime, preview

    task = asyncio.create_task(generate())
    try:
        while not task.done():
            done, _ = await asyncio.wait([task], timeout=max(0.1, settings.lease_seconds / 3))
            if done:
                break
            if not renew(factory, job.id, job.lease_owner, settings.lease_seconds):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                with factory() as db:
                    current = db.get(Job, job.id)
                    cancelled = current is None or (
                        current.status == "cancelled" and current.run_id == job.run_id
                    )
                if cancelled:
                    try:
                        await provider.cancel(job.run_id or job.id)
                    except httpx.HTTPError:
                        log.info("job=%s gpu_cancel_unavailable", job.id)
                return True
        mime, preview = await task
        with factory() as db:
            asset = Asset(
                user_id=job.user_id,
                mime=mime,
                size=path.stat().st_size,
                filename=path.name,
                generated=True,
                preview_info=preview,
            )
            db.add(asset)
            db.flush()
            # Перед публикацией ещё раз проверяем владельца и статус. Иначе старый обработчик мог бы вернуть
            # отменённый результат.
            changed = db.execute(
                update(Job)
                .where(
                    Job.id == job.id, Job.status == "running", Job.lease_owner == job.lease_owner
                )
                .values(status="succeeded", output_id=asset.id, lease_owner=None, progress=100)
            )
            if changed.rowcount:
                db.commit()
            else:
                db.rollback()
        log.info("job=%s provider=%s finished", job.id, job.provider)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release_lease(factory, job.id, job.lease_owner)
        raise
    except (GpuUnavailable, httpx.TransportError) as exc:
        log.info("job=%s gpu_unavailable=%s", job.id, type(exc).__name__)
        release_lease(factory, job.id, job.lease_owner, delay=15)
        return False
    except Exception as exc:
        log.warning("job=%s failure=%s", job.id, type(exc).__name__)
        moderated = isinstance(exc, ContentDenied)
        if moderated:
            message = BLOCKED_GENERATION
        elif isinstance(exc, FilterUnavailable):
            message = "Проверка безопасности временно недоступна. Повторите запрос позже."
        else:
            message = "Генерация прервалась. Проверьте сервер и повторите запрос."
        with factory() as db:
            db.execute(
                update(Job)
                .where(
                    Job.id == job.id, Job.status == "running", Job.lease_owner == job.lease_owner
                )
                .values(
                    status="failed",
                    error=message,
                    moderated=moderated,
                    lease_owner=None,
                )
            )
            db.commit()
    finally:
        # Файл мог появиться до записи результата в базу. Удаляем его только после проверки ссылок.
        with factory() as db:
            used = db.query(Asset).filter_by(filename=path.name).first()
        if not used:
            path.unlink(missing_ok=True)
    return True
