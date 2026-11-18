# Проверяет доставку команд удаления исполнителю генерации. Очередь должна пережить отказ связи
# или базы и закончить удаление без повторного создания результата.

"""Очередь стирания не теряет работу при недоступном исполнителе."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx

from app.Models import Job, PendingGpuRemoval
from app.Services.Chats.erasure_records import queue_gpu_removals
from tests.support.fixtures import account


# Создаёт пробное задание, назначает ему внешний запуск и удаляет текст. Возвращает номера задания
# и запуска для проверки очереди.
def queued_erasure(client):
    owner = account(client)
    chat = client.post("/v1/threads", headers=owner).json()["id"]
    job = client.post(
        "/v1/jobs",
        headers={**owner, "Idempotency-Key": str(uuid4())},
        json={"thread_id": chat, "prompt": "Remove private GPU job"},
    ).json()["id"]
    run = str(uuid4())
    with client.app.state.factory() as db:
        item = db.get(Job, job)
        item.provider, item.run_id = "kandinsky", run
        db.commit()
    assert client.delete(f"/v1/jobs/{job}/contents?target=prompt", headers=owner).status_code == 204
    return job, run


# Сначала вызывает отказ исполнителя и сверяет наличие записи об удалении. Затем возвращает
# подтверждение и проверяет удаление из очереди. Следующий проход больше не должен отправлять ту
# же команду.
def test_gpu_cleanup_retains_work_until_confirmed(client):
    from app.Jobs.erasure_delivery import clear_gpu_removals

    job, run = queued_erasure(client)
    factory = client.app.state.factory
    with factory() as db:
        pending = db.get(PendingGpuRemoval, job)
        assert pending is not None and pending.run_id == run
    cleanup = clear_gpu_removals
    calls = []

    class Endpoint:
        available = False

        # Запоминает обе части адреса команды и сообщает об отказе, пока исполнитель недоступен.
        # По списку вызовов видно, какой запуск повторили.
        async def erase(self, job_id, run_id):
            calls.append((job_id, run_id))
            if not self.available:
                raise httpx.ConnectError("isolated outage")

    endpoint = Endpoint()
    asyncio.run(cleanup(factory, endpoint))
    with factory() as db:
        assert db.get(PendingGpuRemoval, job) is not None
    endpoint.available = True
    asyncio.run(cleanup(factory, endpoint))
    with factory() as db:
        assert db.get(PendingGpuRemoval, job) is None
    assert calls == [(job, run), (job, run)]
    asyncio.run(cleanup(factory, endpoint))
    assert len(calls) == 2


# Вызывает отказ при первом открытии базы и ждёт доставки после восстановления. После остановки
# фоновой задачи проверяет, что команда ушла из очереди.
def test_cleanup_recovers_after_database_connection_loss(client):
    from sqlalchemy.exc import OperationalError

    from app.Jobs.erasure_delivery import deliver_gpu_removals

    job, _ = queued_erasure(client)
    attempts = 0

    # На первом вызове сообщает о потерянном соединении, дальше открывает обычную пробную базу.
    # Так проверяется повтор при временном отказе.
    def reconnecting():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("isolated connection lost", {}, None)
        return client.app.state.factory()

    reconnecting.begin = client.app.state.factory.begin

    # Запускает доставку с коротким сроком повтора, ждёт обращения к исполнителю и останавливает
    # свою фоновую задачу. Без завершения она продолжала бы работать после проверки.
    async def scenario():
        finished = asyncio.Event()

        class Endpoint:
            # Сверяет номер принятой команды и даёт сигнал ожиданию. Сигнал означает, что доставка
            # продолжилась после отказа базы.
            async def erase(self, job_id, run_id):
                assert job_id == job
                finished.set()

        task = asyncio.create_task(
            deliver_gpu_removals(reconnecting, Endpoint(), asyncio.Event(), retry_seconds=0.01)
        )
        try:
            await asyncio.wait_for(finished.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    with client.app.state.factory() as db:
        assert db.get(PendingGpuRemoval, job) is None


# Поднимает настоящий сетевой обработчик на этом компьютере и подменяет первое подтверждение
# удаления отказом. Сверяет исчезновение файла и текста, повтор очереди и запрет создания того же
# запуска.
def test_real_gpu_http_lost_ack_keeps_queue_then_recovers(client, tmp_path, monkeypatch):
    import socket
    import threading
    import time

    import uvicorn

    import app.Services.Gpu.Runner.bridge as gpu
    from app.Jobs.erasure_delivery import clear_gpu_removals
    from app.Services.Gpu.provider import GpuProvider

    job, run = queued_erasure(client)
    marker = "private-real-http-erasure-" + uuid4().hex
    monkeypatch.setattr(gpu, "ROOT", tmp_path / "gpu-http")
    monkeypatch.setattr(gpu, "TOKEN", "isolated-real-http-secret-" * 2)
    monkeypatch.setattr(gpu, "runner", lambda: gpu.STOP.wait())

    class LostAck:
        failed = False

        # Передаёт запросы настоящему обработчику и оборачивает отправку ответа. Так проверка
        # меняет только подтверждение, сохраняя удаление.
        async def __call__(self, scope, receive, send):
            # Меняет первый успешный ответ на удаление на временный отказ. Повтор отправляется без
            # подмены, иначе проверка не увидела бы окончательное подтверждение.
            async def respond(message):
                if (
                    scope["type"] == "http"
                    and scope["method"] == "DELETE"
                    and message["type"] == "http.response.start"
                    and message["status"] == 204
                    and not self.failed
                ):
                    self.failed = True
                    message = {**message, "status": 503}
                await send(message)

            await gpu.app(scope, receive, respond)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(LostAck(), log_level="critical"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with httpx.Client(
            base_url=address, headers={"Authorization": "Bearer " + gpu.TOKEN}, trust_env=False
        ) as http:
            response = http.post(
                "/jobs/" + run,
                json={
                    "canonical_job_id": job,
                    "prompt": marker,
                    "model": "image",
                    "aspect": "1:1",
                    "seed": 1,
                },
            )
            assert response.status_code == 200
            folder = gpu.ROOT / run
            folder.mkdir()
            (folder / "result.png").write_bytes(b"private output")
            provider = GpuProvider(address, gpu.TOKEN)
            asyncio.run(clear_gpu_removals(client.app.state.factory, provider))
            with client.app.state.factory() as db:
                assert db.get(PendingGpuRemoval, job) is not None
            assert not folder.exists()
            assert marker.encode() not in (gpu.ROOT / "jobs.db").read_bytes()
            asyncio.run(clear_gpu_removals(client.app.state.factory, provider))
            with client.app.state.factory() as db:
                assert db.get(PendingGpuRemoval, job) is None
            assert (
                http.post(
                    "/jobs/" + run,
                    json={
                        "canonical_job_id": job,
                        "prompt": marker,
                        "model": "image",
                        "aspect": "1:1",
                        "seed": 1,
                    },
                ).status_code
                == 410
            )
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


# Ставит удаление в очередь и снова добавляет ту же задачу с другим запуском. В очереди должен
# сохраниться исходный запуск, иначе команда могла бы уйти к другому результату.
def test_repeated_gpu_erasure_keeps_the_pending_attempt(client):
    job, attempt = queued_erasure(client)
    with client.app.state.factory.begin() as db:
        late = SimpleNamespace(id=job, run_id=str(uuid4()), provider="kandinsky")
        queue_gpu_removals(db, [late])
        assert db.get(PendingGpuRemoval, job).run_id == attempt
