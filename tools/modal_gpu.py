# Готовит образ генератора, запускает обработку на ограниченное время и проверяет остановку.
# Запуск использует местные записи о подготовленном образе и закрытый ключ доступа.

"""Подготовка образа, запуск, проверка и остановка удалённой обработки

Подготовка загружает официальные модели до начала аренды видеокарты
Запуск выделяет один удалённый процесс на указанный срок до часа
Остановка ждёт подтверждения от службы
Ключи читаются из закрытых местных настроек
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
GPU_SOURCE = BACKEND / "app" / "Services" / "Gpu" / "Runner"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("MODAL_CONFIG_PATH", str(ROOT / "work/modal.toml"))

from app.Services.Gpu.Runner.lifecycle import sandbox_options, start_bounded
from app.Services.Gpu.Runner.models import DIFFUSERS_REVISION, VIDEO_SR_MODEL

STATE = ROOT / "work/modal-sandbox.json"
IMAGE = ROOT / "work/modal-image.json"
VIDEO_SR_IMAGE = ROOT / "work/modal-kandinsky-vsr-image.json"


# Получает отпечаток списка зависимостей. По нему видно, соответствует ли сохранённый образ
# нынешним требованиям.
def runtime_fingerprint():
    return hashlib.sha256((GPU_SOURCE / "requirements.txt").read_bytes()).hexdigest()


# Читает ключ службы генерации из местных настроек и сообщает, если он отсутствует. Ключ передают
# службе без печати в отчёт.
def secret():
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("STUDIO_GPU_TOKEN="):
            return line.partition("=")[2].strip()
    raise RuntimeError("Run python3 tools/configure.py first")


# Собирает основной образ и заранее загружает веса моделей без аренды видеокарты. Запоминает номер
# образа и отпечаток зависимостей для следующего запуска.
def prepare(sdk, app):
    image = (
        sdk.Image.debian_slim(python_version="3.12")
        .apt_install("ffmpeg", "git", "libgl1", "libglib2.0-0")
        .pip_install(
            "torch==2.13.0",
            "torchvision==0.28.0",
            "diffusers==0.43.0",
            "transformers==5.22.2",
            "accelerate==1.15.0",
            "sentencepiece",
            "ftfy",
            "imageio[ffmpeg]",
            "fastapi==0.135.1",
            "uvicorn==0.42.0",
        )
        .add_local_dir(
            GPU_SOURCE,
            "/app/gpu",
            copy=True,
            ignore=["__pycache__", "*.pyc", "test_*.py"],
        )
        .run_commands(
            'python -c "from diffusers import Kandinsky5T2IPipeline, Kandinsky5T2VPipeline, Kandinsky5I2VPipeline"',
            'PYTHONPATH=/app python -c "from gpu.models import prepare_models; prepare_models()"',
            "python -m pip freeze > /app/runtime-lock.txt",
        )
    )
    with sdk.enable_output():
        image.build(app)
    IMAGE.write_text(
        json.dumps(
            {
                "image_id": image.object_id,
                "diffusers_revision": DIFFUSERS_REVISION,
                "runtime_fingerprint": runtime_fingerprint(),
            },
            indent=2,
        )
    )
    print("CPU image and model weights prepared")


# Проверяет подходящий основной образ, затем добавляет отдельное окружение, программу и веса
# улучшения видео. Устаревшая основа не должна смешаться с новым набором.
def prepare_video_sr(sdk, app):
    record = json.loads(IMAGE.read_text())
    if record.get("runtime_fingerprint") != runtime_fingerprint():
        raise RuntimeError("Prepare the current base image first")
    repo, revision = VIDEO_SR_MODEL
    image = sdk.Image.from_id(record["image_id"]).run_commands(
        "python -m venv --system-site-packages /opt/kandinsky-vsr",
        "/opt/kandinsky-vsr/bin/python -m pip install --upgrade pip==26.3",
        "/opt/kandinsky-vsr/bin/python -m pip install --no-cache-dir "
        "'kandinsky-6-sr @ git+https://github.com/kandinskylab/kandinsky-6-sr.git@b15e2aac526519c51f59dfec2b848355bb8902de'",
        "/opt/kandinsky-vsr/bin/kandy-sr from-video --help > /app/kandy-sr-help.txt",
        f'python -c "from huggingface_hub import snapshot_download; '
        f"snapshot_download(repo_id={repo!r}, revision={revision!r}, "
        "local_dir='/models/video_sr', ignore_patterns=['assets/*', '*.md', '.gitattributes'])\"",
    )
    with sdk.enable_output():
        image.build(app)
    VIDEO_SR_IMAGE.write_text(
        json.dumps(
            {
                "image_id": image.object_id,
                "video_sr_checkpoint": repo,
                "runtime_fingerprint": record["runtime_fingerprint"],
                "video_sr_revision": revision,
                "diffusers_revision": record["diffusers_revision"],
            }
        )
    )
    print("Official Kandinsky Video SR CPU image prepared")


# Находит арендованный процесс по сохранённому номеру и ждёт завершения. Отмечает остановку только
# после получения кода выхода.
def stop(sdk):
    record = json.loads(STATE.read_text())
    sandbox = sdk.Sandbox.from_id(record["sandbox_id"])
    sandbox.terminate(wait=True)
    exit_code = sandbox.poll()
    if exit_code is None:
        raise RuntimeError("GPU termination not confirmed")
    record["stopped_at"] = time.time()
    record["exit_code"] = exit_code
    STATE.write_text(json.dumps(record, indent=2))
    print("GPU stopped; exit code:", exit_code)


# Читает команду и допустимый срок, затем выбирает подготовку, состояние, запуск или остановку.
# При запуске проверяет образ и готовность моделей, а при сбое просит остановить аренду.
def main():
    import modal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "start", "status", "stop"])
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--video-sr", action="store_true")
    args = parser.parse_args()
    sandbox_options(args.seconds)
    (ROOT / "work").mkdir(exist_ok=True)
    if args.command == "stop":
        stop(modal)
        return
    if args.command == "status":
        record = json.loads(STATE.read_text())
        record["exit_code"] = modal.Sandbox.from_id(record["sandbox_id"]).poll()
        print(json.dumps(record, indent=2))
        return
    app = modal.App.lookup("kandinsky-interview", create_if_missing=True)
    if args.command == "prepare":
        if args.video_sr:
            prepare_video_sr(modal, app)
        else:
            prepare(modal, app)
        return
    record = json.loads((VIDEO_SR_IMAGE if args.video_sr else IMAGE).read_text())
    if args.video_sr and record.get("video_sr_revision") != VIDEO_SR_MODEL[1]:
        raise RuntimeError("Wrong Kandinsky Video SR checkpoint")
    if record.get("runtime_fingerprint") != runtime_fingerprint():
        raise RuntimeError("Run prepare to update the model dependencies")
    if record["diffusers_revision"] != DIFFUSERS_REVISION:
        raise RuntimeError("Run prepare to cache the current Diffusers runtime")
    image = modal.Image.from_id(record["image_id"]).add_local_dir(
        GPU_SOURCE,
        "/app/gpu",
        copy=True,
        ignore=["__pycache__", "*.pyc", "test_*.py"],
    )
    with modal.enable_output():
        image.build(app)
    state = start_bounded(
        modal, app, image, secret(), STATE, seconds=args.seconds, video_sr=args.video_sr
    )
    try:
        import urllib.request

        for attempt in range(60):
            request = urllib.request.Request(
                state["url"] + "/health",
                headers={"Authorization": "Bearer " + secret()},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    health = json.load(response)
                if not all(model["present"] for model in health["models"].values()):
                    raise RuntimeError("Model weights are missing")
                if args.video_sr and "video_sr" not in health["models"]:
                    raise RuntimeError("Video SR runtime is missing")
                print("GPU bridge ready:", state["url"])
                return
            except OSError:
                if attempt == 59:
                    raise
                time.sleep(2)
    except BaseException:
        stop(modal)
        raise


if __name__ == "__main__":
    main()
