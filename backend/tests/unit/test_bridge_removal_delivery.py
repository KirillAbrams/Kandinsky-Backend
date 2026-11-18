# Проверяет доставку изменений удаления, восстановление очереди и очистку подписок. Без этого
# отключившийся телефон мог бы остаться в списках, а действующий пропустить изменение.

import asyncio
from collections import deque
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.Http.Controllers import mobile_socket_controller as websocket
from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.removal_delivery import RemovalDelivery
from app.Services.Mobile.upload_delivery import UploadDelivery
from routes.mobile import register_websocket

OWNER = str(UUID(int=5))


# Чередует успех, временный отказ, догоняющую выдачу и отзыв сеанса. Сверяет новый старт после
# обрыва, пробуждение владельца и завершение дальнейших запросов.
def test_owner_feed_reconnects_with_fresh_cursor_and_stops_after_session_revocation(tmp_path):
    results = deque(
        [
            httpx.Response(200, json={"cursor": "one", "changes": []}),
            httpx.Response(503),
            httpx.Response(200, json={"cursor": "two", "changes": ["catch_up"]}),
            httpx.Response(401),
        ]
    )
    cursors = []

    # Записывает пришедшую отметку и выдаёт следующий подготовленный ответ. Список показывает,
    # была ли старая граница сброшена только в нужный момент.
    def respond(request):
        assert request.url.path == "/v1/jobs/native-removal-changes"
        cursors.append(request.url.params.get("cursor"))
        return results.popleft()

    # Запускает чтение потока с подставным сервером и ждёт окончания после отзыва. Проверяет
    # полученный сигнал и точный порядок отметок.
    async def deliver():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(respond)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            delivery = RemovalDelivery()
            signal = delivery.subscribe(OWNER)
            await asyncio.wait_for(delivery.follow_owner(adapter, "token", signal, 0), timeout=2)
            assert signal.is_set()
            assert cursors == ["", "one", "", "two"]
            assert not results

    asyncio.run(deliver())


# Воспроизводит сбой чтения и при необходимости последующее восстановление, затем закрывает канал.
# Проверяет уведомления и отсутствие подписчиков после закрытия.
@pytest.mark.parametrize("recover", [True, False])
def test_socket_retries_removal_signal_and_unsubscribes_uploads_on_close(tmp_path, recover):
    removals, uploads = RemovalDelivery(), UploadDelivery()
    app = FastAPI()
    state = State(tmp_path / "bridge.sqlite")

    # Возвращает постоянного проверочного владельца. Оба списка подписок должны использовать
    # именно этот номер.
    async def owner(headers):
        return OWNER, "token"

    # Запоминает попытки сверки и ломает вторую. Сценарий сможет отделить первое подключение от
    # временного сбоя и следующего запроса.
    async def sync(owner, token):
        results.append(owner)
        if len(results) == 2:
            raise HTTPException(503, "Исходный сервер недоступен")

    # Сообщает, что дополнительных сообщений нет. Тогда канал отдаёт только проверяемые
    # уведомления списка и загрузок.
    async def event_page(*args):
        return None

    results = []
    adapter = SimpleNamespace(owner=owner, sync_content_removals=sync, event_page=event_page)
    register_websocket(
        app,
        lambda: adapter,
        state,
        removals,
        uploads,
        poll_seconds=3600,
        erasure_key="local-test",
        erasure_poll_seconds=120,
    )

    with TestClient(app) as client, client.websocket_connect("/v1/ws") as ws:
        ws.send_json(
            {
                "type": "resume",
                "cursor": 0,
                "features": {"chatDirectory": True, "uploadProgress": True},
            }
        )
        assert ws.receive_json()["type"] == "resumed"
        assert ws.receive_json() == {"type": "kandinsky.uploads", "uploads": []}
        client.portal.call(removals.changed, OWNER)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        assert results == [OWNER, OWNER]
        if recover:
            client.portal.call(removals.changed, OWNER)
            assert ws.receive_json() == {"type": "kandinsky.catalog.changed"}
            uploaded = client.portal.call(
                uploads.begin, OWNER, "operation", "phone", 100, "image/jpeg"
            )
            assert ws.receive_json() == {
                "type": "kandinsky.uploads",
                "uploads": [
                    {
                        "id": "operation",
                        "deviceId": "phone",
                        "received": 0,
                        "total": 100,
                        "mime": "image/jpeg",
                    }
                ],
            }
            client.portal.call(uploads.finish, uploaded)
            assert ws.receive_json() == {"type": "kandinsky.uploads", "uploads": []}
    assert not uploads.sockets
    assert not removals.sockets


