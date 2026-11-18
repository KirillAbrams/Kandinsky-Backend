# Проверяет уведомление открытого телефона при удалении чата через любой серверный вход. Другой
# телефон должен обновить список без нового действия пользователя.

"""Удаление обновляет уже открытую панель другого телефона без нового нажатия."""

import json

import anyio
import pytest

from app.Models import Thread
from app.Services.Mobile.session_tokens import token_from
from tests.support.fixtures import integration_clients
from tests.support.fixtures import register_mobile as register


# Читает сообщения канала до заданного вида или короткого срока. По таймеру сообщает об
# отсутствующем уведомлении вместо бесконечного ожидания.
async def receive_kind(socket, kind):
    try:
        with anyio.fail_after(2):
            while True:
                message = await socket._send_rx.receive()
                packet = json.loads(message["text"])
                if packet.get("type") == kind:
                    return packet
    except TimeoutError:
        raise AssertionError("No deletion catalog notice reached the idle phone") from None


# Открывает постоянный канал, отделяет начальное подключение и удаляет чат выбранным путём.
# Подтверждает, что обновление списка приходит именно из события удаления.
@pytest.mark.parametrize("internal_key", ["", "isolated-directory-erasure-key"])
@pytest.mark.parametrize("via_bridge", [False, True])
def test_chat_delete_wakes_other_phone_catalog_without_more_input(
    tmp_path, internal_key, via_bridge
):
    upstream, bridge = integration_clients(tmp_path, erasure_key=internal_key)
    with upstream, bridge:
        _, auth = register(bridge, "idle-phone-erasure@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with upstream.app.state.factory() as db:
            thread = db.query(Thread).one().id
        with bridge.websocket_connect("/v1/ws", headers=auth) as socket:
            socket.send_json({"type": "resume", "cursor": 0, "features": {"chatDirectory": True}})
            socket.portal.call(receive_kind, socket, "resumed")
            # Отделяем уведомление начального подключения от события самого удаления.
            socket.send_json({"type": "ping"})
            socket.portal.call(receive_kind, socket, "pong")
            if via_bridge:
                response = bridge.delete(f"/v1/kandinsky/threads/{chat}", headers=auth)
            else:
                canonical = token_from({"authorization": auth["Authorization"]})
                response = upstream.delete(
                    f"/v1/threads/{thread}", headers={"Authorization": "Bearer " + canonical}
                )
            assert response.status_code == 204
            socket.portal.call(receive_kind, socket, "kandinsky.catalog.changed")
