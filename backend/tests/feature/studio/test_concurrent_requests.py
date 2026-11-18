# Проверяет одновременный приход одинаковых запросов через сервер и сверяет записи отдельно.
# Одинаковый ответ сам по себе ещё не доказывает отсутствие дублей в базе.

"""Сверяем число строк напрямую, чтобы проверка не повторяла правила очереди"""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from tests.support.fixtures import account


# Отправляет запросы с одним номером повтора и сверяет, что в базе создано одно задание.
# Изменённое содержимое с тем же номером отклоняется, иначе новый запрос мог бы получить чужой
# результат.
def test_concurrent_http_idempotency_independent_sql(client):
    h = account(client)
    thread = client.post("/v1/threads", headers=h).json()["id"]
    body = dict(thread_id=thread, prompt="Один запрос", model="image", seed=42, aspect="1:1")
    headers = {**h, "Idempotency-Key": "same-operation-123456"}
    with ThreadPoolExecutor(8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/v1/jobs", headers=headers, json=body), range(8))
        )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    with client.app.state.factory() as db:
        assert db.execute(text("SELECT count(*) FROM jobs")).scalar() == 1
    body["seed"] = 43
    assert client.post("/v1/jobs", headers=headers, json=body).status_code == 409


# Создаёт чат двумя одновременными запросами с одним номером операции. Сервер должен вернуть один
# и тот же чат и сохранить одну запись, иначе повтор разделил бы историю.
def test_thread_idempotency_under_concurrency(client):
    import uuid

    h = account(client)
    thread_id = str(uuid.uuid4())
    with ThreadPoolExecutor(5) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/v1/threads", headers=h, json={"id": thread_id}), range(5)
            )
        )
    assert all(r.status_code == 201 for r in responses)
    assert {r.json()["id"] for r in responses} == {thread_id}
    with client.app.state.factory() as db:
        assert db.execute(text("SELECT count(*) FROM threads")).scalar() == 1
