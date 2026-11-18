# Проверяет удаление чата и запроса по записям базы, файлам и повторным обращениям. Общие
# исходники сохраняются, пока ими пользуется другая работа.

"""Проверяет не только ответ удаления, но и оставшиеся строки и файлы."""

import asyncio
import time
from uuid import uuid4

import pytest

from app.Models import Asset, Job, PendingMediaRemoval
from app.Services.Chats.content_removal import clear_pending_media
from tests.support.fixtures import MOBILE_FIXTURES, account


# Получает разрешение и загружает пробный настоящий снимок. При заданном чате связывает передачу с
# ним, чтобы проверять очистку до и после создания запроса.
def source(client, owner, thread=None):
    ticket = client.post(
        "/v1/uploads/ticket", headers=owner, params={"thread_id": thread} if thread else {}
    ).json()["path"]
    photo = (
        MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
    ).read_bytes()
    response = client.post(ticket, files={"file": ("source.jpg", photo, "image/jpeg")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# Отправляет короткий запрос на видео с исходником и новым номером операции. Возвращает также
# исходные поля, чтобы позднее точно повторить эту отправку.
def submit(client, owner, thread, asset):
    operation = str(uuid4())
    body = {
        "thread_id": thread,
        "prompt": "Уникальный текст для удаления",
        "model": "video",
        "input_id": asset,
        "duration_seconds": 1,
    }
    response = client.post("/v1/jobs", headers={**owner, "Idempotency-Key": operation}, json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"], operation, body


# Удаляет чат или текст запроса и сверяет исчезновение задания, последнего исходника и его файла.
# Повтор остаётся успешным, а чтение исходника отказано.
@pytest.mark.parametrize("target", ["chat", "prompt"])
def test_erasure_collects_last_uploaded_source(client, target):
    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    asset = source(client, owner)
    job, _, _ = submit(client, owner, thread, asset)
    with client.app.state.factory() as db:
        path = client.app.state.settings.media_dir / db.get(Asset, asset).filename
    route = (
        f"/v1/threads/{thread}" if target == "chat" else f"/v1/jobs/{job}/contents?target=prompt"
    )
    assert client.delete(route, headers=owner).status_code == 204
    with client.app.state.factory() as db:
        assert db.get(Asset, asset) is None
        assert db.get(Job, job) is None
    assert not path.exists()
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 404
    assert client.get("/v1/uploads", headers=owner).json() == []
    assert client.delete(route, headers=owner).status_code == 204


# Проверяет ошибки при удалении неизвестного результата, затем убирает весь запрос. Строка
# исчезает, но повтор отправки получает отказ, иначе стёртый текст вернулся бы.
def test_prompt_removes_job_payload_but_still_rejects_late_send(client):
    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    job, operation, body = submit(client, owner, thread, source(client, owner))
    route = f"/v1/jobs/{job}/contents"
    assert (
        client.delete(
            route, headers=owner, params={"target": "result", "blob": str(uuid4())}
        ).status_code
        == 404
    )
    assert client.delete(route, headers=owner, params={"target": "result"}).status_code == 422
    assert client.delete(route, headers=owner, params={"target": "prompt"}).status_code == 204
    assert client.get(f"/v1/jobs/{job}", headers=owner).status_code == 410
    with client.app.state.factory() as db:
        assert db.get(Job, job) is None
    replay = client.post("/v1/jobs", headers={**owner, "Idempotency-Key": operation}, json=body)
    assert replay.status_code == 410
    assert client.delete(f"/v1/jobs/{job}/contents?target=prompt", headers=owner).status_code == 204
    assert client.delete(f"/v1/threads/{thread}", headers=owner).status_code == 204
    assert client.get(f"/v1/jobs/{job}", headers=owner).status_code == 404


# Использует фото в одном запросе и привязывает его к черновику другого чата. Удаление запроса
# сохраняет фото, а удаление оставшегося чата освобождает файл.
def test_source_bound_to_another_chat_draft_is_preserved(client):
    owner = account(client)
    first, second = [client.post("/v1/threads", headers=owner).json()["id"] for _ in range(2)]
    asset = source(client, owner)
    job, _, _ = submit(client, owner, first, asset)
    binding = client.post(f"/v1/threads/{second}/sources/{asset}", headers=owner)
    assert binding.status_code == 204, binding.text
    assert client.delete(f"/v1/jobs/{job}/contents?target=prompt", headers=owner).status_code == 204
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 200
    assert client.delete(f"/v1/threads/{second}", headers=owner).status_code == 204
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 404


# Получает право загрузки, удаляет чат и только потом отправляет фото. Отказ должен оставить папку
# и таблицу вложений пустыми.
def test_late_source_upload_cannot_recreate_deleted_chat_files(client):
    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    ticket = client.post("/v1/uploads/ticket", params={"thread_id": thread}, headers=owner).json()[
        "path"
    ]
    assert client.delete(f"/v1/threads/{thread}", headers=owner).status_code == 204
    photo = (
        MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
    ).read_bytes()
    response = client.post(ticket, files={"file": ("source.jpg", photo, "image/jpeg")})
    assert response.status_code in {401, 410}
    with client.app.state.factory() as db:
        assert db.query(Asset).count() == 0
    assert list(client.app.state.settings.media_dir.iterdir()) == []


# Удаляет историю, пока исполнитель работает, и сверяет отсутствие позднего результата. Даже если
# исполнитель успел записать байты, в папке и базе ничего не должно остаться.
def test_erasure_fences_provider_that_finishes_after_delete(client):
    from app.Services.Generation.process_generation import process_one

    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    job, _, _ = submit(client, owner, thread, source(client, owner))

    class LateProvider:
        # Удаляет чат после начала нужного задания и записывает поздний пробный результат. Так
        # можно увидеть ошибку публикации без ожидания реального исполнителя.
        async def generate(self, item, path, reference):
            assert item.id == job
            assert client.delete(f"/v1/threads/{thread}", headers=owner).status_code == 204
            path.write_bytes(b"late result must never be published")
            return "video/mp4"

    assert asyncio.run(
        process_one(client.app.state.factory, client.app.state.settings, LateProvider())
    )
    with client.app.state.factory() as db:
        assert db.query(Asset).count() == 0
        assert db.query(Job).count() == 0
    assert not list(client.app.state.settings.media_dir.iterdir())


# Заполняет один чат десятью тысячами заданий и оставляет отдельное задание в другом. Проверяет
# удаление менее чем за тридцать секунд и отметки для всех запросов. Запись в другом чате
# того же владельца остаётся прежней.
def test_large_chat_erasure_keeps_other_chat_and_finishes_within_budget(client):
    from sqlalchemy import insert, text

    from app.Models import ErasedGeneration

    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    other = client.post("/v1/threads", headers=owner).json()["id"]
    job, _, _ = submit(client, owner, other, source(client, owner))
    with client.app.state.factory.begin() as db:
        user = db.get(Job, job).user_id
        before = tuple(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job}).one())
        db.execute(
            insert(Job),
            [
                dict(
                    id=str(uuid4()),
                    user_id=user,
                    thread_id=thread,
                    operation_key=str(uuid4()),
                    fingerprint="a" * 64,
                    prompt="synthetic erasure load",
                    model="image",
                    aspect="1:1",
                    seed=0,
                    provider="demo",
                )
                for _ in range(10000)
            ],
        )
    started = time.monotonic()
    assert client.delete(f"/v1/threads/{thread}", headers=owner).status_code == 204
    assert time.monotonic() - started < 30
    with client.app.state.factory() as db:
        assert db.query(Job).count() == 1
        assert db.query(ErasedGeneration).count() == 10000
        assert (
            tuple(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job}).one()) == before
        )


