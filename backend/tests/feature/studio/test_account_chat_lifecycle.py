# Проверяет восстановление входа и удаление чатов через сервер. Здесь важны уже выданные права,
# повторные запросы и файлы, которые могут быть нужны другой истории.

import asyncio

from fastapi.testclient import TestClient

from app.Models import Job, PasswordReset, Transfer
from app.Services.Accounts import password_reset
from app.Services.Generation.process_generation import process_one
from app.Services.Generation.queue import claim
from tests.support.fixtures import account
from tests.support.fixtures import create_studio as create_app

RESET = "/v1/auth/password-reset"


# Запрашивает восстановление для настоящего и неизвестного адресов и сравнивает ответы. Проверяет
# отказ по неверному коду, однократное принятие верного, отзыв старых сеансов и сохранение
# истории. Иначе по ответу можно было бы узнать, кто зарегистрирован, или продолжить вход со
# старым паролем.
def test_reset_revokes_sessions_without_disclosing_account(tmp_path):
    sent = []
    with TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path}/db",
            provider="demo",
            media_dir=tmp_path / "media",
            password_reset_secret="local-test-secret" * 4,
            send_recovery=lambda email, code: sent.append((email, code)),
        )
    ) as client:
        email = "owner@example.com"
        popular_password = "123456789012"
        assert (
            client.post(
                "/v1/auth/register", json={"email": email, "password": popular_password}
            ).status_code
            == 422
        )
        first = account(client, email)
        second = client.post(
            "/v1/auth/login", json={"email": email, "password": "long-password-123"}
        ).json()["token"]
        thread = client.post("/v1/threads", headers=first).json()["id"]

        unknown = client.post(f"{RESET}/request", json={"email": "absent@example.com"})
        known = client.post(f"{RESET}/request", json={"email": email})
        assert known.status_code == unknown.status_code == 202
        assert known.json() == unknown.json()
        assert len(sent) == 1 and sent[0][0] == email and sent[0][1].isdigit()
        client.post(f"{RESET}/request", json={"email": email})
        assert len(sent) == 1

        reset = {"email": email, "code": sent[0][1], "password": "new-long-password-42"}
        assert (
            client.post(
                f"{RESET}/confirm", json={**reset, "password": popular_password}
            ).status_code
            == 422
        )
        assert client.post(f"{RESET}/confirm", json=reset).status_code == 204
        assert client.post(f"{RESET}/confirm", json=reset).status_code == 400
        assert client.get("/v1/auth/me", headers=first).status_code == 401
        assert (
            client.get("/v1/auth/me", headers={"Authorization": f"Bearer {second}"}).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/auth/login", json={"email": email, "password": "long-password-123"}
            ).status_code
            == 401
        )
        logged_in = client.post(
            "/v1/auth/login", json={"email": email, "password": reset["password"]}
        )
        assert logged_in.status_code == 200
        new = {"Authorization": f"Bearer {logged_in.json()['token']}"}
        assert client.get("/v1/threads", headers=new).json()[0]["id"] == thread


# Проверяет два отказа, когда отправка письма недоступна и когда число неверных попыток исчерпано.
# После исчерпания отклоняется даже верный код, иначе ограничение не защищало бы от подбора.
def test_reset_exhausts_attempts_and_reports_unavailable_mail(tmp_path):
    sent = []
    settings = {"database_url": f"sqlite:///{tmp_path}/db", "media_dir": tmp_path / "media"}
    with TestClient(create_app(**settings)) as client:
        assert (
            client.post(f"{RESET}/request", json={"email": "owner@example.com"}).status_code == 503
        )
    with TestClient(
        create_app(
            **settings,
            password_reset_secret="x" * 40,
            send_recovery=lambda _, code: sent.append(code),
        )
    ) as client:
        email = "owner@example.com"
        account(client, email)
        assert client.post(f"{RESET}/request", json={"email": email}).status_code == 202
        wrong = "00000000" if sent[0] != "00000000" else "11111111"
        payload = {"email": email, "code": wrong, "password": "new-long-password-42"}
        for _ in range(5):
            assert client.post(f"{RESET}/confirm", json=payload).status_code == 400
        assert client.post(f"{RESET}/confirm", json={**payload, "code": sent[0]}).status_code == 400


# Даёт владельцу переименовать и удалить чат, а чужому аккаунту отказывает. Повтор удаления
# безопасен, поздний запрос не возвращает удалённый чат и не создаёт в нём задание.
def test_chat_rename_delete_and_late_retry(client):
    owner = account(client)
    stranger = account(client, "stranger@example.com")
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    uri = f"/v1/threads/{thread}"
    assert client.patch(uri, json={"title": "Чужой чат"}, headers=stranger).status_code == 404
    assert (
        client.patch(uri, json={"title": "  Семейные фото  "}, headers=owner).json()["title"]
        == "Семейные фото"
    )
    request = dict(thread_id=thread, prompt="Белая сирень", model="image", aspect="1:1", seed=4)
    headers = {**owner, "Idempotency-Key": "first-generation-key-01"}
    job = client.post("/v1/jobs", headers=headers, json=request).json()["id"]
    assert client.delete(uri, headers=stranger).status_code == 404
    assert client.delete(uri, headers=owner).status_code == 204
    assert client.delete(uri, headers=owner).status_code == 204
    assert client.get("/v1/threads", headers=owner).json() == []
    assert client.get(f"/v1/jobs/{job}", headers=owner).status_code == 404
    assert client.post("/v1/threads", headers=owner, json={"id": thread}).status_code == 410
    assert client.post("/v1/jobs", headers=headers, json=request).status_code == 410
    endpoint = "/v1/threads/deletions"
    assert client.post(endpoint, headers=owner, json={"ids": [thread]}).json() == [thread]
    assert client.post(endpoint, headers=stranger, json={"ids": [thread]}).json() == []


