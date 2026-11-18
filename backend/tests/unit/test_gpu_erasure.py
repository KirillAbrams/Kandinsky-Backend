# Проверяет стирание заданий, файлов и процессов на стороне исполнителя. Сценарии работают с
# отдельными папками и подставляют вычисления, поэтому не затрагивают рабочие задания.

"""Запрос удаления стирает данные исполнителя и запрещает поздний повтор."""

import base64
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from unittest.mock import Mock, call
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.Services.Gpu.Runner import erasure
from tests.support.fixtures import MOBILE_FIXTURES


# Пускает настоящий обработчик команд с временной папкой и проверочным ключом. Заменяет
# вычислительный цикл ожиданием, чтобы можно было проверять базу и ответы отдельно от модели.
@pytest.fixture
def gpu_client(tmp_path, monkeypatch):
    import app.Services.Gpu.Runner.bridge as gpu

    monkeypatch.setattr(gpu, "ROOT", tmp_path / "gpu")
    monkeypatch.setattr(gpu, "TOKEN", "isolated-gpu-erasure-test-key-" * 2)
    # Для проверки команд и хранения достаточно пустого цикла без вычислений.
    monkeypatch.setattr(gpu, "runner", lambda: gpu.STOP.wait())
    with TestClient(gpu.app, headers={"Authorization": "Bearer " + gpu.TOKEN}) as client:
        yield gpu, client


# Берёт настоящее фото и собирает запрос с номером исходной работы и заметным текстом. После
# удаления по этому тексту можно проверить, что его нет даже в файле базы.
def payload(canonical, marker):
    photo = (
        MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
    ).read_bytes()
    return {
        "canonical_job_id": canonical,
        "prompt": marker,
        "model": "video",
        "aspect": "1:1",
        "seed": 1,
        "image": base64.b64encode(photo).decode(),
        "duration_seconds": 1,
    }


# Создаёт две попытки одной работы и отдельную работу с собственным файлом. После удаления сверяет
# обе очищенные попытки, нетронутого соседа и отсутствие текста в байтах базы.
def test_erasure_removes_every_attempt_and_preserves_another_job(gpu_client):
    gpu, client = gpu_client
    canonical, other = str(uuid4()), str(uuid4())
    attempts = [str(uuid4()), str(uuid4())]
    marker = "private-gpu-payload-" + uuid4().hex
    for attempt in attempts:
        assert client.post("/jobs/" + attempt, json=payload(canonical, marker)).status_code == 200
        folder = gpu.ROOT / attempt
        folder.mkdir()
        (folder / "result.mp4").write_bytes(b"private output")
        (folder / "inference.log").write_text(marker)
    assert client.post("/jobs/" + other, json=payload(other, "keep-other-job")).status_code == 200
    keep = gpu.ROOT / other
    keep.mkdir()
    (keep / "result.mp4").write_bytes(b"keep")
    response = client.delete("/jobs/" + canonical)
    assert response.status_code == 204, response.text
    with gpu.db() as db:
        assert (
            db.execute("SELECT count(*) FROM jobs WHERE id IN (?,?)", attempts).fetchone()[0] == 0
        )
        assert db.execute("SELECT count(*) FROM jobs WHERE id=?", (other,)).fetchone()[0] == 1
    assert all(not (gpu.ROOT / attempt).exists() for attempt in attempts)
    assert (keep / "result.mp4").read_bytes() == b"keep"
    assert marker.encode() not in (gpu.ROOT / "jobs.db").read_bytes()


