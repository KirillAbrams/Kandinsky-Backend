# Проверяет захват задания, продление права обработки и выдачу результата. Отдельные сценарии
# воспроизводят отказ сети, отмену и поздний выход обработчика.

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.Models import Asset, Job
from app.Services.Generation.process_generation import process_one
from app.Services.Generation.queue import claim, record_progress, release_lease, renew
from tests.support.fixtures import MOBILE_FIXTURES, account, job


# Даёт шести исполнителям одновременно взять одно задание и проверяет единственного победителя.
# После истечения срока другой исполнитель получает работу, а прежний уже не меняет ход, срок или
# право обработки.
def test_claim_race_and_fencing_and_recovery(client):
    a = account(client)
    original = job(client, a).json()
    f = client.app.state.factory
    with ThreadPoolExecutor(6) as pool:
        claims = list(pool.map(lambda _: claim(f, 90), range(6)))
    assert sum(c is not None for c in claims) == 1
    owner = next(c for c in claims if c)
    record_progress(f, owner.id, owner.lease_owner, 43)
    record_progress(f, owner.id, owner.lease_owner, 24)
    assert client.get(f"/v1/jobs/{owner.id}", headers=a).json()["progress"] == 43
    with f() as db:
        item = db.get(Job, original["id"])
        item.lease_until = time.time() - 1
        db.commit()
    replacement = claim(f, 90)
    assert replacement.lease_owner != owner.lease_owner
    record_progress(f, owner.id, owner.lease_owner, 97)
    assert client.get(f"/v1/jobs/{owner.id}", headers=a).json()["progress"] is None
    assert not renew(f, owner.id, owner.lease_owner, 90)
    assert renew(f, replacement.id, replacement.lease_owner, 90)
    release_lease(f, owner.id, owner.lease_owner)
    with f() as db:
        assert db.get(Job, owner.id).lease_owner == replacement.lease_owner


# Доводит первое задание до файла и проверяет, что поздняя отмена не меняет готовый результат.
# Второе задание отменяет в очереди, после чего исполнитель больше не может его взять.
def test_worker_generates_and_cancel_is_terminal(client):
    a = account(client)
    created = job(client, a).json()
    assert asyncio.run(process_one(client.app.state.factory, client.app.state.settings))
    ready = client.get("/v1/jobs/" + created["id"], headers=a).json()
    assert ready["status"] == "succeeded" and ready["progress"] == 100
    assert ready["provider"] == "demo"
    file = client.get("/v1/assets/" + ready["output_id"], headers=a)
    assert file.status_code == 200 and file.content.startswith(b"\x89PNG")
    assert (
        client.post("/v1/jobs/" + created["id"] + "/cancel", headers=a).json()["status"]
        == "succeeded"
    )
    other = job(client, a, key="operation-key-000002").json()
    assert (
        client.post("/v1/jobs/" + other["id"] + "/cancel", headers=a).json()["status"]
        == "cancelled"
    )
    assert claim(client.app.state.factory, 90) is None


# Вызывает отказ исполнителя и читает состояние через сервер и через новое соединение с базой.
# Ошибка сохраняется, но внутренние подробности не попадают в ответ владельцу.
def test_provider_failure_visible_and_restart_durable(client):
    a = account(client)
    created = job(client, a).json()

    class Broken:
        # Возвращает ошибку времени с отдельным внутренним текстом. По этому тексту видно, если
        # исключение по ошибке раскрыли пользователю.
        async def generate(self, *args):
            raise TimeoutError("private upstream details")

    asyncio.run(process_one(client.app.state.factory, client.app.state.settings, Broken()))
    response = client.get("/v1/jobs/" + created["id"], headers=a).json()
    assert response["status"] == "failed"
    assert "private upstream" not in response["error"]
    with client.app.state.factory() as db:
        assert db.scalar(select(Job)).status == "failed"


