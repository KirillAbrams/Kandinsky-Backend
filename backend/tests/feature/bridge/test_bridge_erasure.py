# Проверяет удаление следов чата в таблицах, событиях и файлах местной базы. Сценарии включают
# позднюю историю и чтение, мешающее окончательной очистке.

"""История не должна сохранять текст и кадры удалённого разговора."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest

from app.Services.Generation.process_generation import process_one
from app.Services.Mobile.erasure_sync import sync_content_erasures
from tests.support.fixtures import integration_clients
from tests.support.fixtures import register_mobile as register


# Создаёт тестовый чат и проводит одно задание до готового ответа. Возвращает обе строки и
# заметный текст, по которому можно отследить очистку записей.
def generated_chat(upstream, bridge, auth):
    chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
    marker = "PRIVATE_ERASURE_BRIDGE_" + uuid4().hex
    with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
        ws.send_json({"type": "resume", "cursor": 0})
        assert ws.receive_json()["type"] == "resumed"
        ws.send_json(
            {
                "type": "send",
                "requestId": "erase",
                "messages": [
                    {
                        "clientId": str(uuid4()),
                        "chatId": chat,
                        "text": marker,
                        "generationOptions": {"model": "image"},
                    }
                ],
            }
        )
        assert ws.receive_json()["type"] == "accepted"
        ws.receive_json()
    assert asyncio.run(process_one(upstream.app.state.factory, upstream.app.state.settings))
    prompt, answer = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()["messages"]
    return chat, marker, prompt, answer


# Удаляет готовый чат и читает его старые записи и события напрямую. Проверяет отсутствие текста и
# маленьких кадров, пустые связи обложек и отказ читать историю.
def test_chat_delete_scrubs_messages_events_and_embedded_previews(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "erase-content@example.com")
        chat, marker, _, _ = generated_chat(upstream, bridge, auth)
        assert bridge.delete(f"/v1/kandinsky/threads/{chat}", headers=auth).status_code == 204
        with sqlite3.connect(tmp_path / "bridge.sqlite") as db:
            bodies = [
                value
                for table in ("messages", "events")
                for (value,) in db.execute(f"SELECT body FROM {table}")
            ]
            assert all(marker not in body for body in bodies)
            assert all("tinyPreview" not in body for body in bodies)
            assert db.execute("SELECT count(*) FROM previews").fetchone()[0] == 0
        assert bridge.get(f"/v1/chats/{chat}/history", headers=auth).status_code == 410


# Сохраняет прежнюю пару сообщений, удаляет чат и пытается применить эту пару повторно. Очередь
# должна сохранить отметки удаления и оставить историю пустой.
def test_late_snapshot_cannot_repopulate_erased_chat(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "late-erased@example.com")
        chat, _, prompt, answer = generated_chat(upstream, bridge, auth)
        state = bridge.app.state.adapter.state
        with sqlite3.connect(tmp_path / "bridge.sqlite") as db:
            owner, root = db.execute(
                "SELECT owner,job FROM messages WHERE id=?", (prompt["id"],)
            ).fetchone()
        assert bridge.delete(f"/v1/kandinsky/threads/{chat}", headers=auth).status_code == 204
        assert state.remember_pair(owner, root, prompt, answer, True) == []
        assert all(event["kind"] == "kandinsky.retired" for event in state.events_after(owner, 0))


# Удаляет запрос напрямую, отзывает вход и завершает очистку через закрытую очередь. Сверяет
# исчезновение текста и наличие подтверждения, хотя пользователь больше не вошёл.
def test_internal_cleanup_does_not_require_a_live_user_session(tmp_path):
    key = "isolated-internal-erasure-key"
    upstream, bridge = integration_clients(tmp_path, erasure_key=key)
    with upstream, bridge:
        _, auth = register(bridge, "internal-erase@example.com")
        _, marker, prompt, _ = generated_chat(upstream, bridge, auth)
        adapter = bridge.app.state.adapter
        with sqlite3.connect(tmp_path / "bridge.sqlite") as db:
            _, root = db.execute(
                "SELECT owner,job FROM messages WHERE id=?", (prompt["id"],)
            ).fetchone()
        from app.Services.Mobile.session_tokens import token_from

        canonical_auth = {
            "Authorization": "Bearer " + token_from({"authorization": auth["Authorization"]})
        }
        assert (
            upstream.delete(
                f"/v1/jobs/{root}/contents?target=prompt", headers=canonical_auth
            ).status_code
            == 204
        )
        assert upstream.post("/v1/auth/logout", headers=canonical_auth).status_code == 204
        path = "/v1/internal/content-erasures"
        page = upstream.get(path, headers={"X-Kandinsky-Erasure-Key": key})
        assert page.status_code == 200, page.text
        asyncio.run(sync_content_erasures(adapter, key))
        with sqlite3.connect(tmp_path / "bridge.sqlite") as db:
            for table in ("messages", "events"):
                assert not any(marker in row[0] for row in db.execute(f"SELECT body FROM {table}"))
        assert upstream.get(path, headers={"X-Kandinsky-Erasure-Key": key}).json() == {"items": []}


# Держит чтение базы до ошибки очистки, отпускает его и повторяет удаление. По файлам базы и
# журнала сверяет, что удалённого текста нет и в сохранённых байтах.
def test_busy_sqlite_reader_delays_ack_and_retry_scrubs_raw_database(tmp_path):
    key = "isolated-busy-reader-key"
    upstream, bridge = integration_clients(tmp_path, erasure_key=key)
    with upstream, bridge:
        _, auth = register(bridge, "busy-erasure@example.com")
        chat, marker, _, _ = generated_chat(upstream, bridge, auth)
        state = bridge.app.state.adapter.state
        with sqlite3.connect(tmp_path / "bridge.sqlite") as reader:
            reader.execute("BEGIN")
            owner = reader.execute("SELECT owner FROM messages LIMIT 1").fetchone()[0]
            with pytest.raises(sqlite3.OperationalError, match="active readers"):
                state.erase_chat(owner, chat)
            reader.rollback()
        state.erase_chat(owner, chat)
        for suffix in ("", "-wal", "-shm"):
            file = tmp_path / ("bridge.sqlite" + suffix)
            if file.exists():
                assert marker.encode() not in file.read_bytes()


# Отпускает читателя вскоре после начала удаления и проверяет успешное завершение. Краткое чтение
# не должно требовать второго вызова или оставлять старые байты.
def test_short_history_read_does_not_fail_completed_erasure(tmp_path):
    import threading

    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "short-reader@example.com")
        chat, marker, _, _ = generated_chat(upstream, bridge, auth)
        state = bridge.app.state.adapter.state
        reader = sqlite3.connect(tmp_path / "bridge.sqlite", check_same_thread=False)
        reader.execute("BEGIN")
        owner = reader.execute("SELECT owner FROM messages LIMIT 1").fetchone()[0]
        release = threading.Timer(0.02, reader.rollback)
        release.start()
        try:
            state.erase_chat(owner, chat)
            assert marker.encode() not in (tmp_path / "bridge.sqlite").read_bytes()
        finally:
            release.join()
            reader.close()
