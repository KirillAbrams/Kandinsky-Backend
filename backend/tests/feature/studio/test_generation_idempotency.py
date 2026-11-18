# Проверяет повтор запросов на генерацию, старые номера запусков и целостность записей при сбоях.
# Каждое действие сверяется с состоянием чата или задания после запроса.

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.Models import Job, Thread, User
from app.Services.Accounts.erasure import erase_account
from app.Services.Generation import remove_content, retry_generation, submit_jobs
from tests.support.fixtures import account, register_account
from tests.support.fixtures import create_studio as create_app


# Регистрирует владельца и отправляет один запрос дважды. Оба ответа указывают на одну запись в
# истории. Другой текст с тем же номером отклоняется.
def test_register_and_create_is_idempotent(tmp_path):
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path}/db", media_dir=tmp_path)) as c:
        auth = register_account(c, "kirill@example.com", "long-password-123")
        assert auth.status_code == 201, auth.text
        h = {"Authorization": f"Bearer {auth.json()['token']}"}
        thread = c.post("/v1/threads", headers=h).json()["id"]
        body = {
            "thread_id": thread,
            "prompt": "Белая магнолия у окна",
            "model": "image",
            "aspect": "1:1",
            "seed": 42,
        }
        h["Idempotency-Key"] = "interview-operation-001"
        a = c.post("/v1/jobs", json=body, headers=h)
        b = c.post("/v1/jobs", json=body, headers=h)
        assert a.status_code == 201, a.text
        assert a.json()["id"] == b.json()["id"]
        assert len(c.get(f"/v1/threads/{thread}/jobs", headers=h).json()) == 1
        body["prompt"] = "Другой запрос"
        assert c.post("/v1/jobs", json=body, headers=h).status_code == 409