# Стирает ещё не доставленную работу дважды и пробует послать её позже и после перезапуска.
# Отметка должна пережить запуск и заблокировать позднюю попытку.
def test_erasure_is_idempotent_and_rejects_late_submit_after_restart(gpu_client):
    gpu, client = gpu_client
    canonical = str(uuid4())
    body = payload(canonical, "deleted-before-submit")
    assert client.delete("/jobs/" + canonical).status_code == 204
    assert client.delete("/jobs/" + canonical).status_code == 204
    assert client.get("/jobs/" + canonical).status_code == 410
    assert client.post("/jobs/" + canonical, json=body).status_code == 410
    with TestClient(gpu.app, headers={"Authorization": "Bearer " + gpu.TOKEN}) as restarted:
        assert restarted.post("/jobs/" + str(uuid4()), json=body).status_code == 410


# Просит удалить одну работу с номером попытки другой. Проверяет отказ, сохранность чужого
# результата и возможность поставить своё задание после неудавшегося удаления.
def test_erasure_rejects_an_attempt_owned_by_another_generation(gpu_client):
    gpu, client = gpu_client
    wanted, other, attempt = (str(uuid4()) for _ in range(3))
    assert client.post("/jobs/" + attempt, json=payload(other, "other-job")).status_code == 200
    result = gpu.ROOT / attempt / "result.mp4"
    result.parent.mkdir()
    result.write_bytes(b"keep this result")

    response = client.delete("/jobs/" + wanted, params={"run_id": attempt})

    assert response.status_code == 409
    assert response.json()["detail"] == "Attempt belongs to another generation"
    assert client.get("/jobs/" + attempt).json() == {"status": "queued"}
    assert result.read_bytes() == b"keep this result"
    assert client.post("/jobs/" + wanted, json=payload(wanted, "new-job")).status_code == 200


# Создаёт работу и отправляет удаление с неправильным ключом. Проверяет отказ и наличие строки,
# чтобы команда не могла изменить очередь без допуска.
def test_erasure_requires_internal_secret(gpu_client):
    gpu, client = gpu_client
    job = str(uuid4())
    assert (
        client.post("/jobs/" + job, json=payload(job, "keep-on-denied-delete")).status_code == 200
    )
    assert (
        client.delete("/jobs/" + job, headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    with gpu.db() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE id=?", (job,)).fetchone()[0] == 1


# Сравнивает уже завершившийся процесс и процесс, завершившийся перед посылкой сигнала. В первом
# случае ждать нечего, а во втором всё равно нужно дождаться уборки дочернего процесса.
@pytest.mark.parametrize("already_gone", [True, False])
def test_process_stop_skips_exited_children_and_reaps_signal_race(monkeypatch, already_gone):
    process = Mock(pid=42)
    process.poll.return_value = 0 if already_gone else None
    signal_group = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(erasure.os, "killpg", signal_group)

    erasure.JobErasure.stop(process)

    if already_gone:
        signal_group.assert_not_called()
        process.wait.assert_not_called()
    else:
        signal_group.assert_called_once_with(42, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=3)


# Делает первое ожидание слишком долгим и сверяет порядок остановки. Принудительное завершение
# допускается только после первой попытки и заканчивается ещё одним ожиданием.
@pytest.mark.parametrize("exited_before_kill", [True, False])
def test_process_stop_escalates_only_after_the_first_wait_times_out(
    monkeypatch, exited_before_kill
):
    signal_group = Mock(side_effect=[None, ProcessLookupError() if exited_before_kill else None])
    monkeypatch.setattr(erasure.os, "killpg", signal_group)
    process = Mock(pid=42)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 3), 0]
    events = Mock()
    events.attach_mock(signal_group, "signal")
    events.attach_mock(process.wait, "wait")

    erasure.JobErasure.stop(process)

    assert events.mock_calls == [
        call.signal(42, signal.SIGTERM),
        call.wait(timeout=3),
        call.signal(42, signal.SIGKILL),
        call.wait(timeout=3),
    ]


