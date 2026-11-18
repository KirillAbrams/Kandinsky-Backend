# Здесь описана передача запросов основному серверу. Обычные ответы читаются целиком, а готовые
# файлы передаются частями, чтобы не держать в памяти весь ролик.

"""Запросы службы связи к основному серверу от имени текущего пользователя."""

import httpx
from fastapi import HTTPException


class StudioClient:
    # Принимает уже открытое соединение. Это сохраняет единые настройки связи и позволяет
    # проверкам передать своё соединение.
    def __init__(self, client):
        self.client = client

    # Добавляет ключ входа, пересылает запрос и разбирает ответ. При ограничении частоты сохраняет
    # время ожидания из ответа основного сервера, чтобы вызывающая сторона знала, когда повторить
    # запрос.
    async def request(self, method, path, token=None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            reply = await self.client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise HTTPException(503, "Кандинский не отвечает") from error
        if reply.is_error:
            try:
                detail = reply.json().get("detail")
            except ValueError:
                detail = None
            retry_after = reply.headers.get("Retry-After")
            raise HTTPException(
                reply.status_code,
                detail if isinstance(detail, str) else "Запрос не принят",
                headers={"Retry-After": retry_after}
                if reply.status_code == 429 and retry_after
                else None,
            )
        return reply.json() if reply.content else None

    # Открывает скачивание под ключом владельца и передаёт нужный диапазон байтов. Это позволяет
    # перематывать видео без скачивания всего файла. При ошибке сразу закрывает ответ, чтобы
    # освободить соединение.
    async def open_asset(self, asset_id, token, requested_range):
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
        if requested_range:
            headers["Range"] = requested_range
        request = self.client.build_request("GET", f"/v1/assets/{asset_id}", headers=headers)
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise HTTPException(503, "Не удалось скачать файл") from error
        if response.is_error:
            await response.aclose()
            raise HTTPException(response.status_code, "Файл не найден")
        return response
