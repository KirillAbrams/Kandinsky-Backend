# Собирает вспомогательные шаги для проверок сервера и службы связи. Общая подготовка поднимает
# отдельные базы, создаёт владельцев и ставит задания без обращений к рабочему окружению.

from pathlib import Path
from uuid import UUID

import httpx
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MOBILE_FIXTURES = BACKEND_ROOT / "tests" / "fixtures"
BRIDGE_TEST_PASSWORD = "fixture-password-only-123"


# Переносит настройки обновлений в свежий объект, не подключая настройки журнала. Иначе запуск
# обновлений внутри проверки поменял бы способы вывода журнала для всего прогона.
def migration_config():
    saved = Config(str(BACKEND_ROOT / "alembic.ini"))
    config = Config()
    # При запуске миграций из проверки не нужно менять её настройки журнала.
    for name, value in saved.get_section(saved.config_ini_section).items():
        config.set_main_option(name, value.replace("%", "%%"))
    return config


# Создаёт приложение с переданными настройками и сохраняет письма регистрации в словаре. Код можно
# взять из памяти после запроса без подключения к почтовой службе.
def create_studio(**settings):
    from bootstrap.studio import create_app

    mailbox = {}
    settings.setdefault("password_reset_secret", "isolated-registration-test-secret" * 2)
    app = create_app(send_registration=mailbox.__setitem__, **settings)
    app.state.registration_mailbox = mailbox
    return app


# Запрашивает письмо через выбранный маршрут, берёт присланный код и завершает регистрацию. По
# одному общему шагу можно проверять и основной сервер, и вход с телефона.
def register_account(client, email, password, *, mobile=False):
    path = "/v1/demo/register" if mobile else "/v1/auth/register"
    request = client.post(path + "/request", json={"email": email})
    assert request.status_code == 202, request.text
    code = client.app.state.registration_mailbox.pop(email.lower())
    return client.post(path, json={"email": email, "password": password, "code": code})


# Регистрирует проверочного владельца и возвращает заголовок входа для дальнейших запросов. Каждый
# сценарий получает ключ настоящей регистрации и не подставляет владельца напрямую.
def account(client, email="one@example.com"):
    response = register_account(client, email, "long-password-123")
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


# Создаёт чат и отправляет обычное задание с переданными отличиями. Возвращает исходный ответ,
# чтобы сценарий мог проверить как успешное принятие, так и конкретный отказ.
def job(client, headers, key="operation-key-000001", **overrides):
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    body = dict(thread_id=thread, prompt="Магнолия", model="image", aspect="1:1", seed=42)
    body.update(overrides)
    return client.post("/v1/jobs", headers={**headers, "Idempotency-Key": key}, json=body)


# Направляет запросы службы связи прямо в проверочный сервер, передаёт путь к отдельной базе и
# открывает общий почтовый ящик. Проверки могут подменить передачу запросов, чтобы воспроизвести
# обрыв связи.
def create_bridge(studio, state_path, *, transport_class=httpx.ASGITransport, **settings):
    from bootstrap.bridge import create_app

    app = create_app(
        "http://studio.test", state_path, transport=transport_class(app=studio), **settings
    )
    app.state.registration_mailbox = studio.state.registration_mailbox
    return app


# Создаёт основной сервер и службу связи с разными файлами базы во временной папке. Возвращает
# двух клиентов, чтобы тест мог сверить ответ телефона с источником данных.
def integration_clients(
    tmp_path, erasure_key="", *, transport_class=httpx.ASGITransport, retry_seconds=3600
):
    upstream = create_studio(
        database_url=f"sqlite:///{tmp_path}/studio.sqlite",
        media_dir=tmp_path / "media",
        provider="demo",
        bridge_erasure_key=erasure_key,
    )
    bridge = create_bridge(
        upstream,
        tmp_path / "bridge.sqlite",
        transport_class=transport_class,
        poll_seconds=30,
        erasure_key=erasure_key,
        erasure_poll_seconds=retry_seconds,
    )
    return TestClient(upstream), TestClient(bridge)


# Регистрирует владельца через телефон и сверяет форму номера и ключа. Возвращает профиль и
# заголовок для дальнейших запросов от этого же владельца.
def register_mobile(client, email, password=BRIDGE_TEST_PASSWORD):
    result = register_account(client, email, password, mobile=True)
    assert result.status_code == 201
    profile = result.json()
    assert profile["userId"] > 0 and len(profile["token"]) == 64
    return profile, {"Authorization": "Bearer " + profile["token"]}


# Быстро заполняет длинный чат известными номерами и возвращает ожидаемый порядок. Несколько
# соседних записей получают одно время, чтобы проверка заметила пропуск на границе страниц.
def seed_history(client, headers, count=10000):
    from app.Models import Job, User

    thread = client.post("/v1/threads", headers=headers).json()["id"]
    with client.app.state.factory() as db:
        owner = db.scalar(select(User.id).where(User.email == "one@example.com"))
        expected = [str(UUID(int=i + 1)) for i in range(count)]
        db.execute(
            insert(Job),
            [
                dict(
                    id=key,
                    user_id=owner,
                    thread_id=thread,
                    operation_key=f"history-operation-{i:020}",
                    fingerprint="f" * 64,
                    prompt=f"Сохранённый запрос {i}",
                    model="image",
                    aspect="1:1",
                    seed=42,
                    status="cancelled",
                    provider="demo",
                    created_at=1000.0 + i // 3,
                )
                for i, key in enumerate(expected)
            ],
        )
        db.commit()
    return thread, expected
