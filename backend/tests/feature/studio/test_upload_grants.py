# Проверяет фоновые загрузки по временному разрешению и обычные вложения. Сверяет файлы и строки
# базы после отказов, повторов и одновременных запросов.

import io
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select

from app.Models import Asset, UploadGrant, User
from app.Services.Accounts.erasure import erase_account
from app.Services.Accounts.sessions import digest
from app.Services.Media import background_upload as uploads
from tests.support.fixtures import account


# Загружает одни байты дважды с одним разрешением и получает тот же файл. Оно не даёт права читать
# файл, не принимается с неверным значением и не разрешает подмену содержимого.
def test_background_upload_grant_scoped_and_idempotent(client):
    h = account(client)
    grant = client.post("/v1/uploads/ticket", headers=h)
    assert grant.status_code == 200
    path = grant.json()["path"]
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(image, "PNG")
    files = {"file": ("photo.png", image.getvalue(), "image/png")}
    first = client.post(path, files=files)
    second = client.post(path, files=files)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/v1/assets/" + first.json()["id"]).status_code == 401
    assert client.post("/v1/uploads/not-a-ticket", files=files).status_code == 401
    other = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(other, "PNG")
    assert (
        client.post(path, files={"file": ("other.png", other.getvalue(), "image/png")}).status_code
        == 409
    )


# Рисует маленькое изображение заданного цвета и упаковывает его как вложение. Разные цвета дают
# разные байты для проверки повтора и конфликта.
def photo_file(color="green"):
    photo = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(photo, "PNG")
    return {"file": ("photo.png", photo.getvalue(), "image/png")}


# Задаёт срок разрешения в прошлом и отправляет по нему фото. Проверяет отказ и пустую папку,
# иначе просроченная передача оставила бы файл.
def test_expired_upload_ticket_cannot_store_a_photo(client):
    headers = account(client)
    path = client.post("/v1/uploads/ticket", headers=headers).json()["path"]
    with client.app.state.factory() as db:
        grant = db.get(UploadGrant, digest(path.rsplit("/", 1)[1]))
        grant.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
        db.commit()

    response = client.post(path, files=photo_file())
    assert response.status_code == 401
    assert not list(client.app.state.settings.media_dir.iterdir())


# Ставит низкий предел размера и загружает тот же файл обычным и фоновым путём. После двух отказов
# нет записей, файлов и связи разрешения с результатом.
def test_upload_limit_applies_to_regular_and_background_uploads(client):
    headers = account(client)
    path = client.post("/v1/uploads/ticket", headers=headers).json()["path"]
    client.app.state.settings.max_upload_bytes = 32
    for route in (path, "/v1/assets"):
        assert client.post(route, files=photo_file(), headers=headers).status_code == 413
    with client.app.state.factory() as db:
        assert list(db.scalars(select(Asset))) == []
        assert db.get(UploadGrant, digest(path.rsplit("/", 1)[1])).asset_id is None
    assert not list(client.app.state.settings.media_dir.iterdir())


# Заполняет очередь разрешений одного владельца и сверяет, что второй ещё может получить своё.
# После истечения старого разрешения место освобождается, а старый адрес остаётся
# недействительным.
def test_pending_upload_limit_is_per_user_and_expires_old_grants(client):
    headers = account(client)
    other = account(client, "other@example.com")
    issued = [client.post("/v1/uploads/ticket", headers=headers) for _ in range(20)]
    assert {response.status_code for response in issued} == {200}
    assert client.post("/v1/uploads/ticket", headers=headers).status_code == 429
    assert client.post("/v1/uploads/ticket", headers=other).status_code == 200

    path = issued[0].json()["path"]
    with client.app.state.factory() as db:
        db.get(UploadGrant, digest(path.rsplit("/", 1)[1])).expires_at = 0
        db.commit()
    assert client.post("/v1/uploads/ticket", headers=headers).status_code == 200
    assert client.post(path, files=photo_file()).status_code == 401