# Оставляет вместо файла папку, чтобы удаление встретило отказ. Следующий проход удаляет уже
# обычный файл, сохраняя посторонний вложенный путь.
def test_file_cleanup_retries_disk_errors_and_preserves_nested_files(client):
    factory = client.app.state.factory
    media = client.app.state.settings.media_dir
    failed = media / "pending.jpg"
    failed.mkdir()
    nested = media / "folder" / "keep.jpg"
    nested.parent.mkdir()
    nested.write_bytes(b"unrelated")
    with factory.begin() as db:
        db.add_all(
            [
                PendingMediaRemoval(filename=failed.name),
                PendingMediaRemoval(filename="folder/keep.jpg"),
            ]
        )
    with factory() as db:
        clear_pending_media(db, media)
        assert db.get(PendingMediaRemoval, failed.name) is not None
        assert nested.read_bytes() == b"unrelated"
    failed.rmdir()
    failed.write_bytes(b"remove on retry")
    with factory() as db:
        clear_pending_media(db, media)
        assert db.get(PendingMediaRemoval, failed.name) is None
        assert not failed.exists()
        assert nested.read_bytes() == b"unrelated"


# Даёт двум заданиям общий исходник. После первого удаления второе всё ещё читает исходник, после
# последнего загрузка уже недоступна.
def test_shared_input_is_linked_to_chat_until_its_last_generation_is_deleted(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    asset = source(client, headers, thread)
    first, _, _ = submit(client, headers, thread, asset)
    second, _, _ = submit(client, headers, thread, asset)
    assert (
        client.delete(f"/v1/jobs/{first}/contents?target=prompt", headers=headers).status_code
        == 204
    )
    assert client.get(f"/v1/assets/{asset}", headers=headers).status_code == 200
    assert client.get(f"/v1/jobs/{second}", headers=headers).json()["input_id"] == asset
    assert (
        client.delete(f"/v1/jobs/{second}/contents?target=prompt", headers=headers).status_code
        == 204
    )
    assert client.get(f"/v1/assets/{asset}", headers=headers).status_code == 404
