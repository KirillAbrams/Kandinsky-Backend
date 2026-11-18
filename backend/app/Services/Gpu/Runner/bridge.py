# Принимает задания от основного сервера и держит короткую очередь для видеокарты. Здесь можно
# проверить работу, отменить её или забрать результат. Номер попытки позволяет пережить потерю
# сетевого ответа без повторного запуска.

"""Закрытая служба обработки запускает одну модель за раз

Ключ доступа проверяется при каждом обращении
До запуска модель получает задание с постоянным номером из журнала
Повтор с тем же номером не запускает второй процесс
После перезапуска незаконченные задания отмечаются ошибкой
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .erasure import JobErasure
from .models import VIDEO_SR_MODEL, VIDEO_SR_PYTHON

ROOT = Path(os.environ.get("GPU_STATE_DIR", ".gpu-state")).resolve()
# Поднимаемся до папки, из которой виден пакет исполнителя, в образе и в исходниках.
PROJECT = Path(__file__).resolve().parents[len(__package__.split("."))]
TOKEN = os.environ.get("GPU_TOKEN", "")
STOP = threading.Event()


# Настраивает обращение к полям результата по именам, чтобы обработчик мог читать сохранённое состояние
# задания.
# Открывает местную базу заданий с ограниченным ожиданием записи. Строки доступны по именам полей,
# а удалённые значения вычищаются при записи, чтобы их содержимое не оставалось на прежнем месте.
def db():
    connection = sqlite3.connect(ROOT / "jobs.db", timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA secure_delete=ON")
    return connection


ERASURE = JobErasure(lambda: ROOT, db)


# Сверяет ключ запроса с закрытым ключом службы и отклоняет обращение при пустой настройке. Без
# этой проверки кто угодно с адресом службы мог бы поставить задание или получить файл.
def auth(authorization: str = Header("")):
    if not TOKEN or not hmac.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401, "Unauthorized")


class Input(BaseModel):
    canonical_job_id: UUID | None = None
    prompt: str = Field(min_length=1, max_length=2000)
    model: Literal["image", "video", "video_sr"]
    aspect: Literal["1:1", "5:3", "3:5"]
    seed: int = Field(ge=0, le=2147483647)
    image: str | None = Field(default=None, max_length=28_000_000)
    video: str | None = Field(default=None, max_length=28_000_000)
    quality: Literal["draft", "standard"] = "standard"
    resolution: Literal["compact", "standard"] = "standard"
    duration_seconds: Literal[1, 3, 5] = 5
    negative_prompt: str = Field(default="", max_length=2000)
    guidance_scale: float | None = Field(default=None, ge=1, le=8)

    # Если передано фото, проверяет строковое представление, размер и начальные признаки
    # допустимого изображения. Неподходящие байты отсеиваются до помещения в очередь, чтобы не
    # тратить время карты.
    @field_validator("image")
    @classmethod
    def valid_image(cls, value):
        if value is not None:
            try:
                raw = base64.b64decode(value, validate=True)
                if len(raw) > 20 * 1024 * 1024 or not raw.startswith((b"\xff\xd8\xff", b"\x89PNG")):
                    raise ValueError("Invalid image")
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError("Invalid reference image") from exc
        return value

    # Если передано видео, проверяет запись байтов, размер и признак контейнера. Более точная
    # проверка размеров и длительности происходит при чтении исходника.
    @field_validator("video")
    @classmethod
    def valid_video(cls, value):
        if value is not None:
            raw = base64.b64decode(value, validate=True)
            if len(raw) > 20 * 1024 * 1024 or raw[4:8] != b"ftyp":
                raise ValueError("Invalid MP4 reference")
        return value


# Берёт самое раннее ожидающее задание, отмечает его запущенным и передаёт закрытый файл
# отдельному процессу. Следит за отменой и сроком, затем записывает готовность только при наличии
# результата. Отметка удаления не даёт позднему завершению вернуть стёртый файл.
def runner():
    while not STOP.is_set():
        process = None
        with ERASURE.lock:
            with db() as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row:
                    connection.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            if row:
                body = json.loads(row["body"])
                folder = ROOT / row["id"]
                folder.mkdir(mode=0o700, exist_ok=True)
                output = folder / ("result.png" if body["model"] == "image" else "result.mp4")
                (folder / "progress").unlink(missing_ok=True)
                request = folder / "request.json"
                request.write_text(row["body"])
                args = [
                    VIDEO_SR_PYTHON if body["model"] == "video_sr" else sys.executable,
                    "-m",
                    f"{__package__}.infer",
                    "--request",
                    str(request),
                    "--output",
                    str(output),
                ]
                try:
                    with (folder / "inference.log").open("w") as log:
                        process = subprocess.Popen(
                            args,
                            cwd=PROJECT,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    ERASURE.processes[row["id"]] = process
                except (OSError, subprocess.SubprocessError):
                    request.unlink(missing_ok=True)
                    with db() as connection:
                        connection.execute(
                            "UPDATE jobs SET status='failed' WHERE id=?", (row["id"],)
                        )
        if row is None:
            STOP.wait(1)
            continue
        if process is None:
            continue
        deadline = time.monotonic() + 1800
        while process.poll() is None:
            with db() as connection:
                current = connection.execute(
                    "SELECT status FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
            if (
                STOP.is_set()
                or current is None
                or current["status"] == "cancelled"
                or time.monotonic() > deadline
            ):
                ERASURE.stop(process)
                break
            STOP.wait(1)
        with ERASURE.lock:
            ERASURE.processes.pop(row["id"], None)
            request.unlink(missing_ok=True)
            with db() as connection:
                if ERASURE.contains(connection, row["id"]):
                    ERASURE.remove_folder(row["id"])
                    continue
                status = (
                    "succeeded"
                    if process.returncode == 0 and output.exists() and output.stat().st_size
                    else "failed"
                )
                connection.execute(
                    "UPDATE jobs SET status=? WHERE id=? AND status='running'",
                    (status, row["id"]),
                )


# Проверяет настройку доступа, готовит базу, отмечает незавершённую работу после прежнего запуска
# как сбой и поднимает исполнитель. При остановке ждёт его выхода, чтобы он не остался работать со
# старой очередью.
@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 32:
        raise RuntimeError("Set random GPU_TOKEN, at least 32 characters")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with db() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, "
            "fingerprint TEXT NOT NULL, "
            "body TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "created REAL NOT NULL)"
        )
        connection.execute("UPDATE jobs SET status='failed' WHERE status='running'")
        ERASURE.prepare(connection)
    STOP.clear()
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    yield
    STOP.set()
    thread.join(25)


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth)])


# Сообщает, какие версии моделей заявлены и какие файлы для них есть на машине. Это позволяет
# отличить доступность службы от готовности каждой модели к работе.
@app.get("/health")
def health():
    from .models import DIFFUSERS_REVISION, MODELS

    root = Path(os.environ.get("KANDINSKY_MODELS", "/models"))
    models = {**MODELS}
    if Path(VIDEO_SR_PYTHON).is_file():
        models["video_sr"] = VIDEO_SR_MODEL
    return {
        "ok": True,
        "model": "Kandinsky 5 Lite",
        "engine": "diffusers",
        "diffusers_revision": DIFFUSERS_REVISION,
        "models": {
            mode: {
                "repo": repo,
                "revision": revision,
                "present": (root / mode / "model_index.json").exists(),
            }
            for mode, (repo, revision) in models.items()
        },
    }


# Проверяет режим и входные файлы, затем запоминает задание под номером попытки. Для старого
# номера сверяет отпечаток и возвращает текущее состояние, чтобы повтор не запустил вторую работу
# и не подменил исходный запрос.
@app.post("/jobs/{job_id}")
def submit(job_id: UUID, body: Input):
    if body.model == "image" and body.image:
        raise HTTPException(422, "Image reference is supported only for Video Lite")
    if body.model == "video_sr":
        if (
            not body.video
            or body.image
            or body.duration_seconds != 5
            or (
                body.quality != "standard"
                or body.resolution != "standard"
                or body.negative_prompt
                or body.guidance_scale is not None
            )
        ):
            raise HTTPException(422, "Video SR requires MP4 and fixed ×2 scale")
        if not Path(VIDEO_SR_PYTHON).is_file():
            raise HTTPException(503, "Video SR checkpoint is unavailable")
    elif body.video:
        raise HTTPException(422, "This model does not accept an input video")
    try:
        from .models import generation_spec

        if body.model != "video_sr":
            generation_spec(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Значения по умолчанию не меняют отпечаток. Так сервер узнаёт повтор запроса из старой версии
    # приложения.
    defaults = {
        "quality",
        "resolution",
        "duration_seconds",
        "negative_prompt",
        "guidance_scale",
        "video",
    }
    unchanged = {
        field for field in defaults if getattr(body, field) == Input.model_fields[field].default
    }
    encoded = body.model_dump_json(exclude=unchanged | {"canonical_job_id"})
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical = str(body.canonical_job_id or job_id)
        if ERASURE.contains(connection, str(job_id)) or ERASURE.contains(connection, canonical):
            raise HTTPException(410, "Generation erased")
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise HTTPException(409, "Operation payload changed")
            return {"status": row["status"]}
        count = connection.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if count >= 20:
            raise HTTPException(429, "GPU queue full")
        connection.execute(
            "INSERT INTO jobs(id,fingerprint,body,status,created,canonical) VALUES (?,?,?,?,?,?)",
            (str(job_id), fingerprint, encoded, "queued", time.time(), canonical),
        )
    return {"status": "queued"}


# Находит состояние задания и добавляет число процентов только для выполняющейся работы. Если
# исполнитель ещё не успел записать число, возвращает само состояние вместо отказа.
@app.get("/jobs/{job_id}")
def status(job_id: UUID):
    with db() as connection:
        if ERASURE.contains(connection, str(job_id)):
            raise HTTPException(410, "Generation erased")
        row = connection.execute("SELECT status FROM jobs WHERE id=?", (str(job_id),)).fetchone()
    if not row:
        raise HTTPException(404)
    result = {"status": row["status"]}
    if row["status"] == "running":
        try:
            progress = int((ROOT / str(job_id) / "progress").read_text())
            if 0 <= progress < 100:
                result["progress"] = progress
        except (OSError, ValueError):
            pass
    return result


# Передаёт исполнителю исходный номер задания и известную попытку для очистки. Если попытка
# принадлежала другой работе, сообщает о конфликте, чтобы команда не стёрла чужой результат.
@app.delete("/jobs/{job_id}", status_code=204)
def erase_job(job_id: UUID, run_id: UUID | None = None):
    try:
        ERASURE.erase(str(job_id), str(run_id) if run_id else None)
    except ValueError as error:
        raise HTTPException(409, "Attempt belongs to another generation") from error


# Отмечает остановку только для ожидающей или выполняющейся работы и возвращает итоговое
# состояние. Завершённое раньше задание не превращается задним числом в отменённое.
@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: UUID):
    with db() as connection:
        connection.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','running')",
            (str(job_id),),
        )
    return status(job_id)


# Выбирает изображение, если такой выходной файл существует, иначе видео, и возвращает файл с
# соответствующим видом содержимого.
# Проверяет, что задание завершилось успешно, и отдаёт готовый файл с типом фото или видео. Иначе
# вызывающая сторона могла бы получить неполный или ещё не записанный файл.
@app.get("/jobs/{job_id}/result")
def result(job_id: UUID):
    if status(job_id)["status"] != "succeeded":
        raise HTTPException(409)
    folder = ROOT / str(job_id)
    path = folder / "result.png"
    if not path.exists():
        path = folder / "result.mp4"
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "video/mp4")
