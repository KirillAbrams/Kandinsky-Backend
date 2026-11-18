# Проверяет удаление аккаунта через связующую службу, сайт и приложение. Раздельные пробные базы
# позволяют увидеть, дошло ли удаление от основного сервера до второго устройства.

import base64
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.Exceptions.accounts import ErasedAccount
from app.Http.Controllers.account_web_controller import COOKIE
from app.Services.Database.Mobile.state import State
from tests.support.fixtures import create_bridge, create_studio, register_mobile

PASSWORD = "fixture-only-passphrase-123"
ERASURE_KEY = "fixture-internal-erasure-key-which-is-long"
TABLES = ("identity", "messages", "events", "pending", "previews", "studio_uploads")


# Настраивает основной сервер и связующую службу с разными временными базами и общим проверочным
# ключом удаления. Возвращает способ перезапустить связь поверх той же базы.
def isolated_services(tmp_path, ack_attempts=None):
    upstream = create_studio(
        database_url=f"sqlite:///{tmp_path}/studio.sqlite",
        media_dir=tmp_path / "media",
        provider="demo",
        bridge_erasure_key=ERASURE_KEY,
    )
    path = tmp_path / "bridge.sqlite"

    class Transport(httpx.ASGITransport):
        # Перед подтверждением удаления сверяет оставшиеся записи. При первом подтверждении может
        # вернуть временный отказ, чтобы проверить повтор после фактической очистки.
        async def handle_async_request(self, request):
            if ack_attempts is not None and request.url.path.endswith("/account-erasures/ack"):
                owners = json.loads(request.content)["owners"]
                ack_attempts.append([owned_row_counts(path, owner) for owner in owners])
                if len(ack_attempts) == 1:
                    return httpx.Response(503, json={"detail": "Temporary acknowledgment failure"})
            return await super().handle_async_request(request)

    # Открывает пробную связующую службу поверх прежнего файла базы и подключает обмен с основным
    # сервером. Это сохраняет данные между остановкой и новым запуском.
    def bridge():
        app = create_bridge(
            upstream,
            path,
            transport_class=Transport,
            poll_seconds=0.1,
            erasure_key=ERASURE_KEY,
            erasure_poll_seconds=0.1,
        )
        return TestClient(app, base_url="https://bridge.test")

    return TestClient(upstream), bridge, path


# Регистрирует отдельного владельца через мобильный путь и возвращает профиль. Каждый проверяемый
# аккаунт получает свои сеанс и строки истории.
def register(bridge, email):
    profile, _ = register_mobile(bridge, email, PASSWORD)
    return profile


# Собирает заголовок входа для связующей службы из ключа мобильного профиля. Запросы этого
# владельца используют то же представление, что и телефон.
def authorization(profile):
    return {"Authorization": f"Bearer {profile['token']}"}


# Переводит ключ телефона в вид, который ожидает основной сервер. Так проверка может обратиться к
# обеим службам от одного владельца.
def source_authorization(profile):
    token = base64.urlsafe_b64encode(bytes.fromhex(profile["token"])).decode().rstrip("=")
    return {"Authorization": f"Bearer {token}"}


# Создаёт чат, подписывается на события и отправляет пробное сообщение. Служба должна подтвердить
# приём, чтобы до удаления были реальные записи для сверки.
def seed_history(bridge, headers):
    chat = bridge.post("/v1/demo/chats", headers=headers, json={}).json()["chatId"]
    with bridge.websocket_connect("/v1/ws", headers=headers) as socket:
        socket.send_json({"type": "resume", "cursor": 0})
        assert socket.receive_json()["type"] == "resumed"
        socket.send_json(
            {
                "type": "send",
                "requestId": "history",
                "messages": [
                    {
                        "clientId": str(uuid.uuid4()),
                        "chatId": chat,
                        "text": "Private account deletion test history",
                        "attachments": [],
                    }
                ],
            }
        )
        assert socket.receive_json()["type"] == "accepted"


# Считает записи владельца в таблицах связующей службы. Эти числа позволяют проверить очистку даже
# когда доступ к истории уже закрыт.
def owned_row_counts(path, owner):
    with closing(sqlite3.connect(path)) as db:
        return [
            db.execute(f"SELECT COUNT(*) FROM {table} WHERE owner=?", (owner,)).fetchone()[0]
            for table in TABLES
        ]


