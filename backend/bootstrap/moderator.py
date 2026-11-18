# Закрытая служба принимает небольшие наборы текста и кадров, проверяет их моделями и возвращает
# решения в том же порядке. Ограничения входа и одна работа за раз сдерживают нагрузку.

"""Закрытый сервер проверки текста и кадров."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.Http.errors import invalid_classifier_input
from app.Http.Middleware.body_limit import BodyLimit
from app.Services.Moderation.classifier import LocalClassifier
from app.Services.Moderation.content_filter import FRAME_BATCH
from routes.moderation import register_moderation_routes


# Готовит закрытый сервер с пределом тела и загрузкой моделей при старте. Без ключа он не
# запускается, иначе вычислительную службу можно было бы вызвать без проверки.
def create_app(token, load_classifier):
    if not token:
        raise ValueError("The private classifier needs a token")

    # Загружает модели в стороннем потоке и создаёт блокировку для их вызова. Запросы начинают
    # приниматься после подготовки, поэтому первый вызов не застаёт незагруженную модель.
    @asynccontextmanager
    async def lifespan(app):
        app.state.classifier = await asyncio.to_thread(load_classifier)
        app.state.serial = asyncio.Lock()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, upload_bytes=0, small_bytes=3 * 1024 * 1024)

    app.add_exception_handler(RequestValidationError, invalid_classifier_input)
    register_moderation_routes(app, token)
    return app


# Читает местные пути моделей, размер группы, устройство и ключ. Проверяет наличие папок и
# допустимый размер до старта, чтобы ошибка настройки проявилась сразу.
def from_environment():
    text = Path(os.environ["STUDIO_MODERATION_TEXT_MODEL"])
    images = Path(os.environ["STUDIO_MODERATION_IMAGE_MODEL"])
    batch_size = int(os.environ.get("STUDIO_MODERATION_IMAGE_BATCH", "1"))
    if not text.is_dir() or not images.is_dir() or not 1 <= batch_size <= FRAME_BATCH:
        raise ValueError("Use two local model directories and a supported image batch size")
    return create_app(
        os.environ["STUDIO_MODERATION_TOKEN"],
        lambda: LocalClassifier(
            text, images, os.environ.get("STUDIO_MODERATION_DEVICE"), batch_size
        ),
    )