# Подставляет задание другого исполнителя и проверяет отказ, понятную ошибку и отсутствие файла.
# Иначе один обработчик мог бы взять работу, для которой он не настроен.
def test_worker_rejects_jobs_for_a_different_provider_before_generation(client):
    headers = account(client)
    created = job(client, headers).json()
    factory = client.app.state.factory
    settings = client.app.state.settings
    with factory.begin() as db:
        db.get(Job, created["id"]).provider = "kandinsky"

    class UnexpectedWork:
        # Сразу проваливает проверку при вызове. Для чужого задания обработчик должен остановиться
        # до передачи запроса исполнителю.
        async def generate(self, *_):
            pytest.fail("The configured worker must never run this job")

    assert asyncio.run(process_one(factory, settings, UnexpectedWork()))
    failed = client.get(f"/v1/jobs/{created['id']}", headers=headers).json()
    assert failed["status"] == "failed"
    assert failed["output_id"] is None
    assert failed["error"] == "Генерация прервалась. Проверьте сервер и повторите запрос."
    assert list(settings.media_dir.iterdir()) == []


# Устанавливает короткий срок, удерживает генерацию и проверяет его продление. Пока первый
# исполнитель работает, второй не может забрать задание, а первый затем сохраняет результат.
def test_slow_work_renews_its_lease_before_publishing(client):
    from PIL import Image

    headers = account(client)
    created = job(client, headers).json()
    factory = client.app.state.factory
    settings = client.app.state.settings
    settings.lease_seconds = 1

    # Запускает медленное задание, сверяет владельца и новый срок, затем разрешает выход. При сбое
    # завершает свою задачу, чтобы она не работала после проверки.
    async def exercise():
        started = asyncio.Event()
        finish = asyncio.Event()

        class SlowGeneration:
            # Сообщает о старте, ждёт разрешения и создаёт пробное фото. Такая задержка даёт время
            # проверить продление без случайного окончания расчёта.
            async def generate(self, item, output, reference):
                started.set()
                await finish.wait()
                Image.new("RGB", (48, 32), "green").save(output, format="PNG")
                return "image/png"

        running = asyncio.create_task(process_one(factory, settings, SlowGeneration()))

        # Читает владельца и срок заново через отдельное соединение. По этой паре видно продление
        # того же права с сохранением его владельца.
        def lease():
            with factory() as db:
                row = db.get(Job, created["id"])
                return row.lease_owner, row.lease_until

        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            owner, original = lease()
            assert owner is not None
            cutoff = asyncio.get_running_loop().time() + 3
            while lease()[1] <= original and asyncio.get_running_loop().time() < cutoff:
                await asyncio.sleep(0.02)
            renewed_owner, renewed_until = lease()
            assert renewed_owner == owner
            assert renewed_until > original
            assert claim(factory, settings.lease_seconds) is None
            finish.set()
            assert await asyncio.wait_for(running, timeout=3)
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(exercise())
    completed = client.get(f"/v1/jobs/{created['id']}", headers=headers).json()
    assert completed["status"] == "succeeded"
    assert completed["progress"] == 100
    assert completed["output_id"] is not None


# Меняет владельца права прямо перед выдачей готовых байтов. Старый обработчик не должен оставить
# файл или ссылку на него.
def test_stale_worker_cannot_publish_after_lease_taken(client):
    from app.Models import Asset

    a = account(client)
    created = job(client, a).json()
    factory = client.app.state.factory

    class Stale:
        # Пишет пробные выходные байты и меняет право обработки через отдельную запись. Когда
        # функция возвращает тип файла, право публикации уже потеряно.
        async def generate(self, item, path, reference):
            path.write_bytes(b"old output")
            with factory() as db:
                row = db.get(Job, item.id)
                row.lease_owner = "new-owner"
                db.commit()
            return "image/png"

    asyncio.run(process_one(factory, client.app.state.settings, Stale()))
    with factory() as db:
        assert db.get(Job, created["id"]).output_id is None
        assert db.query(Asset).count() == 0
    assert not list(client.app.state.settings.media_dir.glob("*.png"))


