# Проверяет срок входа и общий счётчик через два экземпляра сервера на одной местной базе. Такие
# проверки обнаруживают ситуацию, когда смена экземпляра обходит ограничение.

import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.Models import Session, User
from app.Services.Accounts.sessions import digest, lock_user
from tests.support.fixtures import account
from tests.support.fixtures import create_studio as create_app


# Переносит время последнего действия до и после границы простоя. Обычное чтение не продлевает
# вход, записанное действие продлевает, а истёкший ключ оба экземпляра отклоняют.
def test_idle_session_expires_on_all_instances_and_cannot_be_revived(tmp_path):
    settings = {
        "database_url": f"sqlite:///{tmp_path}/db",
        "media_dir": tmp_path / "media",
        "session_idle_minutes": 30,
    }
    with TestClient(create_app(**settings)) as first, TestClient(create_app(**settings)) as second:
        assert first.post("/v1/auth/activity").status_code == 401
        headers = account(first)
        key = digest(headers["Authorization"].removeprefix("Bearer "))
        recent = time.time() - 29 * 60

        # Записывает нужное время в сеанс временной базы. Так проверка достигает точной границы
        # без длительного ожидания.
        def set_activity(value):
            with first.app.state.factory.begin() as db:
                db.get(Session, key).last_activity_at = value

        # Читает время из отдельного сеанса базы. Это позволяет сравнить сохранённое значение и
        # проверить, изменил ли его именно нужный запрос.
        def last_activity():
            with first.app.state.factory() as db:
                return db.get(Session, key).last_activity_at

        set_activity(recent)
        assert second.get("/v1/auth/me", headers=headers).status_code == 200
        assert last_activity() == recent
        assert first.post("/v1/auth/activity", headers=headers).status_code == 200
        assert last_activity() > recent
        set_activity(time.time() - 30 * 60 - 1)
        assert second.post("/v1/auth/activity", headers=headers).status_code == 401
        assert first.get("/v1/auth/me", headers=headers).status_code == 401


# Расходует допустимые попытки на двух экземплярах и сверяет общий отказ и время ожидания. Вход
# под другим адресом должен работать, иначе ограничение затронуло бы чужой аккаунт.
def test_login_limit_is_shared_and_leaves_other_accounts_available(tmp_path, monkeypatch):
    monkeypatch.setattr("app.Services.Accounts.auth_limits.MAX_ATTEMPTS", 2)
    settings = {"database_url": f"sqlite:///{tmp_path}/db", "media_dir": tmp_path / "media"}
    with TestClient(create_app(**settings)) as first, TestClient(create_app(**settings)) as second:
        email = "owner@example.com"
        account(first, email)
        other = "other@example.com"
        account(second, other)
        path = "/v1/auth/login"
        for client in [first, second]:
            assert (
                client.post(path, json={"email": email, "password": "wrong-password"}).status_code
                == 401
            )
        credentials = {"email": email, "password": "long-password-123"}
        blocked = second.post(path, json=credentials)
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers["Retry-After"]) <= 60
        assert first.post(path, json={**credentials, "email": other}).status_code == 200


# Проверяет, что обычный вход выдерживает двухдневный простой. Затем отдельно завершает полный
# срок и ждёт отказа через оба экземпляра даже при попытке продлить активность.
def test_default_mobile_session_survives_long_pause_but_keeps_absolute_expiry(tmp_path):
    settings = {"database_url": f"sqlite:///{tmp_path}/db", "media_dir": tmp_path / "media"}
    with TestClient(create_app(**settings)) as first, TestClient(create_app(**settings)) as second:
        headers = account(first)
        key = digest(headers["Authorization"].removeprefix("Bearer "))
        with first.app.state.factory.begin() as db:
            row = db.get(Session, key)
            row.last_activity_at = time.time() - 2 * 86400
        assert second.get("/v1/auth/me", headers=headers).status_code == 200
        assert first.post("/v1/auth/activity", headers=headers).status_code == 200
        with first.app.state.factory.begin() as db:
            db.get(Session, key).expires_at = time.time() - 1
        assert second.get("/v1/auth/me", headers=headers).status_code == 401
        assert first.post("/v1/auth/activity", headers=headers).status_code == 401


# Берёт блокировку одной записи дважды в одном сеансе и откатывает его. Вложенные операции с
# аккаунтом должны вернуть ту же запись без попытки открыть вторую запись в базе.
def test_sqlite_can_lock_the_same_owner_twice_in_one_transaction(client):
    account(client)
    with client.app.state.factory() as db:
        owner = db.scalar(select(User.id))
        first = lock_user(db, id=owner)
        second = lock_user(db, id=owner)
        assert first.id == second.id == owner
        db.rollback()
