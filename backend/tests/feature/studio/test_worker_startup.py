# Запускает обычную точку входа обработчика на отдельной базе. Проверяет продолжение очереди после
# запуска и закрытие после сигнала.

import asyncio
import runpy
import signal
import threading
import time
from uuid import uuid4

import httpx
from PIL import Image

from app.Jobs import worker
from app.Models import ErasedSession, Job, PendingGpuRemoval
from app.Services.Gpu import provider as providers
from tests.support.fixtures import account, job


# Готовит задание, команду удаления и старую отметку сеанса, затем запускает обработчик. Проверяет
# выдачу результата, уборку обеих записей и остановку фоновой работы без обращения к настоящему
# исполнителю.
def test_worker_entry_point_resumes_local_queue_and_closes_after_stop(client, monkeypatch):
    headers = account(client)
    created = job(client, headers)
    assert created.status_code == 201
    job_id = created.json()["id"]
    settings = client.app.state.settings
    factory = client.app.state.factory
    old_job, old_attempt = str(uuid4()), str(uuid4())
    cleanup_requests = []
    with factory.begin() as db:
        db.add(PendingGpuRemoval(job_id=old_job, run_id=old_attempt))
        db.add(ErasedSession(digest="0" * 64, expires_at=0))

    monkeypatch.setenv("STUDIO_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STUDIO_MEDIA_DIR", str(settings.media_dir))
    monkeypatch.setenv("STUDIO_PROVIDER", "demo")
    monkeypatch.setenv("STUDIO_MODERATION_MODE", "off")
    monkeypatch.setenv("STUDIO_GPU_URL", "https://gpu.test")
    monkeypatch.setenv("STUDIO_GPU_TOKEN", "isolated-provider-test")
    start_client = httpx.AsyncClient

    # Проверяет метод, путь и номер удаляемого запуска, запоминает команду и подтверждает её.
    # Вместо сетевого исполнителя отвечает пробный транспорт.
    def gpu(request):
        assert request.method == "DELETE"
        assert request.url.path == f"/jobs/{old_job}"
        assert request.url.params["run_id"] == old_attempt
        cleanup_requests.append(request.url.path)
        return httpx.Response(204)

    # Подключает пробный ответ к обычному сетевому клиенту обработчика. Так запрос проходит
    # штатную подготовку, но не выходит за пределы проверки.
    def transport_client(**options):
        return start_client(**options, transport=httpx.MockTransport(gpu))

    monkeypatch.setattr(providers.httpx, "AsyncClient", transport_client)
    install_handler = asyncio.SelectorEventLoop.add_signal_handler
    started = threading.Event()
    stop = []

    # Устанавливает обычное ожидание сигналов и запоминает обработчик остановки. Отдельный поток
    # сможет вызвать его, не посылая сигнал всему процессу проверки.
    def observe_handler(loop, sig, callback, *args):
        install_handler(loop, sig, callback, *args)
        if sig == signal.SIGTERM:
            stop.append((loop, callback))
            started.set()

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", observe_handler)

    # Ждёт установки обработчика сигнала и проверяет, что результат сохранён, а удаление
    # доставлено. Затем вызывает остановку через очередь цикла, иначе проверка могла бы зависнуть.
    def stop_after_publishing():
        if not started.wait(timeout=5):
            return
        loop, callback = stop[0]
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                with factory() as db:
                    if (
                        db.get(Job, job_id).status == "succeeded"
                        and db.get(PendingGpuRemoval, old_job) is None
                    ):
                        # Даём рабочему циклу завершить ещё одну итерацию ожидания в пустой очереди.
                        time.sleep(1.2)
                        return
                time.sleep(0.02)
        finally:
            loop.call_soon_threadsafe(callback)

    monitor = threading.Thread(target=stop_after_publishing)
    monitor.start()
    try:
        runpy.run_path(worker.__file__, run_name="__main__")
    finally:
        monitor.join(timeout=6)
    assert not monitor.is_alive()
    assert len(stop) == 1
    assert cleanup_requests == [f"/jobs/{old_job}"]
    with factory() as db:
        assert db.get(ErasedSession, "0" * 64) is None
        assert db.get(PendingGpuRemoval, old_job) is None
        assert db.get(Job, job_id).status == "succeeded"
    result = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert client.get(f"/v1/assets/{result['output_id']}", headers=headers).status_code == 200


# Запрашивает отмену у пробного генератора и сверяет уже существующее изображение и состав папки.
# Отмена другого задания не должна трогать готовый файл.
def test_demo_provider_cancels_without_network_or_file_changes(tmp_path):
    output = tmp_path / "previous-result.png"
    Image.new("RGB", (24, 24), "green").save(output)
    original = output.read_bytes()
    asyncio.run(providers.DemoProvider().cancel("another-job"))
    assert output.read_bytes() == original
    assert list(tmp_path.iterdir()) == [output]
