# Доставляет отложенные удаления вычислительной машине отдельно от обработки новых заданий. При
# недоступности машины записи остаются в базе до следующей попытки.

"""Повторяет подтверждённое удаление GPU независимо от текущей генерации."""

import asyncio
import logging

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.Exceptions.generation import GpuUnavailable
from app.Models import PendingGpuRemoval
from app.Services.Gpu.provider import GpuProvider

log = logging.getLogger(__name__)


async def clear_gpu_removals(factory: sessionmaker[Session], provider: GpuProvider) -> bool:
    """Доставляет одну порцию удалений и закрывает только подтверждённые отметки."""
    with factory() as db:
        items = list(
            db.scalars(
                select(PendingGpuRemoval)
                .order_by(PendingGpuRemoval.created_at, PendingGpuRemoval.job_id)
                .limit(128)
            )
        )
    for item in items:
        try:
            await provider.erase(item.job_id, item.run_id)
        except (GpuUnavailable, httpx.HTTPError, OSError, ValueError) as error:
            log.warning("GPU erasure interrupted (%s)", type(error).__name__)
            continue
        with factory.begin() as db:
            db.execute(
                delete(PendingGpuRemoval).where(
                    PendingGpuRemoval.job_id == item.job_id,
                    PendingGpuRemoval.run_id == item.run_id,
                    PendingGpuRemoval.created_at == item.created_at,
                )
            )
    return bool(items)


async def deliver_gpu_removals(
    factory: sessionmaker[Session],
    provider: GpuProvider,
    changed: asyncio.Event,
    retry_seconds: float = 15,
) -> None:
    """Ждёт сигнал очистки. Неудачную доставку повторяет с задержкой."""
    while True:
        changed.clear()
        try:
            pending = await clear_gpu_removals(factory, provider)
        except (SQLAlchemyError, OSError) as error:
            log.warning("GPU erasure database unavailable (%s)", type(error).__name__)
            pending = True
        if not pending:
            await changed.wait()
        else:
            try:
                await asyncio.wait_for(changed.wait(), timeout=retry_seconds)
            except TimeoutError:
                pass
