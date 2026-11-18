# Принимает запросы о чате, загрузках и создании медиа. Запускает подписку на изменения базы,
# чтобы служба связи узнавала об удалениях. Само создание файлов выполняет отдельный процесс.

"""Сервер принимает запросы, проверяет доступ к данным и сохраняет изменения
Задания обрабатывает отдельный процесс"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.Http.Middleware.body_limit import BodyLimit
from app.Http.Middleware.request_id import request_id
from app.Models import Base
from app.Services.Chats.content_removal import clear_pending_media
from app.Services.Database.connection import database
from app.Services.Database.notifications import RemovalFeed, forward_local_commits, listen
from config.studio import Settings
from routes.api import register_studio_routes

log = logging.getLogger("studio.api")


# Собирает сервер из настроек, базы и обработчиков запросов. При проверках можно передать
# отдельную базу и подставить отправку писем, чтобы не затрагивать рабочие данные и почту.
def create_app(*, send_recovery=None, send_registration=None, **overrides) -> FastAPI:
    """Собирает сервер, хранилище и способы входа из одной конфигурации"""
    settings = Settings(**overrides)
    engine, factory = database(settings.database_url)

    feed = RemovalFeed()
    if engine.dialect.name == "sqlite":
        forward_local_commits(factory, feed)

    # Готовит папку файлов, пробует закончить отложенные удаления и открывает подписку на события
    # базы. При остановке отменяет подписку и закрывает соединения, иначе они могли бы остаться
    # открытыми.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        if settings.database_url.startswith("sqlite"):
            Base.metadata.create_all(engine)
        with factory() as db:
            clear_pending_media(db, settings.media_dir)
        listener = None
        if engine.dialect.name == "postgresql":
            listener = asyncio.create_task(listen(settings.database_url, feed.publish))
        try:
            yield
        finally:
            if listener is not None:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener
            engine.dispose()

    app = FastAPI(title="Kandinsky Studio", version="0.1.0", lifespan=lifespan)
    app.state.factory, app.state.settings = factory, settings
    app.state.removal_feed = feed
    app.add_middleware(BodyLimit, upload_bytes=settings.max_upload_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    app.middleware("http")(request_id)
    register_studio_routes(
        app,
        settings=settings,
        feed=feed,
        log=log,
        send_recovery=send_recovery,
        send_registration=send_registration,
    )

    return app


app = create_app()
