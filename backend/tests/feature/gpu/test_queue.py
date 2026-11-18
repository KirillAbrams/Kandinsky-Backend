# Проверяет очередь вычислительной службы, права вызова и выдачу готовых файлов. Пробная очередь
# работает без запуска настоящих моделей.

"""Проверяет приём запроса и запись в журнал без запуска модели"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.Services.Gpu.Runner import bridge
from app.Services.Gpu.Runner.infer import write_progress
from tests.support.fixtures import MOBILE_FIXTURES


# Назначает отдельные папки, ключ доступа и путь к исполнителю для каждой проверки. Так запуск не
# смешивает пробные задания с рабочими файлами.
@pytest.fixture
def queue_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "TOKEN", "x" * 48)
    monkeypatch.setenv("KANDINSKY_MODELS", str(tmp_path / "models"))
    monkeypatch.setattr(bridge, "VIDEO_SR_PYTHON", str(tmp_path / "sr-python"))
    return tmp_path


# Подменяет обработку ожиданием остановки и открывает клиент очереди. Проверки сетевых ответов
# проходят без вычисления результатов.
@pytest.fixture
def api(queue_directory, monkeypatch):
    # Проверки очереди завершаются без загрузки моделей.
    monkeypatch.setattr(bridge, "runner", lambda: bridge.STOP.wait())
    with TestClient(bridge.app) as client:
        yield client


# Проверяет отказ без ключа, единственную запись на повтор, отказ при смене параметров и проверку
# вложения. Сверяет ход и отмену задания, а также отсутствие установленных моделей.
def test_private_idempotent_queue_and_cancel(api):
    operation = str(uuid4())
    body = {"prompt": "Тихое озеро", "model": "image", "aspect": "1:1", "seed": 7}
    path = "/jobs/" + operation
    assert api.post(path, json=body).status_code == 401
    headers = {"Authorization": "Bearer " + "x" * 48}
    for _ in range(2):
        assert api.post(path, json=body, headers=headers).json() == {"status": "queued"}
    with bridge.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert api.post(path, json={**body, "seed": 8}, headers=headers).status_code == 409
    assert api.post(path, json={**body, "image": "wrong"}, headers=headers).status_code == 422
    encoded_text = base64.b64encode(b"not an image").decode()
    assert api.post(path, json={**body, "image": encoded_text}, headers=headers).status_code == 422
    assert api.post(path, json={**body, "image": None}, headers=headers).json() == {
        "status": "queued"
    }
    assert api.get(path + "/result", headers=headers).status_code == 409
    with bridge.db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (operation,))
    assert api.get(path, headers=headers).json() == {"status": "running"}
    folder = bridge.ROOT / operation
    folder.mkdir(exist_ok=True)
    write_progress(folder / "result.png", 2, 8)
    assert api.get(path, headers=headers).json() == {"status": "running", "progress": 25}
    write_progress(folder / "result.png", 8, 8)
    assert api.get(path, headers=headers).json() == {"status": "running", "progress": 99}
    (folder / "progress").write_text("100")
    assert api.get(path, headers=headers).json() == {"status": "running"}
    assert api.post(path + "/cancel", headers=headers).json() == {"status": "cancelled"}
    assert api.get(path, headers=headers).json() == {"status": "cancelled"}
    health = api.get("/health", headers=headers).json()
    assert set(health["models"]) == {"image", "video", "image_video"}
    assert not any(model["present"] for model in health["models"].values())


# Добавляет пробный ключ к клиенту. Тесты действий очереди получают те же права, которые нужны
# обычной вычислительной службе.
@pytest.fixture
def authorized(api):
    api.headers["Authorization"] = "Bearer " + bridge.TOKEN
    return api


# Заполняет очередь и проверяет отказ новому заданию, пока повтор старого номера ещё возвращает
# прежний ответ. После отмены одного задания новое занимает освободившееся место.
def test_full_queue_still_accepts_the_original_operation_number(authorized):
    request = {"prompt": "Озеро", "model": "image", "aspect": "1:1", "seed": 4}
    paths = [f"/jobs/{uuid4()}" for _ in range(21)]
    for path in paths[:20]:
        reply = authorized.post(path, json=request)
        assert reply.status_code == 200 and reply.json() == {"status": "queued"}
    assert authorized.get(paths[-1]).status_code == 404
    assert authorized.post(paths[-1], json=request).status_code == 429
    replay = authorized.post(paths[0], json=request)
    assert replay.status_code == 200 and replay.json() == {"status": "queued"}
    assert authorized.post(paths[0] + "/cancel").json() == {"status": "cancelled"}
    assert authorized.post(paths[-1], json=request).status_code == 200


# Перебирает несовместимые исходники и настройки для удвоения разрешения видео. Правильный запрос
# сначала получает отказ без исполнителя, а после появления пробного файла исполнителя встаёт в
# очередь.
def test_video_sr_checks_input_and_fixed_parameters_before_accepting(authorized, tmp_path):
    source = (MOBILE_FIXTURES / "media/video-01.mp4").read_bytes()
    video = base64.b64encode(source).decode()
    image_path = tmp_path / "reference.png"
    Image.new("RGB", (16, 16)).save(image_path)
    image = base64.b64encode(image_path.read_bytes()).decode()
    request = {
        "prompt": "Улучшение видео",
        "model": "video_sr",
        "aspect": "1:1",
        "seed": 8,
        "video": video,
    }
    path = f"/jobs/{uuid4()}"
    for changed in (
        {"video": None},
        {"image": image},
        {"duration_seconds": 3},
        {"quality": "draft"},
        {"resolution": "compact"},
        {"negative_prompt": "Туман"},
        {"guidance_scale": 4.0},
    ):
        reply = authorized.post(path, json={**request, **changed})
        assert reply.status_code == 422
        assert reply.json()["detail"] == "Video SR requires MP4 and fixed ×2 scale"

    for changed in (
        {"video": base64.b64encode(b"not an mp4").decode()},
        {"model": "video"},
        {"model": "image", "video": None, "image": image},
        {"model": "video", "video": None, "guidance_scale": 5.0},
    ):
        assert authorized.post(path, json={**request, **changed}).status_code == 422
    assert authorized.get(path).status_code == 404
    assert authorized.post(path, json=request).status_code == 503

    (tmp_path / "sr-python").touch()
    health = authorized.get("/health").json()
    assert health["models"]["video_sr"]["present"] is False
    accepted = authorized.post(path, json=request)
    assert accepted.status_code == 200 and accepted.json() == {"status": "queued"}


# Записывает фото или ролик в папку задания, проверяет отказ до готовности и затем сверяет тип и
# каждый байт выданного файла. Иначе наличие файла могло бы преждевременно открыть результат.
@pytest.mark.parametrize(
    ("model", "suffix", "mime"),
    [
        ("image", "png", "image/png"),
        ("video", "mp4", "video/mp4"),
    ],
)
def test_result_serves_the_saved_file_with_the_matching_type(
    authorized, tmp_path, model, suffix, mime
):
    job = str(uuid4())
    path = f"/jobs/{job}"
    payload = {"prompt": "Озеро", "model": model, "aspect": "1:1", "seed": 4}
    assert authorized.post(path, json=payload).status_code == 200
    folder = tmp_path / job
    folder.mkdir()
    output = folder / f"result.{suffix}"
    if model == "image":
        Image.new("RGB", (16, 16)).save(output)
    else:
        output.write_bytes((MOBILE_FIXTURES / "media/video-01.mp4").read_bytes())
    assert authorized.get(path + "/result").status_code == 409
    with bridge.db() as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job,))
    reply = authorized.get(path + "/result")
    assert reply.status_code == 200
    assert reply.headers["content-type"] == mime and reply.content == output.read_bytes()


# Подставляет слишком короткий ключ и проверяет отказ уже при запуске. Папка очереди не должна
# появиться, если защита ещё не настроена.
def test_startup_stops_before_opening_the_queue_with_a_short_key(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr(bridge, "ROOT", root)
    monkeypatch.setattr(bridge, "TOKEN", "short")
    monkeypatch.setattr(bridge, "runner", lambda: bridge.STOP.wait())
    with pytest.raises(RuntimeError, match="at least 32 characters"), TestClient(bridge.app):
        pytest.fail("The queue must reject initialization")
    assert not root.exists()


# Запускает обработчика с успешным, пустым и аварийным выходом дочернего процесса или с отказом
# запуска. Сверяет итог и удаление временного запроса, чтобы лишняя копия текста не оставалась на
# диске.
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("success", "succeeded"),
        ("empty", "failed"),
        ("exit_error", "failed"),
        ("launch_error", "failed"),
    ],
)
def test_worker_checks_its_child_and_removes_the_temporary_request(
    queue_directory, monkeypatch, mode, expected
):
    product = queue_directory / "fixture.png"
    Image.new("RGB", (16, 16), color="white").save(product)
    job = str(uuid4())
    folder = queue_directory / job
    path = "/jobs/" + job
    payload = {"prompt": "Озеро", "model": "image", "aspect": "1:1", "seed": 4}
    start = subprocess.Popen
    launches = []

    # Проверяет команду, папку результата и поля временного запроса. Запускает свой короткий
    # дочерний процесс с заданным исходом, чтобы проверить обработку отказа без модели.
    def child(args, **options):
        request = Path(args[args.index("--request") + 1])
        output = Path(args[args.index("--output") + 1])
        assert args[:3] == [sys.executable, "-m", "app.Services.Gpu.Runner.infer"]
        assert request.parent == folder and output == folder / "result.png"
        parsed = json.loads(request.read_text())
        assert all(parsed[key] == value for key, value in payload.items())
        assert options["start_new_session"] is True
        launches.append(output)
        if mode == "launch_error":
            raise OSError("Ошибка запуска в изолированной проверке")
        program = {
            "success": (
                "import sys\nfrom pathlib import Path\n"
                "Path(sys.argv[1]).write_bytes(Path(sys.argv[2]).read_bytes())\n"
            ),
            "empty": "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).touch()\n",
            "exit_error": "raise SystemExit(7)",
        }[mode]
        return start([sys.executable, "-c", program, str(output), str(product)], **options)

    monkeypatch.setattr(bridge.subprocess, "Popen", child)
    headers = {"Authorization": "Bearer " + bridge.TOKEN}
    with TestClient(bridge.app, headers=headers) as client:
        assert client.post(path, json=payload).json() == {"status": "queued"}
        deadline = time.monotonic() + 8
        status = "queued"
        while time.monotonic() < deadline:
            status = client.get(path).json()["status"]
            if status in {"failed", "succeeded"}:
                break
            time.sleep(0.04)
        assert status == expected
        assert launches == [folder / "result.png"]
        assert not (folder / "request.json").exists()
        reply = client.get(path + "/result")
        if mode == "success":
            assert reply.status_code == 200 and reply.content == product.read_bytes()
        else:
            assert reply.status_code == 409
