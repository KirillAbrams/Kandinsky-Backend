# Проверяет ограничитель размера отдельно от сетевого сервера. Подставные отправитель и получатель
# показывают, в какой момент проверка прекращает чтение.

import asyncio

import pytest
from fastapi import HTTPException

from app.Http.Middleware.body_limit import BodyLimit


# Передаёт неправильную длину и проверяет ошибку запроса. Ни чтение тела, ни запуск приложения не
# должны начаться.
def test_request_with_invalid_content_length_stops_before_dispatch():
    messages = []

    # Сразу останавливает сценарий, если запрос дошёл до приложения. Это показывает нарушение
    # порядка даже при правильном ответе.
    async def app(*args):
        raise AssertionError("Invalid body reached the application")

    # Считает любую попытку чтения тела ошибкой. По объявленной длине уже видно, что продолжать
    # чтение не нужно.
    async def receive():
        raise AssertionError("Invalid body was read")

    # Складывает части ответа, чтобы сценарий смог проверить статус и сообщение без сетевого
    # подключения.
    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "path": "/v1/threads",
        "headers": [(b"content-length", b"unknown")],
    }
    asyncio.run(BodyLimit(app, upload_bytes=256)(scope, receive, send))
    assert messages[0]["status"] == 400
    assert b"Invalid Content-Length" in messages[1]["body"]


# Проверяет, что сообщение о закрытии проходит через ограничитель без изменений. Иначе приложение
# не узнало бы, что соединение уже потеряно.
def test_finished_request_reader_passes_on_disconnect():
    expected = {"type": "http.disconnect"}

    # Возвращает подготовленный сигнал закрытия вместо данных тела. Так проверка точно
    # воспроизводит последний шаг запроса.
    async def receive():
        return expected

    # Сверяет сигнал с ожидаемым и возвращает собственную отметку окончания. Сценарий может
    # подтвердить, что ограничитель передал сообщение приложению и вернул итог.
    async def app(scope, bounded_receive, send):
        assert await bounded_receive() == expected
        return "disconnected"

    scope = {"type": "http", "path": "/v1/threads", "headers": []}
    assert asyncio.run(BodyLimit(app, upload_bytes=256)(scope, receive, None)) == "disconnected"


# Передаёт две короткие части без длины, сумма которых превышает допустимое. Отказ должен
# возникнуть на второй части, чтобы нельзя было обойти предел дроблением запроса.
def test_body_limit_counts_bytes_across_streamed_chunks_without_content_length():
    chunks = iter([b"ab", b"cde"])

    # Даёт следующий кусок при каждом чтении, как при постепенном приходе файла. После прогона по
    # списку видно, какие части действительно прочли.
    async def receive():
        return {"type": "http.request", "body": next(chunks)}

    # Сверяет, что первая часть прошла, и запрашивает вторую. Ошибка должна появиться при этом
    # чтении второй части сразу после превышения предела.
    async def app(scope, read, send):
        assert (await read())["body"] == b"ab"
        await read()

    scope = {"type": "http", "path": "/v1/threads", "headers": []}
    with pytest.raises(HTTPException) as error:
        asyncio.run(BodyLimit(app, upload_bytes=256, small_bytes=4)(scope, receive, None))
    assert error.value.status_code == 413
    assert list(chunks) == []
