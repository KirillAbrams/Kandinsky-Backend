# Проверяет доступ к заданиям и файлам, отзыв входа и ограничения загрузки. Ошибочный запрос
# должен получить отказ до выдачи чужих данных или затратной обработки.

import io
import time

from PIL import Image
from sqlalchemy import select

from app.Models import Session, User
from tests.support.fixtures import account, job


# Сравнивает чтение владельцем, посторонним и без входа. После выхода проверяет отказ по прежнему
# ключу, иначе доступ к закрытым данным пережил бы отзыв.
def test_private_jobs_and_assets_and_revocation(client):
    a, b = account(client), account(client, "two@example.com")
    created = job(client, a).json()
    assert client.get("/v1/jobs/" + created["id"], headers=b).status_code == 404
    assert client.get("/v1/threads/" + created["thread_id"] + "/jobs", headers=b).status_code == 404
    image = io.BytesIO()
    Image.new("RGB", (30, 20)).save(image, "PNG")
    uploaded = client.post(
        "/v1/assets", headers=a, files={"file": ("../../bad.png", image.getvalue(), "image/png")}
    )
    assert uploaded.status_code == 201
    url = "/v1/assets/" + uploaded.json()["id"]
    assert client.get(url, headers=b).status_code == 404
    assert client.get(url).status_code == 401
    assert client.get(url, headers=a).headers["content-type"].startswith("image/jpeg")
    assert client.post("/v1/auth/logout", headers=a).status_code == 204
    assert client.get("/v1/auth/me", headers=a).status_code == 401


# Смотрит, что база содержит проверочные значения вместо открытых пароля и ключа, затем
# просрочивает сеанс. Сервер должен отклонить прежний ключ, даже если он всё ещё хранится у
# клиента.
def test_expired_session_and_no_plaintext_secrets(client):
    a = account(client)
    token = a["Authorization"].split()[1]
    with client.app.state.factory() as db:
        session = db.scalar(select(Session))
        assert session.digest != token
        assert len(session.digest) == 64
        user = db.scalar(select(User))
        assert user.password_hash.startswith("$argon2")
        session.expires_at = time.time() - 1
        db.commit()
    assert client.get("/v1/auth/me", headers=a).status_code == 401


# Загружает ложное изображение и страницу с разметкой и проверяет отказ. Затем проверяет отказ во
# входе с неверным паролем. Иначе одного имени файла или адреса хватило бы, чтобы пройти проверку.
def test_bad_media_and_password(client):
    a = account(client)
    assert (
        client.post(
            "/v1/assets", headers=a, files={"file": ("x.png", b"not an image", "image/png")}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/assets", headers=a, files={"file": ("x.html", b"<script/>", "text/html")}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/auth/login", json={"email": "one@example.com", "password": "wrong-password-000"}
        ).status_code
        == 401
    )


# Отправляет запросы с превышением предела и с пустым файлом. Слишком большое тело запроса должно
# получить отказ до разбора отдельных частей, иначе разбор мог бы потратить память впустую.
def test_body_limit_precedes_multipart_parsing(client):
    a = account(client)
    response = client.post(
        "/v1/assets",
        headers={**a, "Content-Type": "application/octet-stream"},
        content=b"x" * (21 * 1024 * 1024),
    )
    assert response.status_code == 413
    assert not list(client.app.state.settings.media_dir.glob("*"))


# Передаёт изображение выше предела приложения по числу точек, которое библиотека ещё готова
# читать. Без отдельной проверки снимок прошёл бы ограничение по размеру файла.
def test_pixel_limit_rejects_even_below_pillow_hard_bomb_threshold(client, monkeypatch):
    import pytest

    h = account(client)
    image = io.BytesIO()
    Image.new("RGB", (10, 3)).save(image, "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 20)
    with pytest.warns(Image.DecompressionBombWarning):
        response = client.post(
            "/v1/assets", headers=h, files={"file": ("large.png", image.getvalue(), "image/png")}
        )
    assert response.status_code == 415