# Передаёт ролик на повышение разрешения через пробную службу. Проверяет, что процент не
# уменьшается, а выданный файл сохраняет правильные байты, тип и расширение.
def test_video_sr_worker_stores_mp4(client, monkeypatch):
    import base64
    import json

    import httpx

    from app.Models import Asset
    from app.Services.Gpu.provider import GpuProvider

    headers = account(client)
    created = job(client, headers).json()
    factory = client.app.state.factory
    settings = client.app.state.settings
    settings.provider = "kandinsky"
    source = b"\x00\x00\x00\x18ftypmp42source"
    result = b"\x00\x00\x00\x18ftypmp42result"
    with factory() as db:
        item = db.get(Job, created["id"])
        ref = Asset(user_id=item.user_id, mime="video/mp4", size=len(source), filename="source.mp4")
        db.add(ref)
        db.flush()
        item.provider = "kandinsky"
        item.model = "video_sr"
        item.input_id = ref.id
        db.commit()
    (settings.media_dir / "source.mp4").write_bytes(source)

    progress_updates = iter((37, 12, 68, None))
    saved_progress = []

    # Проверяет параметры исходного вызова, выдаёт несколько значений хода и возвращает пробный
    # результат. Перед ответом читает прежний процент, чтобы поймать нежелательное снижение.
    def gpu(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["model"] == "video_sr"
            assert payload["image"] is None
            assert base64.b64decode(payload["video"]) == source
            return httpx.Response(202)
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=result)
        with factory() as db:
            saved_progress.append(db.get(Job, created["id"]).progress)
        progress = next(progress_updates)
        if progress is not None:
            return httpx.Response(200, json={"status": "running", "progress": progress})
        return httpx.Response(200, json={"status": "succeeded"})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.Services.Gpu.provider.httpx.AsyncClient",
        lambda **options: async_client(transport=httpx.MockTransport(gpu), **options),
    )
    assert asyncio.run(process_one(factory, settings, GpuProvider("https://gpu.test", "test")))
    assert saved_progress == [None, 37, 37, 68]
    with factory() as db:
        ready = db.get(Job, created["id"])
        asset = db.get(Asset, ready.output_id)
        assert ready.status == "succeeded" and ready.progress == 100
        assert asset.mime == "video/mp4"
        assert asset.filename.endswith(".mp4")
        assert (settings.media_dir / asset.filename).read_bytes() == result


# Имитирует недоступность до принятия внешнего запроса и после принятия. В обоих случаях задание
# возвращается в очередь без потери попытки и без окончательной ошибки.
@pytest.mark.parametrize("submitted", [False, True])
def test_gpu_gateway_loss_returns_job_to_queue(client, monkeypatch, submitted):
    import httpx

    from app.Services.Gpu.provider import GpuProvider

    owner = account(client)
    created = job(client, owner).json()
    factory = client.app.state.factory
    settings = client.app.state.settings
    settings.provider = "kandinsky"
    with factory.begin() as db:
        db.get(Job, created["id"]).provider = "kandinsky"
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.Services.Gpu.provider.httpx.AsyncClient",
        lambda **opts: async_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    202 if submitted and request.method == "POST" else 502
                )
            ),
            **opts,
        ),
    )
    asyncio.run(process_one(factory, settings, GpuProvider("https://gpu.test", "test")))
    with factory() as db:
        item = db.get(Job, created["id"])
        assert (item.status, item.attempts, item.lease_owner, item.error) == (
            "queued",
            0,
            None,
            None,
        )


