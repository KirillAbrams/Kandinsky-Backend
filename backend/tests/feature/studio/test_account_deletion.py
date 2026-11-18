# Проверяет, что удаление аккаунта закрывает доступ, убирает его данные и защищает от запоздавшей
# генерации. Другой аккаунт служит проверкой границы владения.

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.Models import Asset, Job, PasswordReset, Session, Thread, Transfer, UploadGrant, User
from app.Services.Accounts import erasure
from app.Services.Generation.process_generation import process_one
from tests.support.fixtures import account, job


# Сначала проверяет отказ без входа и с неверным паролем. Затем удаляет аккаунт с правильным
# паролем и сверяет отзыв сеансов и ссылок, исчезновение записей и файлов владельца и сохранность
# чужих данных.
def test_account_deletion_requires_password_and_removes_owned_data(client):
    email = "to-erase@example.com"
    password = "long-password-123"
    owner = account(client, email)
    other = account(client, "keep-account@example.com")
    user_id = client.get("/v1/auth/me", headers=owner).json()["user_id"]
    second_login = client.post("/v1/auth/login", json={"email": email, "password": password})
    second = {"Authorization": f"Bearer {second_login.json()['token']}"}
    completed = job(client, owner).json()
    assert asyncio.run(process_one(client.app.state.factory, client.app.state.settings))
    asset_id = client.get(f"/v1/jobs/{completed['id']}", headers=owner).json()["output_id"]
    ticket = client.post(f"/v1/assets/{asset_id}/ticket", headers=owner).json()["path"]
    assert job(client, owner, key="queued-before-account-deletion").status_code == 201
    other_job = job(client, other).json()

    with client.app.state.factory() as db:
        asset = db.get(Asset, asset_id)
        path = client.app.state.settings.media_dir / asset.filename
    assert path.is_file()
    upload = client.post("/v1/uploads/ticket", headers=owner).json()["path"]
    source = client.post(upload, files={"file": ("input.png", path.read_bytes(), "image/png")})
    assert source.status_code == 201
    with client.app.state.factory() as db:
        original = db.get(Asset, source.json()["id"])
        source_path = client.app.state.settings.media_dir / original.filename
    assert source_path.is_file()

    assert client.delete("/v1/auth/me").status_code == 401
    wrong = client.request(
        "DELETE", "/v1/auth/me", headers=owner, json={"password": "wrong-password-123"}
    )
    assert wrong.status_code == 403
    assert client.get(ticket).status_code == 200
    deleted = client.request("DELETE", "/v1/auth/me", headers=owner, json={"password": password})
    assert deleted.status_code == 204

    for headers in (owner, second):
        assert client.get("/v1/auth/me", headers=headers).status_code == 401
    assert client.get(ticket).status_code == 401
    assert not path.exists()
    assert not source_path.exists()
    assert client.get(f"/v1/jobs/{other_job['id']}", headers=other).status_code == 200
    with client.app.state.factory() as db:
        assert db.get(User, user_id) is None
        for model in (Asset, Job, PasswordReset, Session, Thread, UploadGrant):
            assert (
                db.scalar(select(func.count()).select_from(model).where(model.user_id == user_id))
                == 0
            )
        assert (
            db.scalar(
                select(func.count()).select_from(Transfer).where(Transfer.asset_id == asset_id)
            )
            == 0
        )


# Удерживает обработчик во время генерации, удаляет владельца и разрешает вычислению закончиться.
# Ни результат, ни остаточный файл не должны появиться после удаления.
def test_render_finishing_after_account_deletion_cannot_publish(client):
    owner = account(client, "erase-during-render@example.com")
    job_id = job(client, owner).json()["id"]
    factory, settings = client.app.state.factory, client.app.state.settings
    rendering = Event()
    finish = Event()

    class HeldProvider:
        # Сообщает, что обработка началась, и ждёт разрешения на завершение. После сигнала пишет
        # пробное изображение, чтобы проверка могла поймать его ошибочную публикацию после
        # удаления.
        async def generate(self, _job, output, _reference):
            rendering.set()
            assert await asyncio.to_thread(finish.wait, 8)
            Image.new("RGB", (2, 2)).save(output)
            return "image/png"

    with ThreadPoolExecutor() as executor:
        render = executor.submit(asyncio.run, process_one(factory, settings, HeldProvider()))
        try:
            assert rendering.wait(6)
            deleted = client.request(
                "DELETE", "/v1/auth/me", headers=owner, json={"password": "long-password-123"}
            )
            assert deleted.status_code == 204
        finally:
            finish.set()
        assert render.result(timeout=10)

    with factory() as db:
        assert db.get(Job, job_id) is None
        assert db.scalar(select(func.count()).select_from(Asset)) == 0
    assert not list(settings.media_dir.iterdir())


# Вызывает сбой удаления файлов после записи изменений в базу. Доступ к аккаунту уже закрыт, а
# отдельный повтор убирает остаток. Иначе сбой диска выглядел бы как отмена удаления аккаунта.
def test_account_deletion_returns_success_when_file_cleanup_has_to_retry(client, monkeypatch):
    headers = account(client)
    photo = Image.new("RGB", (20, 20), "green")
    output = io.BytesIO()
    photo.save(output, "PNG")
    uploaded = client.post(
        "/v1/assets", headers=headers, files={"file": ("photo.png", output.getvalue(), "image/png")}
    )
    assert uploaded.status_code == 201
    factory = client.app.state.factory
    media = client.app.state.settings.media_dir
    assert len(list(media.iterdir())) == 1

    # Имитирует сбой хранилища в момент очистки файлов. Успешное удаление записей при этом
    # остаётся видимым для проверки.
    def fail_cleanup(*args):
        raise OperationalError("isolated filesystem cleanup delay", {}, None)

    with monkeypatch.context() as patch:
        patch.setattr(erasure, "clear_pending_media", fail_cleanup)
        result = client.request(
            "DELETE", "/v1/auth/me", headers=headers, json={"password": "long-password-123"}
        )
        assert result.status_code == 204
    assert client.get("/v1/auth/me", headers=headers).status_code == 401
    assert len(list(media.iterdir())) == 1
    with factory() as db:
        erasure.clear_pending_media(db, media)
        assert list(db.scalars(select(Asset))) == []
    assert list(media.iterdir()) == []


# Удаляет владельца между проверкой сеанса и повторным чтением владельца в операции. Проверяет
# отказ во время гонки и признак удалённого аккаунта при следующем запросе.
def test_account_erase_after_authentication_returns_documented_session_error(client, monkeypatch):
    headers = account(client)
    factory = client.app.state.factory
    original = erasure.lock_user

    # Удаляет владельца через отдельное соединение перед чтением владельца в изменяющей операции.
    # Так сбой порядка воспроизводится без случайных задержек.
    def erase_between_checks(db, *, id):
        with factory() as peer:
            erasure.erase_account(peer, peer.get(User, id))
        return original(db, id=id)

    monkeypatch.setattr(erasure, "lock_user", erase_between_checks)
    lost = client.request(
        "DELETE", "/v1/auth/me", headers=headers, json={"password": "long-password-123"}
    )
    assert lost.status_code == 401
    assert lost.json()["detail"] == "Войдите снова"
    assert client.get("/v1/auth/me", headers=headers).json()["detail"] == "account_deleted"