# Готовит результат, получает право скачивания и удаляет чат. Проверяет исчезновение записи и
# файла и отказ по прежнему разрешению. Иначе удалённое содержимое ещё можно было бы скачать.
def test_chat_removes_its_result_and_revokes_download(client):
    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    request = dict(thread_id=thread, prompt="Кувшинка", model="image", aspect="1:1", seed=1)
    job = client.post(
        "/v1/jobs", headers={**owner, "Idempotency-Key": "remove-generation-key-01"}, json=request
    ).json()
    factory = client.app.state.factory
    settings = client.app.state.settings
    assert asyncio.run(process_one(factory, settings))
    asset = client.get(f"/v1/jobs/{job['id']}", headers=owner).json()["output_id"]
    link = client.post(f"/v1/assets/{asset}/ticket", headers=owner).json()["path"]
    assert len(list(settings.media_dir.iterdir())) == 1
    assert client.delete(f"/v1/threads/{thread}", headers=owner).status_code == 204
    assert not list(settings.media_dir.iterdir())
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 404
    assert client.get(link).status_code == 401
    with factory() as db:
        assert db.query(Transfer).count() == 0


# Даёт обработчику взять задание, просрочить срок работы и взять его снова. Первое время запуска
# остаётся в ответе сервера, иначе повтор искажал бы время начала.
def test_lease_keeps_first_start_time(client):
    owner = account(client)
    thread = client.post("/v1/threads", headers=owner).json()["id"]
    request = dict(thread_id=thread, prompt="Листья", model="image", aspect="1:1")
    job = client.post(
        "/v1/jobs", headers={**owner, "Idempotency-Key": "running-generation-key-01"}, json=request
    ).json()
    assert job["started_at"] is None
    factory = client.app.state.factory
    first = claim(factory, 90)
    with factory() as db:
        db.get(Job, first.id).lease_until = 0
        db.commit()
    assert claim(factory, 90).started_at == first.started_at
    remote = client.get(f"/v1/jobs/{job['id']}", headers=owner).json()
    assert remote["started_at"] == first.started_at and first.started_at >= job["created_at"]


# Использует готовый файл из одного чата как исходник для другого. После удаления первого чата
# файл ещё нужен второму. После удаления последнего он исчезает, иначе останется лишняя копия.
def test_generated_input_is_kept_until_its_last_chat_is_deleted(client):
    owner = account(client)
    first = client.post("/v1/threads", headers=owner).json()["id"]
    second = client.post("/v1/threads", headers=owner).json()["id"]
    photo = client.post(
        "/v1/jobs",
        headers={**owner, "Idempotency-Key": "share-generated-photo-01"},
        json={"thread_id": first, "prompt": "Фото", "model": "image"},
    ).json()
    assert asyncio.run(process_one(client.app.state.factory, client.app.state.settings))
    asset = client.get(f"/v1/jobs/{photo['id']}", headers=owner).json()["output_id"]
    video = client.post(
        "/v1/jobs",
        headers={**owner, "Idempotency-Key": "share-generated-video-01"},
        json={"thread_id": second, "prompt": "Движение", "model": "video", "input_id": asset},
    )
    assert video.status_code == 201
    assert client.delete(f"/v1/threads/{first}", headers=owner).status_code == 204
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 200
    assert client.delete(f"/v1/threads/{second}", headers=owner).status_code == 204
    assert client.get(f"/v1/assets/{asset}", headers=owner).status_code == 404


# Задаёт известные коды, искусственно состаривает первый и запрашивает второй. Старый должен
# перестать подходить, а новый должен менять пароль. Иначе перевыпуск мог бы оставить два
# пригодных кода.
def test_reissued_reset_code_replaces_expired_code(tmp_path, monkeypatch):
    sent = []
    app = create_app(
        database_url=f"sqlite:///{tmp_path}/db",
        media_dir=tmp_path / "media",
        password_reset_secret="local-recovery-test-secret" * 3,
        send_recovery=lambda _, code: sent.append(code),
    )
    with TestClient(app) as client:
        email = "reset-reissue@example.com"
        account(client, email)
        codes = iter([12345678, 87654321])
        monkeypatch.setattr(password_reset.secrets, "randbelow", lambda _: next(codes))
        assert client.post(f"{RESET}/request", json={"email": email}).status_code == 202
        with app.state.factory.begin() as db:
            db.query(PasswordReset).one().requested_at = 0
        assert client.post(f"{RESET}/request", json={"email": email}).status_code == 202
        assert sent == ["12345678", "87654321"]
        payload = {"email": email, "password": "new-recovery-fixture-password-42"}
        assert client.post(f"{RESET}/confirm", json={**payload, "code": sent[0]}).status_code == 400
        assert client.post(f"{RESET}/confirm", json={**payload, "code": sent[1]}).status_code == 204
