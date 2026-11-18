# Проверяет признаки работы и порядок чатов, а также число обращений к базе для большого списка.
# Повтор запроса не должен менять порядок или повторно уведомлять владельца.

"""Список отражает всю очередь чата, даже если телефон не загружал его историю."""

from uuid import uuid4

from sqlalchemy import event, insert, select, update

from app.Models import Job, Thread, User
from tests.support.fixtures import account, seed_history


# Запускает несколько частей запроса и сверяет, что чат занят, пока хотя бы одна часть ещё
# работает. Проверяет, что чужой список не видит это состояние.
def test_busy_summary_tracks_every_member_and_stays_owner_scoped(client):
    headers = account(client)
    chat, jobs = seed_history(client, headers, 4)
    foreign = account(client, "foreign-directory@example.com")
    for statuses, expected in [
        (["queued"] * 4, True),
        (["succeeded", "failed", "cancelled", "running"], True),
        (["succeeded", "succeeded", "succeeded", "queued"], True),
        (["succeeded", "failed", "cancelled", "succeeded"], False),
    ]:
        with client.app.state.factory() as db:
            for key, status in zip(jobs, statuses, strict=True):
                db.execute(update(Job).where(Job.id == key).values(status=status))
            db.commit()
        rows = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()
        assert rows[0]["id"] == chat
        assert rows[0].get("generating") is expected, statuses
        states = client.post("/v1/threads/states", headers=headers, json={"ids": [chat]}).json()
        assert states["threads"][0].get("generating") is expected
        assert (
            client.post("/v1/threads/states", headers=foreign, json={"ids": [chat]}).json()[
                "threads"
            ]
            == []
        )


# Закрепляет чаты в порядке, который отличается от порядка запросов. Сравнение выводит ошибку,
# если список сортируется по времени закрепления.
def test_pinned_chats_order_by_last_request_not_pin_time(client):
    headers = account(client)
    first = client.post("/v1/threads", headers=headers).json()["id"]
    second = client.post("/v1/threads", headers=headers).json()["id"]
    with client.app.state.factory() as db:
        db.execute(update(Thread).where(Thread.id == first).values(pinned_at=10, activity_at=30))
        db.execute(update(Thread).where(Thread.id == second).values(pinned_at=20, activity_at=25))
        db.commit()
    rows = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()
    assert [row["id"] for row in rows] == [first, second]


# Принимает новый запрос в старом чате и повторяет его с тем же номером после имитации потери
# ответа. Только первая операция сдвигает чат вверх.
def test_old_chat_promotes_once_when_response_is_lost(client):
    headers = account(client)
    old = client.post("/v1/threads", headers=headers).json()["id"]
    newer = client.post("/v1/threads", headers=headers).json()["id"]
    with client.app.state.factory() as db:
        db.execute(update(Thread).where(Thread.id == old).values(activity_at=1))
        db.execute(update(Thread).where(Thread.id == newer).values(activity_at=2))
        db.commit()
    headers = {**headers, "Idempotency-Key": "drawer-lost-response-0001"}
    body = {
        "thread_id": old,
        "prompt": "Апельсиновое дерево",
        "model": "image",
        "aspect": "1:1",
        "seed": 42,
    }
    first = client.post("/v1/jobs", headers=headers, json=body)
    assert first.status_code == 201, first.text
    before = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()
    repeated = client.post("/v1/jobs", headers=headers, json=body)
    assert repeated.json()["id"] == first.json()["id"]
    after = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()
    assert [row["id"] for row in after] == [old, newer]
    assert after[0]["activity_at"] == before[0]["activity_at"]


# Повторяет неудачную генерацию и сверяет обновление времени чата. Повтор того же действия не
# должен переставлять чат, иначе восстановление связи меняло бы список ещё раз.
def test_retry_promotes_chat_once_without_reordering_on_replay(client):
    headers = account(client)
    chat, jobs = seed_history(client, headers, 1)
    with client.app.state.factory() as db:
        db.execute(update(Thread).where(Thread.id == chat).values(activity_at=1))
        db.execute(update(Job).where(Job.id == jobs[0]).values(status="failed"))
        db.commit()
    params = {"operation": str(uuid4())}
    path = f"/v1/jobs/{jobs[0]}/retry"
    accepted = client.post(path, headers=headers, params=params)
    assert accepted.status_code == 200, accepted.text
    first = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()[0]
    assert first["activity_at"] > 1
    replay = client.post(path, headers=headers, params=params)
    assert replay.status_code == 200
    second = client.get("/v1/threads", headers=headers, params={"view": "recent"}).json()[0]
    assert second["activity_at"] == first["activity_at"]


# Открывает список из двухсот чатов и считает обращения к базе. Список и признаки работы
# загружаются двумя запросами, без отдельного запроса истории для каждого чата.
def test_two_reads_cover_two_hundred_chats_without_history_queries(client):
    headers = account(client)
    with client.app.state.factory() as db:
        owner = db.scalar(select(User.id).where(User.email == "one@example.com"))
        db.execute(insert(Thread), [{"id": str(uuid4()), "user_id": owner} for _ in range(200)])
        db.commit()
        engine = db.get_bind()
    reads = []

    # Собирает обращения к базе во время чтения списка. По ним проверка может обнаружить отдельный
    # запрос для каждого чата, даже когда ответ получился верным.
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("SELECT") and any(
            name in statement for name in ("FROM threads", "FROM jobs")
        ):
            reads.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get("/v1/threads", headers=headers, params={"view": "recent"})
        assert response.status_code == 200
        assert len(response.json()) == 200
        assert len(reads) == 2
        assert "jobs.status IN" in reads[1]
        assert "jobs.user_id =" in reads[1]
    finally:
        event.remove(engine, "before_cursor_execute", record)


# Сравнивает уведомления после принятия запроса и повторной выдачи того же ответа. Владелец должен
# получить один сигнал, иначе телефон напрасно перечитал бы состояние.
def test_accepted_request_notifies_owner_once_even_when_reply_is_replayed(client, monkeypatch):
    from app.Services.Generation import submit_jobs as generation

    headers = account(client)
    owner = client.get("/v1/auth/me", headers=headers).json()["user_id"]
    chat = client.post("/v1/threads", headers=headers).json()["id"]
    notices = []
    original = generation.notify

    # Перехватывает сообщение об изменении во время записи и сохраняет сведения о получателе. Так
    # видно, кто получил уведомление и сколько раз его отправили.
    def notify(db, change):
        assert db.in_transaction()
        notices.append(change)
        original(db, change)

    monkeypatch.setattr(generation, "notify", notify)
    body = {"thread_id": chat, "prompt": "Дерево", "model": "image", "aspect": "1:1", "seed": 42}
    headers = {**headers, "Idempotency-Key": "drawer-notice-operation-001"}
    assert client.post("/v1/jobs", headers=headers, json=body).status_code == 201
    assert client.post("/v1/jobs", headers=headers, json=body).status_code == 201
    assert notices.count("owner:" + owner) == 1
    assert notices.count("queue") == 1
