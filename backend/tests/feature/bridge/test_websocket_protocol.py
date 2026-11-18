# Проверяет долгоживущий канал телефона при начале обмена, получении команд и отзыве входа.
# Сценарии используют два местных приложения и управляемые часы для срока проверки сеанса.

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app.Http.Controllers import mobile_socket_controller as websocket
from app.Services.Database.Mobile.state import State
from tests.support.fixtures import BRIDGE_TEST_PASSWORD, integration_clients, register_mobile


# Открывает канал без данных входа и ждёт отказа до начала обмена. Это ловит случай, когда служба
# соглашается принять неподтверждённое подключение.
def test_websocket_with_no_account_is_rejected(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge, pytest.raises(WebSocketDisconnect) as disconnected:
        with bridge.websocket_connect("/v1/ws"):
            pytest.fail("Unauthenticated connection entered the protocol")
    assert disconnected.value.code == 1008


# Передаёт начальное сообщение без допустимой отметки продолжения и сверяет причину закрытия.
# Канал должен знать, откуда читать историю, до обработки остальных команд.
@pytest.mark.parametrize(("packet", "code"), [(None, 1003), ({"type": "ping"}, 1008)])
def test_websocket_requires_a_resume_object(tmp_path, packet, code):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register_mobile(bridge, "protocol@example.com")
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json(packet)
            with pytest.raises(WebSocketDisconnect) as disconnected:
                ws.receive_json()
        assert disconnected.value.code == code


# Проверяет ответы при неверных границах, исходниках и командах. После отказов читает историю и
# подтверждает, что не появилось ни одного задания, а канал продолжает принимать допустимые
# обращения.
def test_websocket_does_not_create_jobs_for_rejected_commands(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register_mobile(bridge, "protocol@example.com")
        assert upstream.get("/v1/internal/account-erasures").status_code == 404
        assert bridge.get("/health").json() == {
            "ok": True,
            "upstream": {"ok": True, "provider": "demo"},
        }
        assert [
            item["id"] for item in bridge.get("/v1/kandinsky/models", headers=auth).json()["models"]
        ] == ["image", "video"]
        assert bridge.get("/v1/bootstrap", headers=auth).json()["messages"] == []
        assert bridge.post("/v1/kandinsky/activity", headers=auth).json() == {"ok": True}
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        assert (
            bridge.get(
                f"/v1/chats/{chat}/history", headers=auth, params={"before": 900}
            ).status_code
            == 404
        )
        assert (
            bridge.put(
                f"/v1/uploads/{uuid4()}",
                headers={**auth, "X-Kandinsky-Source": "1", "X-Kandinsky-Chat": "invalid-chat"},
                content=b"photo",
            ).status_code
            == 422
        )
        packet = {"type": "kandinsky.check", "chatId": chat, "messageId": 1}
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": "not-a-number"})
            assert ws.receive_json() == {
                "type": "error",
                "requestId": None,
                "code": "Неверный запрос",
                "retryable": False,
            }
            ws.send_json({"type": "resume", "cursor": -1})
            assert ws.receive_json()["type"] == "resumed"
            for command, expected in [
                ({"type": "send", "messages": []}, "Слишком много запросов"),
                (packet, "Задание не найдено"),
                ({"type": "delete"}, "Используйте отмену генерации"),
            ]:
                ws.send_json({**command, "requestId": "rejected"})
                assert ws.receive_json() == {
                    "type": "error",
                    "requestId": "rejected",
                    "code": expected,
                    "retryable": False,
                }

            ws.send_json({"type": "applied", "cursor": 0})
            assert ws.receive_json() == {"type": "applied", "cursor": 0}
        assert bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()["messages"] == []


# Ставит задание через канал, открывает тот же чат и отменяет создание. Номер ответа должен
# сохраниться, а версия вырасти, чтобы в ленте обновилась прежняя строка.
def test_websocket_open_and_cancel_update_the_same_message(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register_mobile(bridge, "open-chat@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json(
                {
                    "type": "send",
                    "messages": [{"clientId": str(uuid4()), "chatId": chat, "text": "Магнолия"}],
                }
            )
            assert ws.receive_json()["type"] == "accepted"
            original = ws.receive_json()["events"][1]["message"]
            ws.send_json({"type": "kandinsky.open", "chatId": chat})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            ws.send_json({"type": "kandinsky.cancel", "chatId": chat, "messageId": original["id"]})
            cancelled = ws.receive_json()["events"][0]["message"]
            assert cancelled["id"] == original["id"]
            assert cancelled["text"] == "Генерация отменена"
            assert cancelled["revision"] > original["revision"]


# Отзывает вошедший сеанс двумя способами и переводит часы за границу проверки. Канал должен
# закрыться, а при удалении аккаунта сообщить особую причину.
@pytest.mark.parametrize("erased", [True, False])
def test_resumed_socket_rechecks_a_session_after_its_validation_window(
    tmp_path, monkeypatch, erased
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(websocket, "time", SimpleNamespace(monotonic=lambda: clock.now))
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        email = "socket-expiration@example.com"
        _, auth = register_mobile(bridge, email)
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            if erased:
                login = upstream.post(
                    "/v1/auth/login", json={"email": email, "password": BRIDGE_TEST_PASSWORD}
                )
                assert login.status_code == 200
                session = {"Authorization": f"Bearer {login.json()['token']}"}
                deleted = upstream.request(
                    "DELETE",
                    "/v1/auth/me",
                    headers=session,
                    json={"password": BRIDGE_TEST_PASSWORD},
                )
                assert deleted.status_code == 204
            else:
                assert bridge.post("/v1/kandinsky/logout", headers=auth).status_code == 204
            clock.now = 31
            ws.send_json({"type": "ping"})
            if erased:
                packet = ws.receive_json()
                if packet == {"type": "pong"}:
                    packet = ws.receive_json()
                assert packet == {"type": "error", "code": "account_deleted", "retryable": False}
            with pytest.raises(WebSocketDisconnect) as disconnected:
                while ws.receive_json() == {"type": "pong"}:
                    pass
            assert disconnected.value.code == 1008


# Сравнивает подключение удалённого владельца с обычным. Проверяет закрытие и уведомление в первом
# случае и сохранение ответов без лишних проверок входа во втором.
@pytest.mark.parametrize("erased_in_bridge", [True, False])
def test_socket_poll_checks_local_erasure_and_keeps_valid_sessions(
    tmp_path, monkeypatch, erased_in_bridge
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(websocket, "time", SimpleNamespace(monotonic=lambda: clock.now))
    upstream, bridge = integration_clients(tmp_path, erasure_key="local-test-erasure-key")
    with upstream, bridge:
        email = "valid-session@example.com"
        _, auth = register_mobile(bridge, email)
        credentials = {"email": email, "password": BRIDGE_TEST_PASSWORD}
        owner = upstream.post("/v1/auth/login", json=credentials).json()["user_id"]
        adapter = bridge.app.state.adapter
        request = AsyncMock(wraps=adapter.studio.request)
        monkeypatch.setattr(adapter.studio, "request", request)
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            if erased_in_bridge:
                State(tmp_path / "bridge.sqlite").erase_owners([owner])
            clock.now = 31.0
            ws.send_json({"type": "ping"})
            if erased_in_bridge:
                assert ws.receive_json() == {
                    "type": "error",
                    "code": "account_deleted",
                    "retryable": False,
                }
                with pytest.raises(WebSocketDisconnect) as disconnected:
                    ws.receive_json()
                assert disconnected.value.code == 1008
            else:
                assert ws.receive_json() == {"type": "pong"}
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}
                auth_requests = [
                    call
                    for call in request.await_args_list
                    if call.args[:2] == ("GET", "/v1/auth/me")
                ]
                assert len(auth_requests) == 2
