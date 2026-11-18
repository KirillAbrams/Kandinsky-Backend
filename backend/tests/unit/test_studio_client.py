# Проверяет передачу частичного файла и сохранение причины ошибки между основным сервером и
# телефоном. Все сетевые ответы подставляются внутри проверки.

import asyncio
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.Services.Mobile.studio import StudioClient


# Просит только часть файла и сверяет ответ, границы и байты. Это обнаруживает потерю перемотки
# или замену частичного ответа скачиванием целого файла.
def test_media_range_is_forwarded_without_loading_the_whole_file():
    asset = UUID(int=1)

    # Проверяет путь, сеанс, требуемые байты и сохранение исходных байтов без перекодировки.
    # Возвращает ровно тот отрезок, который запрашивался.
    def upstream(request):
        assert request.url.path == f"/v1/assets/{asset}"
        assert request.headers["authorization"] == "Bearer credential"
        assert request.headers["range"] == "bytes=1-3"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(206, content=b"abc", headers={"Content-Range": "bytes 1-3/5"})

    # Открывает частичный ответ и сверяет принятые поля и содержимое. Закрывает чтение даже при
    # ошибке, чтобы тест не оставлял открытый поток.
    async def read():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            response = await StudioClient(client).open_asset(asset, "credential", "bytes=1-3")
            try:
                assert response.status_code == 206
                assert response.headers["content-range"] == "bytes 1-3/5"
                assert await response.aread() == b"abc"
            finally:
                await response.aclose()

    asyncio.run(read())


# Передаёт отказ очереди через службу связи и проверяет, что телефон получает исходную причину.
# Иначе пользователь увидел бы общий сбой вместо подсказки дождаться заданий.
def test_server_rejection_reaches_the_phone():
    # Сверяет адрес команды и отвечает ограничением очереди с понятным текстом. Проверка
    # сравнивает его с ошибкой на стороне телефона.
    def upstream(request):
        assert request.url.path == "/v1/jobs"
        return httpx.Response(429, json={"detail": "Сначала дождитесь текущих генераций"})

    # Отправляет команду через клиент и читает исключение. Статус и объяснение должны сохраниться
    # при передаче.
    async def submit():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            with pytest.raises(HTTPException) as error:
                await StudioClient(client).request("POST", "/v1/jobs", "credential", json={})
            assert error.value.status_code == 429
            assert error.value.detail == "Сначала дождитесь текущих генераций"

    asyncio.run(submit())


# Имитирует задержку сети при открытии файла без ответа сервера. Проверяет отдельный отказ службы,
# чтобы телефон мог отличить временную проблему от отсутствия файла.
def test_unavailable_media_transport_returns_a_service_error():
    # Сверяет, что запрошен нужный файл, и завершает попытку по времени. Ответ со статусом
    # намеренно не создаётся.
    def upstream(request):
        assert request.url.path == f"/v1/assets/{UUID(int=1)}"
        raise httpx.ReadTimeout("isolated slow response", request=request)

    # Открывает файл через клиент с недоступной сетью. Проверяет код временного отказа и текст о
    # скачивании.
    async def read():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            with pytest.raises(HTTPException) as error:
                await StudioClient(client).open_asset(UUID(int=1), "fixture", None)
            assert error.value.status_code == 503
            assert error.value.detail == "Не удалось скачать файл"

    asyncio.run(read())