# Отзывает владельца при первой сверке и при обновлении уже открытого канала. В обоих случаях ждёт
# отдельное сообщение об удалении и затем закрытие.
def test_socket_closes_on_first_sync_after_owner_is_revoked(tmp_path):
    removals, uploads = RemovalDelivery(), UploadDelivery()
    app = FastAPI()

    # Подставляет прошедшего проверку владельца для открытия канала. Последующий отказ должен
    # появиться при очередной сверке.
    async def owner(headers):
        return OWNER, "token"

    # Сообщает об удалённом аккаунте, когда тест переключает признак. Так можно выбрать точный
    # момент потери доступа.
    async def sync(owner, token):
        if denied:
            raise HTTPException(401, "account_deleted")

    denied = False

    # Оставляет очередь сообщений пустой, чтобы в этом сценарии наблюдались только ответы
    # подключения и отзыва.
    async def event_page(*args):
        return None

    adapter = SimpleNamespace(owner=owner, sync_content_removals=sync, event_page=event_page)
    register_websocket(
        app,
        lambda: adapter,
        State(tmp_path / "bridge.sqlite"),
        removals,
        uploads,
        poll_seconds=3600,
        erasure_key="local-test",
        erasure_poll_seconds=120,
    )
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws") as ws:
            denied = True
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json() == {
                "type": "error",
                "code": "account_deleted",
                "retryable": False,
            }
            with pytest.raises(WebSocketDisconnect) as disconnected:
                ws.receive_json()
            assert disconnected.value.code == 1008

        denied = False
        with client.websocket_connect("/v1/ws") as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            denied = True
            client.portal.call(removals.changed, OWNER)
            assert ws.receive_json() == {
                "type": "error",
                "code": "account_deleted",
                "retryable": False,
            }
            with pytest.raises(WebSocketDisconnect) as disconnected:
                ws.receive_json()
            assert disconnected.value.code == 1008


# Подписывает двух владельцев, выдаёт изменение только для одного и проверяет оба сигнала.
# Уведомление не должно разбудить чужой экран.
def test_global_feed_wakes_only_owner_named_in_an_incremental_update(tmp_path):
    untouched = str(UUID(int=6))
    requests = []

    # Записывает границу входа, один раз выдаёт изменение владельца и затем отменяет чтение.
    # Сценарий проверяет, что новая граница успела использоваться.
    def respond(request):
        cursor = request.url.params["cursor"]
        requests.append(cursor)
        if not cursor:
            return httpx.Response(200, json={"cursor": "one", "changes": ["owner:" + OWNER]})
        raise asyncio.CancelledError

    # Подключает обоих владельцев к общему получателю и запускает чтение до отмены. Сверяет
    # вызванный сигнал и отсутствие сигнала у другого.
    async def follow():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(respond)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            delivery = RemovalDelivery()
            changed = delivery.subscribe(OWNER)
            other = delivery.subscribe(untouched)
            with pytest.raises(asyncio.CancelledError):
                await delivery.follow(adapter, "fixture-key", 0)
            assert changed.is_set()
            assert not other.is_set()
            assert requests == ["", "one"]

    asyncio.run(follow())


# Открывает соединение, которое не прислало начальную границу, и сокращает ожидание до проверки.
# Оба набора подписок должны освободиться.
def test_socket_without_resume_releases_subscriptions_after_timeout(tmp_path, monkeypatch):
    removals, uploads = RemovalDelivery(), UploadDelivery()
    app = FastAPI()

    # Даёт рабочий проверочный вход, чтобы таймер начался после принятия соединения. Проверка не
    # должна закончиться преждевременным отказом во входе.
    async def owner(headers):
        return OWNER, "token"

    class IdleConnection:
        headers = {}
        accepted = False

        # Запоминает успешное принятие. Это подтверждает, что истечение ожидания проверялось после
        # открытия канала.
        async def accept(self):
            self.accepted = True

        # Не отдаёт начальное сообщение и ждёт отмены. Так тест воспроизводит подключение, от
        # которого больше ничего не пришло.
        async def receive_json(self):
            await asyncio.Event().wait()

    real_wait = asyncio.wait

    # Сверяет исходный предел ожидания и сразу проверяет готовые задачи. Сценарий обнаруживает
    # нарушение срока без настоящей паузы.
    async def immediately_expire(tasks, *, timeout, return_when):
        assert timeout == 15
        return await real_wait(tasks, timeout=0, return_when=return_when)

    monkeypatch.setattr(websocket.asyncio, "wait", immediately_expire)
    register_websocket(
        app,
        lambda: SimpleNamespace(owner=owner),
        State(tmp_path / "bridge.sqlite"),
        removals,
        uploads,
        poll_seconds=3600,
        erasure_key="local-test",
        erasure_poll_seconds=120,
    )
    endpoint = next(route.endpoint for route in app.routes if isinstance(route, WebSocketRoute))
    connection = IdleConnection()
    asyncio.run(endpoint(connection))
    assert connection.accepted
    assert not uploads.sockets
    assert not removals.sockets
