# Готовит параметры краткой аренды и сохраняет её номер для последующей остановки. Здесь важно
# ограничить срок заранее и не заводить вторую машину, если прежняя ещё работает.

"""Время аренды ограничивает всю жизнь удалённого процесса

Образ с весами готовится до начала аренды
Чтение этого файла само по себе не обращается к платной службе
"""

import json
import os
from datetime import UTC, datetime

MAX_SECONDS = 3600


# Проверяет срок аренды и выбирает видеокарту и объём памяти для нужной модели. Проверка до
# запуска не даёт начать аренду с неподходящим сроком.
def sandbox_options(seconds, *, video_sr=False):
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("GPU lifetime must be an integer between 1 and 3600 seconds")
    return {
        "timeout": seconds,
        "gpu": "A100-80GB" if video_sr else "L4",
        "cpu": 4,
        "memory": 65536,
        "encrypted_ports": [8099],
        "workdir": "/app",
        "name": "kandinsky-studio",
    }


# Сохраняем номер аренды сразу после запуска, до получения адреса. По нему можно остановить уже оплачиваемый
# процесс.
# Проверяет срок и прежнюю аренду, открывает вычислительную машину и сразу сохраняет её номер.
# Если не удаётся сохранить состояние или получить адрес, останавливает машину, чтобы начисление
# за ненужную работу не продолжалось.
def start_bounded(sdk, app, image, token, state_file, seconds=MAX_SECONDS, *, video_sr=False):
    options = sandbox_options(
        seconds, video_sr=video_sr
    )  # Проверяем срок до обращения к платной службе
    if len(token) < 32:
        raise ValueError("GPU token must contain at least 32 characters")
    if state_file.exists():
        previous = json.loads(state_file.read_text())
        # Если не удаётся узнать состояние прежней аренды, новую не начинаем. Иначе можно платить за две
        # видеокарты.
        if (
            previous.get("sandbox_id")
            and sdk.Sandbox.from_id(previous["sandbox_id"]).poll() is None
        ):
            raise RuntimeError("GPU sandbox is already running; stop it first")
    sandbox = sdk.Sandbox.create(
        "python",
        "-m",
        "uvicorn",
        "gpu.bridge:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8099",
        "--no-access-log",
        app=app,
        image=image,
        secrets=[sdk.Secret.from_dict({"GPU_TOKEN": token})],
        env={
            "GPU_STATE_DIR": "/state",
            "KANDINSKY_MODELS": "/models",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        },
        **options,
    )
    record = {
        "sandbox_id": sandbox.object_id,
        "timeout": seconds,
        "started_at": datetime.now(UTC).isoformat(),
    }

    # Записывает состояние аренды в закрытый временный файл и заменяет прежний целиком. Следующий
    # запуск не увидит незаписанную до конца запись и сможет найти номер машины.
    def save():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = state_file.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(record, file, indent=2)
        os.replace(temp, state_file)

    try:
        save()  # Записываем номер процесса сразу, чтобы его можно было остановить без адреса
        record["url"] = sandbox.tunnels()[8099].url
        save()
        return record
    except BaseException as error:
        sandbox.terminate()
        sandbox.wait(raise_on_termination=False)
        if sandbox.poll() is None:
            raise RuntimeError("GPU termination is not confirmed; inspect Modal console") from error
        raise