# Удаляет владельца в основном сервере при остановленной связующей службе, затем запускает её.
# Сверяет отказ второму устройству, две попытки подтверждения после очистки и сохранность другого
# владельца.
def test_direct_erasure_reconciles_after_bridge_restart_and_revokes_other_device(
    tmp_path,
):
    ack_attempts = []
    upstream, bridge, path = isolated_services(tmp_path, ack_attempts)
    email = "erase-offline@example.com"
    feed = "/v1/internal/account-erasures"
    key = {"X-Kandinsky-Erasure-Key": ERASURE_KEY}
    with upstream:
        with bridge() as client:
            other = register(client, "keep@example.com")
            seed_history(client, authorization(other))
            owner = register(client, email)
            seed_history(client, authorization(owner))
            second = client.post(
                "/v1/demo/login", json={"email": email, "password": PASSWORD}
            ).json()
        source = source_authorization(owner)
        owner_id = upstream.get("/v1/auth/me", headers=source).json()["user_id"]
        other_id = upstream.get("/v1/auth/me", headers=source_authorization(other)).json()[
            "user_id"
        ]
        foreign_before = owned_row_counts(path, other_id)
        assert all(owned_row_counts(path, owner_id)[:4])
        assert upstream.get(feed).status_code == 403
        deleted = upstream.request(
            "DELETE", "/v1/auth/me", headers=source, json={"password": PASSWORD}
        )
        assert deleted.status_code == 204
        assert upstream.get(feed, headers=key).json() == {"owners": [owner_id]}
        assert owned_row_counts(path, owner_id)[0] > 0
        with bridge() as client:
            with client.websocket_connect("/v1/ws", headers=authorization(second)) as socket:
                assert socket.receive_json() == {
                    "type": "error",
                    "code": "account_deleted",
                    "retryable": False,
                }
            assert client.get("/v1/chats", headers=authorization(second)).json() == {
                "error": {"code": "account_deleted"}
            }
            deadline = time.monotonic() + 4
            while upstream.get(feed, headers=key).json()["owners"] and time.monotonic() < deadline:
                time.sleep(0.025)
            assert upstream.get(feed, headers=key).json() == {"owners": []}
            assert owned_row_counts(path, owner_id) == [0] * len(TABLES)
            assert owned_row_counts(path, other_id) == foreign_before
            assert ack_attempts == [[[0] * len(TABLES)]] * 2
            with pytest.raises(ErasedAccount):
                State(path).identity(owner_id, "thread", str(uuid.uuid4()))
            recreated = register(client, email)
            assert recreated["userId"] > owner["userId"]


# Проверяет отказ мобильного удаления с неверным паролем и отказ формы без подтверждения. Затем
# проверяет оба разрешённых пути, очистку базы и отзыв доступа.
def test_browser_and_mobile_erase_only_after_confirmed_password(tmp_path):
    upstream, bridge, path = isolated_services(tmp_path)
    with upstream, bridge() as client:
        email = "erase-web@example.com"
        profile = register(client, email)
        seed_history(client, authorization(profile))
        owner = upstream.get("/v1/auth/me", headers=source_authorization(profile)).json()["user_id"]
        assert (
            client.request(
                "DELETE",
                "/v1/kandinsky/account",
                headers=authorization(profile),
                json={"password": "wrong-credential-123"},
            ).status_code
            == 403
        )
        page = client.get("/account/delete")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {"email": email, "password": PASSWORD, "csrf": csrf}
        assert client.post("/account/delete", data=form).status_code == 400
        assert client.post("/account/delete", data={**form, "confirm": "yes"}).status_code == 200
        assert owned_row_counts(path, owner) == [0] * len(TABLES)
        assert client.get("/v1/chats", headers=authorization(profile)).status_code == 401
        another = register(client, "erase-mobile@example.com")
        assert (
            client.request(
                "DELETE",
                "/v1/kandinsky/account",
                headers=authorization(another),
                json={"password": PASSWORD},
            ).status_code
            == 204
        )


# Проверяет отказы для неверной формы и сбой удаления после правильного входа. Страница сообщает о
# незавершённой операции и пытается закрыть временный сеанс. Прежний вход владельца продолжает
# работать.
@pytest.mark.parametrize("logout_works", [True, False])
def test_browser_reports_failed_deletion_and_attempts_to_close_its_session(
    tmp_path, monkeypatch, logout_works
):
    upstream, bridge, _ = isolated_services(tmp_path)
    with upstream, bridge() as client:
        email = "web-erasure-failure@example.com"
        profile = register(client, email)
        assert client.get("/account/delete").status_code == 200

        # Собирает форму с текущей проверочной отметкой и заменяет отдельные поля по условиям
        # проверки. Все варианты отправляются одним путём.
        def submit(**overrides):
            form = {
                "email": email,
                "password": PASSWORD,
                "csrf": client.cookies.get(COOKIE),
                "confirm": "yes",
                **overrides,
            }
            return client.post("/account/delete", data=form)

        assert client.post("/account/delete", json={"email": email}).status_code == 400
        assert submit(email="a" * 8192).status_code == 413
        assert submit(csrf="expired-form").status_code == 400
        assert submit(email="not-an-email").status_code == 400
        assert submit(password="wrong-password-123").status_code == 401

        adapter = client.app.state.adapter
        send = adapter.studio.request
        created_tokens = []
        logout_tokens = []

        # Вызывает отказ удаления и при необходимости отказ выхода, остальные вызовы передаёт
        # пробному серверу. Запоминает созданный и закрываемый ключи, чтобы сверить, какой сеанс
        # затронут.
        async def fail_erase(method, route, token=None, **options):
            if method == "DELETE" and route == "/v1/auth/me":
                raise HTTPException(503, "Очистка временно недоступна")
            if route == "/v1/auth/logout":
                logout_tokens.append(token)
                if not logout_works:
                    raise HTTPException(503, "Выход временно недоступен")
            result = await send(method, route, token, **options)
            if route == "/v1/auth/login":
                created_tokens.append(result["token"])
            return result

        monkeypatch.setattr(adapter.studio, "request", fail_erase)
        failure = submit()
        assert failure.status_code == 503
        assert "Не удалось подтвердить удаление" in failure.text
        assert "Удаление аккаунта" in failure.text
        assert len(created_tokens) == 1 and logout_tokens == created_tokens
        temporary = {"Authorization": f"Bearer {created_tokens[0]}"}
        assert upstream.get("/v1/auth/me", headers=temporary).status_code == (
            401 if logout_works else 200
        )
        assert upstream.get("/v1/auth/me", headers=source_authorization(profile)).status_code == 200