# Перезапускает исполнителя, отменяет запрос владельцем или передаёт право другому исполнителю.
# Сверяет состояние каждой ветви и отсутствие частичного файла. Только явная отмена должна уходить
# внешней службе.
@pytest.mark.parametrize("outcome", ["restart", "cancel", "replace"])
def test_interrupted_worker_cannot_publish_or_cancel_a_replacement(client, outcome):
    import httpx

    headers = account(client)
    created = job(client, headers).json()
    path = "/v1/jobs/" + created["id"]
    factory = client.app.state.factory
    settings = client.app.state.settings
    settings.lease_seconds = 1

    # Запускает удерживаемую работу, вызывает выбранное прерывание и проверяет её остановку.
    # Отдельно сверяет команду отмены, чтобы новый исполнитель не получил чужое прерывание.
    async def exercise():
        started = asyncio.Event()
        stopped = asyncio.Event()
        remote_cancellations = []
        operation = None

        class InFlight:
            # Сохраняет номер внешней операции и пишет неполный файл. Остаётся ждать до отмены и
            # сообщает о выходе, чтобы проверить очистку именно начатой работы.
            async def generate(self, item, output, reference):
                nonlocal operation
                operation = item.run_id or item.id
                output.write_bytes(b"unfinished media")
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            # Запоминает отменённый запуск и имитирует недоступность службы. Нужный номер важен,
            # иначе отмена могла бы остановить заменивший его расчёт.
            async def cancel(self, job_id):
                remote_cancellations.append(job_id)
                raise httpx.ConnectError("GPU is temporarily unreachable")

        running = asyncio.create_task(process_one(factory, settings, InFlight()))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            if outcome == "restart":
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            elif outcome == "cancel":
                response = await asyncio.to_thread(client.post, path + "/cancel", headers=headers)
                assert response.json()["status"] == "cancelled"
                assert await asyncio.wait_for(running, timeout=3)
            else:
                with factory.begin() as db:
                    db.get(Job, created["id"]).lease_owner = "replacement"
                assert await asyncio.wait_for(running, timeout=3)
            assert stopped.is_set()
            assert remote_cancellations == ([operation] if outcome == "cancel" else [])
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(exercise())
    final = client.get(path, headers=headers).json()
    assert final["output_id"] is None
    assert final["error"] is None
    assert (
        final["status"]
        == {"restart": "queued", "cancel": "cancelled", "replace": "running"}[outcome]
    )
    with factory() as db:
        assert db.get(Job, created["id"]).lease_owner == (
            "replacement" if outcome == "replace" else None
        )
    assert not list(settings.media_dir.glob("*.png"))


# Даёт исполнителю вернуть пустой файл с правильным типом. Обработчик сообщает об ошибке и убирает
# файл, иначе фотогалерея увидела бы пустой результат как готовый.
def test_worker_rejects_an_empty_output(client):
    headers = account(client)
    created = job(client, headers).json()
    settings = client.app.state.settings

    class EmptyResult:
        # Создаёт выходной файл без байтов и возвращает тип изображения. Так проверка отличает
        # наличие файла от пригодного результата.
        async def generate(self, item, output, reference):
            output.touch()
            return "image/png"

    assert asyncio.run(process_one(client.app.state.factory, settings, EmptyResult()))
    failed = client.get("/v1/jobs/" + created["id"], headers=headers).json()
    assert failed["status"] == "failed"
    assert failed["output_id"] is None
    assert not list(settings.media_dir.glob("*.png"))


# Загружает исходник и скрывает его при чтении задания обработчиком. Проверяет отказ до генерации
# и сохранность настоящего исходника, поскольку исчезновение было только в проверочном чтении.
def test_worker_fails_without_running_the_provider_when_source_is_unavailable(client, monkeypatch):
    headers = account(client)
    photo = (
        MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
    ).read_bytes()
    uploaded = client.post(
        "/v1/assets", headers=headers, files={"file": ("photo.jpg", photo, "image/jpeg")}
    )
    assert uploaded.status_code == 201
    source = uploaded.json()["id"]
    created = job(client, headers, model="video", input_id=source, duration_seconds=1)
    assert created.status_code == 201
    factory = client.app.state.factory
    session = factory.class_
    read = session.get

    # Скрывает только нужный исходник, оставляя остальные чтения обычными. Без этой подмены нельзя
    # было бы воспроизвести исчезновение файла в нужном месте.
    def read_after_source_vanished(db, model, key, *args, **options):
        if model is Asset and key == source:
            return None
        return read(db, model, key, *args, **options)

    class UnexpectedGeneration:
        # Проваливает проверку при попытке передачи запроса без исходника. Пригодность исходных
        # данных проверяется раньше.
        async def generate(self, *_):
            pytest.fail("The provider received a request whose source is unavailable")

    # Запускает обработчика с немедленным началом фоновых задач и затем возвращает прежний способ
    # запуска. Это даёт воспроизводимый порядок чтения без влияния на следующие проверки.
    async def work():
        loop = asyncio.get_running_loop()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            return await process_one(factory, client.app.state.settings, UnexpectedGeneration())
        finally:
            loop.set_task_factory(None)

    with monkeypatch.context() as patch:
        patch.setattr(session, "get", read_after_source_vanished)
        assert asyncio.run(work())

    result = client.get(f"/v1/jobs/{created.json()['id']}", headers=headers).json()
    assert result["status"] == "failed"
    assert result["output_id"] is None
    assert client.get(f"/v1/assets/{source}", headers=headers).status_code == 200
