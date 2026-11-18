"""Запускает рабочий процесс генерации и фоновой очистки."""

import asyncio
import logging
import signal
import time
from contextlib import suppress

from sqlalchemy import delete

from app.Jobs.erasure_delivery import deliver_gpu_removals
from app.Models import ErasedSession
from app.Providers.generation import make_provider
from app.Services.Chats.content_removal import clear_pending_media
from app.Services.Database.connection import database
from app.Services.Database.notifications import listen
from app.Services.Generation.process_generation import process_one
from app.Services.Generation.queue import next_claim_time
from app.Services.Gpu.provider import GpuProvider
from config.studio import Settings


async def main() -> None:
    """Запускает обработку, ожидание новых заданий и фоновую очистку файлов."""
    settings = Settings()
    engine, factory = database(settings.database_url)
    provider = make_provider(settings)
    queue_changed = asyncio.Event()
    erasure_changed = asyncio.Event()

    # Будит обработку заданий и удалений по сигналу очереди или переподключения. Без этого работа
    # ждала бы следующей проверки по времени даже после появления новой записи.
    def wake_worker(change: str) -> None:
        if change in {"queue", "catch_up"}:
            queue_changed.set()
            erasure_changed.set()

    listener = None
    if engine.dialect.name == "postgresql":
        listener = asyncio.create_task(listen(settings.database_url, wake_worker))
    erasure_task = None
    if settings.gpu_token:
        erasure_task = asyncio.create_task(
            deliver_gpu_removals(
                factory, GpuProvider(settings.gpu_url, settings.gpu_token), erasure_changed
            )
        )
    cleanup_at = 0.0
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        while True:
            if time.monotonic() >= cleanup_at:
                with factory() as db:
                    db.execute(delete(ErasedSession).where(ErasedSession.expires_at <= time.time()))
                    db.commit()
                    clear_pending_media(db, settings.media_dir)
                cleanup_at = time.monotonic() + 60
            # Сбрасываем сигнал до чтения базы. Новое задание, пришедшее во время запроса, разбудит цикл.
            queue_changed.clear()
            if await process_one(factory, settings, provider):
                if listener is None:
                    erasure_changed.set()
                continue
            if listener is None:
                erasure_changed.set()
                await asyncio.sleep(1)
                continue
            next_claim = next_claim_time(factory)
            timeout = max(0, cleanup_at - time.monotonic())
            if next_claim is not None:
                timeout = min(timeout, max(0, next_claim - time.time()))
            try:
                await asyncio.wait_for(queue_changed.wait(), timeout)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        if listener is not None:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener
        if erasure_task is not None:
            erasure_task.cancel()
            with suppress(asyncio.CancelledError):
                await erasure_task
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