# Даёт второй передаче закончиться раньше и проверяет повтор с тем же фото и с другим. В обоих
# случаях на диске и в базе остаётся только результат первой завершившейся передачи.
@pytest.mark.parametrize("same_photo", [True, False])
def test_background_upload_cleans_up_if_another_request_completed_first(
    client, monkeypatch, same_photo
):
    headers = account(client)
    path = client.post("/v1/uploads/ticket", headers=headers).json()["path"]
    ticket = path.rsplit("/", 1)[1]
    factory = client.app.state.factory
    settings = client.app.state.settings
    original_lock = uploads.lock_user
    winner_id = None

    # До блокировки владельца завершает конкурирующую загрузку через отдельное соединение. Так
    # исходный запрос встречает уже сохранённый файл точно в нужном месте.
    def finish_other_request_then_lock(db, *, id):
        nonlocal winner_id
        if winner_id is None:
            with factory() as peer, monkeypatch.context() as patch:
                patch.setattr(uploads, "lock_user", original_lock)
                winner = uploads.accept_background_upload(
                    peer,
                    ticket,
                    io.BytesIO(photo_file()["file"][1]),
                    "image/png",
                    max_upload_bytes=settings.max_upload_bytes,
                    media_dir=settings.media_dir,
                )
                winner_id = winner.id
        return original_lock(db, id=id)

    monkeypatch.setattr(uploads, "lock_user", finish_other_request_then_lock)
    response = client.post(path, files=photo_file("green" if same_photo else "red"))
    assert response.status_code == (201 if same_photo else 409)
    if same_photo:
        assert UUID(response.json()["id"]) == UUID(winner_id)
    with factory() as db:
        rows = list(db.scalars(select(Asset)))
        assert [asset.id for asset in rows] == [winner_id]
        assert db.get(UploadGrant, digest(ticket)).asset_id == winner_id
        assert [file.name for file in settings.media_dir.iterdir()] == [rows[0].filename]


# Проверяет повтор привязки миниатюры, запрет переноса к другому файлу и вложения одной миниатюры
# в другую. Отклонённая загрузка не оставляет лишнего файла и строки.
def test_preview_upload_rolls_back_when_parent_cannot_have_another_preview(client):
    headers = account(client)
    source = client.post("/v1/assets", files=photo_file(), headers=headers)
    assert source.status_code == 201
    parent = source.json()["id"]

    preview = client.post(
        "/v1/assets", params={"parent": parent}, files=photo_file("red"), headers=headers
    )
    assert preview.status_code == 201, preview.text
    repeated = client.post(f"/v1/assets/{parent}/previews/{preview.json()['id']}", headers=headers)
    assert repeated.status_code == 204
    another = client.post("/v1/assets", files=photo_file("orange"), headers=headers)
    assert another.status_code == 201
    misplaced = client.post(
        f"/v1/assets/{another.json()['id']}/previews/{preview.json()['id']}", headers=headers
    )
    assert misplaced.status_code == 409
    rejected = client.post(f"/v1/assets/{preview.json()['id']}/previews/{parent}", headers=headers)
    assert rejected.status_code == 422

    media = client.app.state.settings.media_dir
    before = {path.name for path in media.iterdir()}
    rejected_upload = client.post(
        "/v1/assets",
        params={"parent": preview.json()["id"]},
        files=photo_file("blue"),
        headers=headers,
    )
    assert rejected_upload.status_code == 422
    assert {path.name for path in media.iterdir()} == before
    with client.app.state.factory() as db:
        assert len(list(db.scalars(select(Asset)))) == 3


# Удаляет аккаунт между проверкой адреса загрузки и блокировкой владельца. После отказа не
# остаётся файла, записи вложения или разрешения.
def test_upload_stops_if_account_disappears_before_it_locks_owner(client, monkeypatch):
    headers = account(client)
    path = client.post("/v1/uploads/ticket", headers=headers).json()["path"]
    factory = client.app.state.factory
    lock = uploads.lock_user

    # Удаляет владельца через другое соединение и вызывает обычную блокировку. В таком порядке
    # загрузка должна ещё раз проверить право записи.
    def erase_before_lock(db, *, id):
        with factory() as peer:
            erase_account(peer, peer.get(User, id))
        return lock(db, id=id)

    monkeypatch.setattr(uploads, "lock_user", erase_before_lock)
    assert client.post(path, files=photo_file()).status_code == 401
    assert not list(client.app.state.settings.media_dir.iterdir())
    with factory() as db:
        assert list(db.scalars(select(Asset))) == []
        assert list(db.scalars(select(UploadGrant))) == []
