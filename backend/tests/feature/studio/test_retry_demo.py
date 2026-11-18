# Проверяет, что повтор одной неудачной генерации сохраняет соседние результаты и не создаёт
# дубликатов при потере ответа.

import asyncio
import uuid

from sqlalchemy import func, select

from app.Models import Job
from app.Services.Generation.process_generation import process_one
from tests.support.fixtures import account


# Даёт первым трём позициям завершиться, а четвёртой завершиться ошибкой. Повторяет запрос на
# четвёртую и сверяет тот же запуск, те же три файла и ровно четыре результата в конце.
def test_four_results_survive_replayed_retry(client):
    from tests.feature.studio.retry_demo_worker import RetryDemoProvider

    auth = account(client)
    chat = client.post("/v1/threads", headers=auth).json()["id"]
    response = client.post(
        "/v1/jobs/batch",
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "thread_id": chat,
            "prompt": "Магнолия",
            "model": "image",
            "aspect": "1:1",
            "seed": 42,
            "count": 4,
        },
    )
    assert response.status_code == 201, response.text
    job_ids = [row["id"] for row in response.json()]
    factory = client.app.state.factory
    settings = client.app.state.settings
    provider = RetryDemoProvider()
    for _ in range(4):
        assert asyncio.run(process_one(factory, settings, provider))

    batch_url = f"/v1/jobs/{job_ids[0]}/batch"
    before = client.get(batch_url, headers=auth).json()
    assert [row["status"] for row in before] == ["succeeded"] * 3 + ["failed"]
    first_outputs = [row["output_id"] for row in before[:3]]
    assert len(set(first_outputs)) == 3 and all(first_outputs)
    retry_url = f"/v1/jobs/{job_ids[3]}/retry"
    operation = {"operation": str(uuid.uuid4())}
    accepted = client.post(retry_url, headers=auth, params=operation)
    assert accepted.status_code == 200
    assert asyncio.run(process_one(factory, settings, provider))
    replay = client.post(retry_url, headers=auth, params=operation)
    assert replay.status_code == 200
    assert replay.json()["run_id"] == accepted.json()["run_id"]
    after = client.get(batch_url, headers=auth).json()
    assert [row["id"] for row in after] == job_ids
    assert [row["status"] for row in after] == ["succeeded"] * 4
    assert [row["output_id"] for row in after[:3]] == first_outputs
    assert after[3]["output_id"] and after[3]["output_id"] not in first_outputs
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 4
