# Здесь принимаются файлы от телефонов и выдаётся список недавних исходников. Запрос проверяет
# размер и содержимое, а другим устройствам владельца передаётся ход отправки.

"""Принимает исходный файл и сообщает ход передачи другим телефонам владельца."""

import hashlib
import uuid
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.upload_delivery import MAX_UPLOAD, UploadDelivery


class MobileUploadsController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Подключает отправку файлов и привязку готовых исходников к чатам. Обработчики используют общую
    # проверку владельца и общий список текущих загрузок.
    def __init__(
        self, adapter: Callable[[], Adapter], state: State, delivery: UploadDelivery
    ) -> None:
        self._adapter = adapter
        self._state = state
        self._delivery = delivery

    # Привязывает уже загруженный файл к чату вошедшего пользователя на основном сервере. Можно
    # повторно выбрать исходник без отправки тех же байтов.
    async def bind_recent(self, request: Request, chat: int, blob: uuid.UUID):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        thread = service.thread(owner, chat)
        await service.studio.request("POST", f"/v1/threads/{thread}/sources/{blob}", token)

    # Возвращает до пяти недавних файлов подходящего размера и формата. Это оставляет в списке те
    # исходники, которые приложение может выбрать для следующей генерации.
    async def recent_uploads(self, request: Request):
        service = self._adapter()
        _, token = await service.owner(request.headers)
        rows = await service.studio.request("GET", "/v1/uploads", token)
        return {
            "uploads": [
                {"blobId": uuid.UUID(row["id"]).hex, "mime": row["mime"], "byteSize": row["size"]}
                for row in rows
                if row["mime"] in {"image/jpeg", "video/mp4"} and 0 < row["size"] <= MAX_UPLOAD
            ][:5],
        }

    # Читает файл по частям с ограничением размера, затем сверяет длину и контрольную сумму.
    # Передаёт проверенный файл основному серверу и убирает строку хода отправки даже при ошибке,
    # чтобы она не зависла на другом устройстве.
    async def upload(self, request: Request, upload: uuid.UUID):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        source = request.headers.get("x-kandinsky-source") == "1"
        chat = request.headers.get("x-kandinsky-chat")
        parent = request.headers.get("x-kandinsky-parent")
        try:
            thread = service.thread(owner, int(chat)) if source and chat else None
            parent = str(uuid.UUID(parent)) if parent else None
        except ValueError as error:
            raise HTTPException(422, "Неверный номер чата или исходника") from error
        declared = request.headers.get("content-length")
        try:
            total = int(declared) if declared is not None else None
        except ValueError as error:
            raise HTTPException(422, "Неверный размер файла") from error
        if total is not None and not 0 < total <= MAX_UPLOAD:
            raise HTTPException(413 if total > MAX_UPLOAD else 422, "Неверный размер файла")
        mime = request.headers.get("content-type", "application/octet-stream")
        device = request.headers.get("x-kandinsky-device", "")
        try:
            device = str(uuid.UUID(device)) if device else ""
        except ValueError as error:
            raise HTTPException(422, "Неверный номер устройства") from error
        attempt = (
            self._delivery.begin(owner, str(upload), device, total, mime[:80])
            if source and total
            else None
        )
        content = bytearray()
        try:
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_UPLOAD:
                    raise HTTPException(413, "Файл больше 20 МБ")
                content.extend(chunk)
                if attempt:
                    self._delivery.progress(attempt, len(content))
            fingerprint = hashlib.sha256(content).hexdigest()
            if not content or fingerprint != request.headers.get("x-content-sha256"):
                raise HTTPException(422, "Содержимое файла не совпадает")
            if total is not None and total != len(content):
                raise HTTPException(422, "Размер файла не совпадает")
            file = str(upload), bytes(content), mime
            if source:
                blob, size = await service.studio_upload(
                    owner, token, upload, fingerprint, file, thread=thread
                )
            else:
                result = await service.studio.request(
                    "POST",
                    "/v1/assets",
                    token,
                    files={"file": file},
                    params={"parent": parent} if parent else {},
                )
                blob, size = uuid.UUID(result["id"]).hex, result["size"]
            return {
                "blobId": blob,
                "byteSize": size,
                "ownerId": self._state.identity(owner, "user", owner),
            }
        finally:
            if attempt:
                self._delivery.finish(attempt)
