# Здесь телефон получает обложки, оригиналы и нужные отрезки видео. Перед скачиванием служба
# проверяет владельца и местную отметку удаления.

"""Выдача оригиналов, превью и запрошенных частей файла."""

import base64
import re
import uuid
from collections.abc import Callable

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter


class MobileMediaController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Подключает скачивание по номеру файла и передаёт обработчику общую проверку входа и хранилище
    # отметок. Это связывает выдачу файла с тем же владельцем, который видит его в истории.
    def __init__(self, adapter: Callable[[], Adapter], state: State) -> None:
        self._adapter = adapter
        self._state = state

    # Отсекает неправильные и удалённые номера. Для номера обложки возвращает картинку, а для
    # оригинала пересылает запрошенный отрезок. Телефон может открыть полную запись или перемотать
    # её без повторной загрузки всего файла.
    async def media(self, request: Request, blob: str):
        owner, token = await self._adapter().owner(request.headers)
        if re.fullmatch(r"[0-9a-f]{32}", blob) is None or self._state.media_removed(owner, blob):
            raise HTTPException(404, "Файл не найден")
        asset = self._state.preview(owner, blob)
        if asset:
            info = await self._adapter().studio.request(
                "GET", f"/v1/assets/{asset}/metadata", token
            )
            return Response(base64.b64decode(info["poster"]), media_type="image/jpeg")
        reply = await self._adapter().studio.open_asset(
            uuid.UUID(blob), token, request.headers.get("range")
        )
        forwarded = {
            key: reply.headers[key]
            for key in (
                "content-range",
                "content-length",
                "accept-ranges",
                "content-type",
            )
            if key in reply.headers
        }

        # Выдаёт файл частями и закрывает соединение с основным сервером в конце или при
        # прерывании. Иначе уход с экрана мог бы оставить скачивание открытым.
        async def stream():
            try:
                async for part in reply.aiter_bytes():
                    yield part
            finally:
                await reply.aclose()

        return StreamingResponse(stream(), status_code=reply.status_code, headers=forwarded)
