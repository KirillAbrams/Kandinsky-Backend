# Ограничивает размер запроса до разбора полей и файлов. Проверяет и объявленный размер, и реально
# полученные куски, поэтому отсутствие заголовка не снимает предел.

"""Ограничивает размер запроса до разбора вложения
Предел действует и без заранее указанного размера"""

from fastapi import HTTPException
from starlette.responses import JSONResponse


class BodyLimit:
    # Запоминает предел обычного запроса и предел отправки файла. Отдельные пределы позволяют
    # принимать медиа без увеличения допустимого размера остальных запросов.
    def __init__(self, app, upload_bytes: int, small_bytes: int = 32768):
        self.app, self.upload_bytes, self.small_bytes = app, upload_bytes, small_bytes

    # Выбирает предел по пути запроса, проверяет объявленную длину и передаёт приложению
    # ограниченное чтение. Запрос сверх предела отсекается до передачи целиком в обработчик.
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (
            self.upload_bytes + 65536
            if (scope["path"] == "/v1/assets" or scope["path"].startswith("/v1/uploads/"))
            else self.small_bytes
        )
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                scope, receive, send
            )
        if length > limit:
            return await JSONResponse({"detail": "Файл или запрос слишком большой"}, 413)(
                scope, receive, send
            )
        size = 0

        # Суммирует размер полученных частей тела и прерывает чтение при превышении. Так отправка
        # без объявленного размера тоже не сможет занять память без ограничения.
        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise HTTPException(413, "Файл или запрос слишком большой")
            return message

        return await self.app(scope, bounded_receive, send)
