"""Принимает наборы текста и кадров для закрытой службы проверки."""

import base64
import binascii
import io
import secrets
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from PIL import Image

from app.Exceptions.moderation import FilterUnavailable
from app.Http.Requests.moderation import ImageBatch, TextBatch
from app.Services.Moderation.classifier import classify_batch
from app.Services.Moderation.content_filter import MAX_FRAME_BYTES, MAX_SIDE


def open_frame(value):
    decoded = base64.b64decode(value, validate=True)
    if not 1 <= len(decoded) <= MAX_FRAME_BYTES:
        raise ValueError("Invalid frame size")
    with Image.open(io.BytesIO(decoded)) as image:
        if (
            image.format != "JPEG"
            or max(image.size) > MAX_SIDE
            or getattr(image, "n_frames", 1) != 1
        ):
            raise ValueError("Invalid frame format")
        return image.convert("RGB")


class ModerationController:
    """Проверяет доступ и передаёт данные готовой модели."""

    def __init__(self, app: FastAPI, token: str) -> None:
        self._app = app
        self._token = token

    # Сверяет заголовок доступа до вызова модели.
    def authorize(self, authorization: Annotated[str, Header()] = ""):
        if not secrets.compare_digest(authorization.encode(), ("Bearer " + self._token).encode()):
            raise HTTPException(401, "Unauthorized")

    async def _classify(self, method, items):
        try:
            verdicts = await classify_batch(self._app.state.serial, method, items)
        except FilterUnavailable:
            raise HTTPException(503, "Classifier unavailable") from None
        return {"allowed": verdicts}

    # Подтверждает готовность после проверки доступа.
    @staticmethod
    async def health():
        return {"ok": True}

    # Передаёт текст модели и сохраняет порядок ответов.
    async def text(self, body: TextBatch):
        return await self._classify(self._app.state.classifier.text, body.items)

    # Проверяет изображения и передаёт модели готовые кадры.
    async def images(self, body: ImageBatch):
        try:
            frames = [open_frame(item) for item in body.items]
        except (binascii.Error, OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, "Invalid classifier image") from None
        return await self._classify(self._app.state.classifier.images, frames)