# Подставляет вместо папки задания ссылку на соседнюю папку. Проверяет, что ушла только запись
# задания, а содержимое по другому пути сохранилось.
def test_erasure_unlinks_the_job_entry_without_touching_its_symlink_target(gpu_client):
    gpu, client = gpu_client
    job = str(uuid4())
    neighbor = gpu.ROOT / "neighbor"
    neighbor.mkdir()
    file = neighbor / "keep.txt"
    file.write_text("Keep this file")
    entry = gpu.ROOT / job
    entry.symlink_to(neighbor, target_is_directory=True)

    assert client.delete("/jobs/" + job).status_code == 204
    assert not entry.is_symlink()
    assert file.read_text() == "Keep this file"


# Удерживает чтение очереди во время удаления, сверяет отложенный отказ и сохранённую отметку,
# затем освобождает читателя. Повтор должен завершиться без возврата задания.
def test_erasure_reports_an_active_reader_and_can_finish_after_it_disconnects(tmp_path):
    path = tmp_path / "queue.sqlite"
    job = str(uuid4())

    # Открывает короткое соединение с временной базой без ожидания блокировок. Это делает видимой
    # помеху очистке от отдельного читателя.
    @contextmanager
    def connect():
        with closing(sqlite3.connect(path, timeout=0)) as connection, connection:
            connection.row_factory = sqlite3.Row
            yield connection

    queue = erasure.JobErasure(lambda: tmp_path, connect)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO jobs VALUES (?,'queued')", (job,))
        queue.prepare(db)
    with connect() as reader:
        reader.execute("BEGIN")
        assert (
            reader.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] == "queued"
        )
        with pytest.raises(OSError, match="Erasure waits for active readers"):
            queue.erase(job)
        with connect() as db:
            assert queue.contains(db, job)
            assert db.execute("SELECT id FROM jobs WHERE id=?", (job,)).fetchone() is None
    queue.erase(job)
    with connect() as db:
        assert queue.contains(db, job)


# Запускает местный дочерний процесс и проверяет удаление и отмену отдельно. После остановки он не
# должен оставить исходный запрос или записать запоздалый результат.
@pytest.mark.parametrize("action", ["erase", "cancel"])
def test_stopping_running_work_retires_the_child_and_its_input(tmp_path, monkeypatch, action):
    import sys

    import app.Services.Gpu.Runner.bridge as gpu

    monkeypatch.setattr(gpu, "ROOT", tmp_path / "running-gpu")
    monkeypatch.setattr(gpu, "TOKEN", "isolated-running-erasure-key-" * 2)
    original = subprocess.Popen
    started = threading.Event()
    children = []

    # Заменяет модель процессом, который пишет результат только после паузы. Тогда проверка
    # действительно может остановить начатую работу до записи файла.
    def delayed_model(args, **kwargs):
        output = args[args.index("--output") + 1]
        program = 'import sys,time;from pathlib import Path;time.sleep(20);Path(sys.argv[1]).write_bytes(b"late private result")'
        process = original([sys.executable, "-c", program, output], **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(gpu.subprocess, "Popen", delayed_model)
    job = str(uuid4())
    try:
        with TestClient(gpu.app, headers={"Authorization": "Bearer " + gpu.TOKEN}) as client:
            assert (
                client.post("/jobs/" + job, json=payload(job, "running-private-data")).status_code
                == 200
            )
            assert started.wait(5), "The isolated child process did not start"
            path = "/jobs/" + job
            folder = gpu.ROOT / job
            if action == "erase":
                response = client.delete(path)
                assert response.status_code == 204, response.text
                assert children[0].poll() is not None, (
                    "Deletion acknowledged while its child was still alive"
                )
                assert not folder.exists()
                assert (
                    client.post(path, json=payload(job, "running-private-data")).status_code == 410
                )
            else:
                assert client.post(path + "/cancel").json() == {"status": "cancelled"}
                assert children[0].wait(timeout=5) != 0
                request = folder / "request.json"
                deadline = time.monotonic() + 5
                while request.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert not request.exists()
                assert not (folder / "result.mp4").exists()
                assert client.get(path).json() == {"status": "cancelled"}
                assert client.get(path + "/result").status_code == 409
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
