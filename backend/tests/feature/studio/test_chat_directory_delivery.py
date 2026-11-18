# Проверяет уведомления об изменениях списка чатов. Владельцу нужно узнать о настоящем изменении,
# а лишние уведомления вызывают повторное чтение.

"""Изменение списка уведомляет только владельца после записи в базу."""

from app.Services.Chats import update_chat
from app.Services.Database.notifications import notify as send_notice
from tests.support.fixtures import account


# Переименовывает, закрепляет и архивирует чат и сверяет каждое уведомление владельцу. Чужое
# действие и повтор без изменения не должны отправлять уведомление.
def test_thread_metadata_emits_owner_notice_and_ignores_noop(client, monkeypatch):
    auth = account(client, "directory-owner@example.com")
    foreign = account(client, "directory-foreign@example.com")
    owner = client.get("/v1/auth/me", headers=auth).json()["user_id"]
    chat = client.post("/v1/threads", headers=auth).json()["id"]
    notices = []

    # Записывает факт уведомления и его получателя во время вызова из операции. Проверка потом
    # сравнивает накопленный список с действительными изменениями.
    def notify(db, change):
        assert db.in_transaction()
        notices.append(change)
        send_notice(db, change)

    monkeypatch.setattr(update_chat, "notify", notify)
    path = f"/v1/threads/{chat}"
    assert client.patch(path, headers=foreign, json={"title": "Foreign"}).status_code == 404
    assert notices == []
    assert client.patch(path, headers=auth, json={"title": "Renamed"}).status_code == 200
    assert notices == ["owner:" + owner]
    assert client.patch(path, headers=auth, json={"title": "Renamed"}).status_code == 200
    assert notices == ["owner:" + owner]
    assert client.patch(path, headers=auth, json={"pinned": True}).status_code == 200
    assert client.patch(path, headers=auth, json={"archived": True}).status_code == 200
    assert notices == ["owner:" + owner] * 3
