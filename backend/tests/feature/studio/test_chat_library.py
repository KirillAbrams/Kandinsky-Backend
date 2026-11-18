# Проверяет список чатов, закрепление, архив, поиск и восстановление. Порядок и доступность
# истории должны переживать изменения карточки чата.

from tests.support.fixtures import account


# Проверяет чужой доступ и допустимые значения, затем закрепляет и убирает чат в архив. Чат
# исчезает из недавних, остаётся доступен в поиске и возвращается вместе с историей.
def test_pin_archive_restore_and_search_keep_chat_history(client):
    headers = account(client)
    another = account(client, "another@example.com")
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    path = f"/v1/threads/{thread}"
    for invalid in ({}, {"title": "   "}, {"title": None}):
        assert client.patch(path, headers=headers, json=invalid).status_code == 422
    assert (
        client.patch(path, headers=headers, json={"title": "  Зимний лес 100%_  "}).json()["title"]
        == "Зимний лес 100%_"
    )
    result = client.post(
        "/v1/jobs",
        headers={**headers, "Idempotency-Key": "chat-library-generation-01"},
        json={"thread_id": thread, "prompt": "Снег на деревьях"},
    )
    assert result.status_code == 201
    assert client.patch(path, headers=another, json={"pinned": True}).status_code == 404
    pinned = client.patch(path, headers=headers, json={"pinned": True})
    assert pinned.status_code == 200 and pinned.json()["pinned_at"] is not None
    unpinned = client.patch(path, headers=headers, json={"pinned": False}).json()
    assert unpinned["pinned_at"] is None
    assert client.patch(path, headers=headers, json={"pinned": True}).status_code == 200
    archived = client.patch(path, headers=headers, json={"archived": True}).json()
    assert archived["archived_at"] is not None and archived["pinned_at"] is None
    assert client.get("/v1/threads", params={"view": "recent"}, headers=headers).json() == []
    found = client.get(
        "/v1/threads", params={"view": "all", "q": "ЛЕС 100%_"}, headers=headers
    ).json()
    assert [row["id"] for row in found] == [thread]
    assert (
        client.get("/v1/threads", params={"view": "all", "q": "лес 100__"}, headers=headers).json()
        == []
    )
    assert client.patch(path, headers=headers, json={"pinned": True}).status_code == 409
    assert (
        client.get(f"{path}/history", headers=headers).json()["messages"][0]["id"]
        == result.json()["id"]
    )
    restored = client.patch(path, headers=headers, json={"archived": False}).json()
    assert restored["archived_at"] is None
    assert client.delete(path, headers=headers).status_code == 204
    assert (
        client.get("/v1/threads", params={"view": "all", "q": "лес"}, headers=headers).json() == []
    )


# Создаёт запрос в старом чате и проверяет его подъём в списке. Повтор того же запроса порядок не
# меняет, иначе повтор при потере ответа мог бы снова двигать чат.
def test_recent_order_changes_only_on_a_new_generation(client):
    headers = account(client)
    older = client.post("/v1/threads", headers=headers).json()["id"]
    newer = client.post("/v1/threads", headers=headers).json()["id"]
    path = "/v1/threads"
    before = client.get(path, params={"view": "recent", "limit": 1}, headers=headers).json()
    assert [row["id"] for row in before] == [newer]
    request = {"thread_id": older, "prompt": "Кадр из старого чата"}
    key = {**headers, "Idempotency-Key": "chat-library-generation-02"}
    assert client.post("/v1/jobs", headers=key, json=request).status_code == 201
    after = client.get(path, params={"view": "recent"}, headers=headers).json()
    assert [row["id"] for row in after] == [older, newer]
    assert client.post("/v1/jobs", headers=key, json=request).status_code == 201
    assert client.get(path, params={"view": "recent"}, headers=headers).json() == after


# Обновляет известные телефону чаты, которые не попали на первую страницу. Сервер сообщает, какой
# чат архивирован и какой удалён, иначе телефон сохранил бы устаревшие карточки.
def test_refresh_resolves_cached_chats_outside_the_first_page(client):
    headers = account(client)
    another = account(client, "another@example.com")
    archived = client.post("/v1/threads", headers=headers).json()["id"]
    removed = client.post("/v1/threads", headers=headers).json()["id"]
    foreign = client.post("/v1/threads", headers=another).json()["id"]
    client.patch(f"/v1/threads/{archived}", json={"archived": True}, headers=headers)
    client.delete(f"/v1/threads/{removed}", headers=headers)
    response = client.post(
        "/v1/threads/states", json={"ids": [archived, removed, foreign]}, headers=headers
    )
    assert response.status_code == 200
    result = response.json()
    assert client.post("/v1/threads/deletions", json={"ids": []}, headers=headers).json() == []
    assert result["deleted"] == [removed]
    assert [item["id"] for item in result["threads"]] == [archived]
    assert result["threads"][0]["archived_at"] is not None