# Отправляет запрос со слишком коротким номером операции и читает базу. После отказа нет задания и
# исходное название чата сохранено. Иначе ошибочный запрос мог бы изменить историю.
def test_bad_operation_key_does_not_create_or_rename_a_chat_job(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    rejected = client.post(
        "/v1/jobs",
        headers={**headers, "Idempotency-Key": "short"},
        json={"thread_id": thread, "prompt": "Магнолия"},
    )
    assert rejected.status_code == 422
    with client.app.state.factory() as db:
        assert db.get(Thread, thread).title == "Новый чат"
        assert list(db.scalars(select(Job))) == []


# Создаёт две части одной генерации и пытается применить номер второй части к отдельному запросу.
# Отказ сохраняет две исходные части без подмены.
def test_batch_member_key_cannot_be_reused_as_the_root(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    body = {"thread_id": thread, "prompt": "Магнолия", "seed": 42}
    created = client.post(
        "/v1/jobs/batch",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={**body, "count": 2},
    )
    assert created.status_code == 201, created.text
    _, member = created.json()
    rejected = client.post(
        "/v1/jobs",
        headers={**headers, "Idempotency-Key": member["operation_key"]},
        json={**body, "seed": 43},
    )
    assert rejected.status_code == 409
    assert len(client.get(f"/v1/threads/{thread}/jobs", headers=headers).json()) == 2


# Приостанавливает генерацию владельца после старого запроса. Повтор чтения возвращает прежнюю
# ошибку, запуск новой попытки запрещён. Иначе ограничение могло бы стереть видимость истории или
# допустить новую работу.
def test_suspended_owner_can_read_previous_submit_but_cannot_start_a_retry(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    body = {"thread_id": thread, "prompt": "Магнолия"}
    command = {**headers, "Idempotency-Key": str(uuid4())}
    created = client.post("/v1/jobs", headers=command, json=body)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    with client.app.state.factory() as db:
        row = db.get(Job, job_id)
        row.status = "failed"
        db.get(User, row.user_id).generation_suspended_at = row.created_at
        db.commit()

    replay = client.post("/v1/jobs", headers=command, json=body)
    assert replay.status_code == 201
    assert (replay.json()["id"], replay.json()["status"]) == (job_id, "failed")
    retry = client.post(
        f"/v1/jobs/{job_id}/retry", headers=headers, params={"operation": str(uuid4())}
    )
    assert retry.status_code == 403
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"] == "failed"


# Сначала запрашивает повтор с устаревшим номером запуска и сверяет, что последняя ошибка
# сохранилась. Повтор с нынешним номером даёт новую попытку. Так отложенный запрос не
# перезапускает более свежую работу.
def test_retry_rejects_previous_run_without_changing_the_latest_failure(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    job = client.post(
        "/v1/jobs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"thread_id": thread, "prompt": "Магнолия"},
    ).json()["id"]
    latest_run = str(uuid4())
    with client.app.state.factory() as db:
        row = db.get(Job, job)
        row.status = "failed"
        row.run_id = latest_run
        db.commit()
    rejected = client.post(
        f"/v1/jobs/{job}/retry",
        headers=headers,
        params={"operation": str(uuid4()), "expected_run": str(uuid4())},
    )
    assert rejected.status_code == 409
    state = client.get(f"/v1/jobs/{job}", headers=headers).json()
    assert (state["status"], state["run_id"]) == ("failed", latest_run)
    accepted = client.post(
        f"/v1/jobs/{job}/retry",
        headers=headers,
        params={"operation": str(uuid4()), "expected_run": latest_run},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "queued"
    assert accepted.json()["run_id"] != latest_run


# Имитирует отказ записи посередине принятия запроса. Проверяет, что исчезло задание и вернулось
# прежнее название чата. После восстановления связи тот же запрос создаёт задание.
def test_submit_rolls_back_the_job_and_chat_title_when_commit_fails(client, monkeypatch):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    command = {**headers, "Idempotency-Key": str(uuid4())}
    body = {"thread_id": thread, "prompt": "Магнолия"}
    session_class = client.app.state.factory.class_

    # Отправляет накопленные изменения в базу и вызывает ошибку перед подтверждением. Так проверка
    # ловит частично сохранённые данные, если откат работает неверно.
    def reject_commit(db):
        db.flush()
        raise IntegrityError(None, None, RuntimeError("isolated transaction rejected"))

    with monkeypatch.context() as patch:
        patch.setattr(session_class, "commit", reject_commit)
        assert client.post("/v1/jobs", headers=command, json=body).status_code == 409
    with client.app.state.factory() as db:
        assert db.get(Thread, thread).title == "Новый чат"
        assert list(db.scalars(select(Job))) == []
    created = client.post("/v1/jobs", headers=command, json=body)
    assert created.status_code == 201


# Удаляет владельца после проверки входа и до закрытия записи на время операции. При отправке,
# повторе или удалении текста должен прийти отказ. Чат и задания не появляются заново.
@pytest.mark.parametrize("action", ["submit", "retry", "remove"])
def test_generation_write_stops_if_owner_is_deleted_after_authentication(
    client, monkeypatch, action
):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    command = {**headers, "Idempotency-Key": str(uuid4())}
    body = {"thread_id": thread, "prompt": "Магнолия"}
    created = client.post("/v1/jobs", headers=command, json=body)
    assert created.status_code == 201
    job_id = created.json()["id"]
    factory = client.app.state.factory
    module = {"remove": remove_content, "submit": submit_jobs, "retry": retry_generation}[action]
    original = module.lock_user

    # Удаляет владельца через соседнее соединение и продолжает обычное чтение под блокировкой.
    # Действия происходят в нужном порядке без ожидания удачного момента.
    def erase_before_lock(db, *, id):
        with factory() as peer:
            erase_account(peer, peer.get(User, id))
        return original(db, id=id)

    monkeypatch.setattr(module, "lock_user", erase_before_lock)
    if action == "submit":
        response = client.post(
            "/v1/jobs", headers={**headers, "Idempotency-Key": str(uuid4())}, json=body
        )
    elif action == "retry":
        response = client.post(
            f"/v1/jobs/{job_id}/retry", headers=headers, params={"operation": str(uuid4())}
        )
    else:
        response = client.delete(
            f"/v1/jobs/{job_id}/contents", headers=headers, params={"target": "prompt"}
        )
    assert response.status_code == 401
    with factory() as db:
        assert list(db.scalars(select(Job))) == []
        assert db.get(Thread, thread) is None


# Помечает старую запись удалённой и пробует прочитать её и повторить исходную отправку. Оба пути
# должны сообщить об удалении, иначе повтор вернул бы старый запрос.
def test_legacy_prompt_removal_is_still_respected(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    command = {**headers, "Idempotency-Key": str(uuid4())}
    body = {"thread_id": thread, "prompt": "Магнолия"}
    created = client.post("/v1/jobs", headers=command, json=body)
    assert created.status_code == 201
    job_id = created.json()["id"]
    with client.app.state.factory.begin() as db:
        item = db.get(Job, job_id)
        item.status = "cancelled"
        item.prompt = ""
        item.prompt_removed = True
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).status_code == 410
    assert client.post("/v1/jobs", headers=command, json=body).status_code == 410


# Имитирует чтение, которое не увидело уже принятое задание. После конфликта при записи сервер
# возвращает исходный номер, а в базе остаётся одна запись.
def test_submit_returns_existing_operation_when_commit_loses_unique_key_race(client, monkeypatch):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    command = {**headers, "Idempotency-Key": str(uuid4())}
    payload = {"thread_id": thread, "prompt": "Магнолия"}
    created = client.post("/v1/jobs", headers=command, json=payload)
    assert created.status_code == 201
    factory = client.app.state.factory
    read = factory.class_.scalar
    missed = False

    # Скрывает первое чтение задания и пропускает последующие чтения. Вторая попытка вставки
    # встречает существующую запись, как при одновременном запросе.
    def stale_first_read(db, statement, *args, **kwargs):
        nonlocal missed
        selected = getattr(statement, "column_descriptions", [])
        if not missed and selected and selected[0].get("expr") is Job:
            missed = True
            return None
        return read(db, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(factory.class_, "scalar", stale_first_read)
        replay = client.post("/v1/jobs", headers=command, json=payload)
    assert missed
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    with factory() as db:
        assert len(list(db.scalars(select(Job)))) == 1
