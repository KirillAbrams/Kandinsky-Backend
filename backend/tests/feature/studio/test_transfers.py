# Проверяет временное право скачивания, выдачу части файла и отзыв доступа, если аккаунт исчез во
# время чтения.

import io
import time

import pytest
from PIL import Image
from sqlalchemy import select

from app.Models import Asset, Transfer, User
from app.Services.Accounts.erasure import erase_account
from tests.support.fixtures import account


# Получает разрешение на скачивание и запрашивает точный отрезок файла. Проверяет байты отрезка и
# отказ для просроченного или неверного разрешения.
def test_scoped_ticket_range_and_expiry(client):
    h = account(client)
    picture = io.BytesIO()
    Image.new("RGB", (50, 50)).save(picture, "PNG")
    a = client.post(
        "/v1/assets", headers=h, files={"file": ("x.png", picture.getvalue(), "image/png")}
    ).json()
    ticket = client.post("/v1/assets/" + a["id"] + "/ticket", headers=h).json()
    response = client.get(ticket["path"], headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert len(response.content) == 10
    assert response.headers["content-range"].startswith("bytes 0-9/")
    with client.app.state.factory() as db:
        row = db.scalar(select(Transfer))
        row.expires_at = time.time() - 1
        db.commit()
    assert client.get(ticket["path"]).status_code == 401
    assert client.get("/v1/transfers/not-a-real-ticket").status_code == 401


# Удаляет владельца между двумя чтениями в проверке сеанса или выдаче файла. Оба пути должны
# закрыть доступ, а при проверке сеанса сервер сообщает об удалённом аккаунте.
@pytest.mark.parametrize("stage", ["session", "download"])
def test_readers_handle_account_deletion_between_lookups(client, monkeypatch, stage):
    headers = account(client)
    picture = io.BytesIO()
    Image.new("RGB", (20, 20)).save(picture, "PNG")
    asset = client.post(
        "/v1/assets",
        headers=headers,
        files={"file": ("photo.png", picture.getvalue(), "image/png")},
    ).json()["id"]
    if stage == "download":
        path = client.post(f"/v1/assets/{asset}/ticket", headers=headers).json()["path"]
    else:
        path = "/v1/auth/me"
    factory = client.app.state.factory
    read = factory.class_.get
    with factory() as db:
        owner = db.scalar(select(User.id))

    # При чтении нужной записи удаляет владельца через отдельное соединение. Затем продолжает
    # исходное чтение, чтобы проверить отказ в строго заданный момент.
    def get_after_deletion(db, table, key, *args, **kwargs):
        if table is (Asset if stage == "download" else User):
            with factory() as peer:
                erase_account(peer, read(peer, User, owner))
        return read(db, table, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(factory.class_, "get", get_after_deletion)
        result = client.get(path, headers=headers)
    assert result.status_code == 401
    if stage == "session":
        assert result.json()["detail"] == "account_deleted"
    with factory() as db:
        assert db.get(User, owner) is None
        assert db.get(Asset, asset) is None
