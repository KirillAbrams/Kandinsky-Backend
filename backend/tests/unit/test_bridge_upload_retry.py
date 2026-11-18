# Проверяет, что после обрыва ответа служба связи может продолжить отправку исходника и привязать
# уже принятый файл. Часы и ответы основного сервера заменены управляемыми.

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.Services.Database.Mobile.state import State
from app.Services.Mobile import adapter
from tests.support.fixtures import MOBILE_FIXTURES


# Теряет первый ответ после приёма байтов и возобновляет работу с прежней базой. Проверяет
# сохранение или замену разрешения по сроку, повторную привязку файла и отказ после удаления.
@pytest.mark.parametrize("expired", [False, True], ids=["reuse-permit", "renew-permit"])
def test_source_upload_recovers_lost_reply_and_links_completed_asset(
    tmp_path, monkeypatch, expired
):
    video = (MOBILE_FIXTURES / "media/video-01.mp4").read_bytes()
    fingerprint = hashlib.sha256(video).hexdigest()
    owner, thread, second_thread, asset = (str(UUID(int=i)) for i in range(100, 104))
    operation = UUID(int=104)
    permits = ["/v1/uploads/" + "A" * 43, "/v1/uploads/" + "B" * 43]
    requests = []
    issued = 0
    uploads = 0
    clock = [1000]
    monkeypatch.setattr(adapter, "time", SimpleNamespace(time=lambda: clock[0]))

    # Выдаёт проверочные разрешения, сверяет полученные байты и имитирует пропажу первого ответа.
    # Запоминает обращения, чтобы обнаружить лишнюю отправку или потерянную привязку.
    def upstream(request):
        nonlocal issued, uploads
        path = request.url.path
        requests.append(path)
        assert request.method == "POST"
        if path == "/v1/uploads/ticket":
            assert request.url.params.multi_items() == [("thread_id", thread)]
            assert request.headers["Authorization"] == "Bearer isolated-session"
            permit = permits[issued]
            issued += 1
            return httpx.Response(200, json={"path": permit, "expires_in": 60})
        if path in permits:
            assert video in request.content
            uploads += 1
            if uploads == 1:
                raise httpx.ReadError("Проверочный обрыв после загрузки")
            return httpx.Response(201, json={"id": asset, "size": len(video)})
        assert path == f"/v1/threads/{second_thread}/sources/{asset}"
        assert request.headers["Authorization"] == "Bearer isolated-session"
        return httpx.Response(204)

    # Открывает новый экземпляр службы после обрыва и проверяет, что он читает прежнее разрешение
    # с диска. После восстановления сверяет порядок запросов и запрет заново создать удалённый
    # файл.
    async def recover():
        state = State(tmp_path / "bridge.sqlite")
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            file = ("source.mp4", video, "video/mp4")
            bridge = adapter.Adapter(client, state)
            with pytest.raises(HTTPException) as error:
                await bridge.studio_upload(
                    owner, "isolated-session", operation, fingerprint, file, thread=thread
                )
            assert error.value.status_code == 503
            assert state.studio_upload(owner, str(operation)) == (
                fingerprint,
                permits[0],
                1060,
                None,
                None,
            )
            clock[0] = 1061 if expired else 1010
            # Повтор через новый экземпляр читает сохранённое разрешение из местной базы.
            bridge = adapter.Adapter(client, State(state.path))
            blob = UUID(asset).hex
            assert await bridge.studio_upload(
                owner, "isolated-session", operation, fingerprint, file, thread=thread
            ) == (blob, len(video))
            assert await bridge.studio_upload(
                owner, "isolated-session", operation, fingerprint, file, thread=second_thread
            ) == (blob, len(video))
            expected = ["/v1/uploads/ticket", permits[0]]
            expected += ["/v1/uploads/ticket", permits[1]] if expired else [permits[0]]
            expected.append(f"/v1/threads/{second_thread}/sources/{asset}")
            assert requests == expected
            state.erase_assets(owner, [{"blobId": blob}])
            with pytest.raises(HTTPException) as error:
                await bridge.studio_upload(owner, "isolated-session", operation, fingerprint, file)
            assert error.value.status_code == 410
            assert requests == expected

    asyncio.run(recover())


# Сравнивает отказ неподдерживаемому файлу и отказ от неправильного разрешения. В обоих случаях
# служба не должна сохранять начатую отправку.
def test_source_upload_requires_supported_media_and_a_valid_upstream_permit(tmp_path):
    owner = str(UUID(int=100))
    operation = UUID(int=104)
    video = (MOBILE_FIXTURES / "media/video-01.mp4").read_bytes()
    fingerprint = hashlib.sha256(video).hexdigest()
    requests = []

    # Возвращает адрес разрешения без обязательного ключа. Записывает путь, чтобы проверка
    # увидела, успела ли служба обратиться за разрешением.
    def upstream(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"path": "/v1/uploads/", "expires_in": 60})

    # Отправляет оба проверочных варианта и сверяет вид отказа и отсутствие записи в базе. Запрос
    # за разрешением ожидается только для поддерживаемого файла.
    async def upload():
        state = State(tmp_path / "bridge.sqlite")
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            bridge = adapter.Adapter(client, state)
            for mime, status in [("audio/mpeg", 422), ("video/mp4", 502)]:
                with pytest.raises(HTTPException) as error:
                    await bridge.studio_upload(
                        owner, "isolated-session", operation, fingerprint, ("source", video, mime)
                    )
                assert error.value.status_code == status
                assert state.studio_upload(owner, str(operation)) is None
            assert requests == ["/v1/uploads/ticket"]

    asyncio.run(upload())
