# Проверяет сохранность задания при остановке и повторном запуске служб местного стенда. Затем
# восстанавливает копию в отдельную базу и ищет эту же запись, чтобы подтвердить пригодность
# копии.

"""Проверяет восстановление после перезапуска служб студии
Копия базы хранится в закрытой местной папке"""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from client import DeploymentClient

root = Path(__file__).resolve().parents[4]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--email", required=True, help="Свободный адрес проверочного ящика")
parser.add_argument("--url", default="http://127.0.0.1:8098")
options = parser.parse_args()
compose = (
    ["docker", "compose"]
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
    else ["docker-compose"]
)
client = DeploymentClient(options.url)
api = client.request


# Вызывает выбранное средство управления контейнерами из папки проекта и возвращает вывод. Ошибка
# команды немедленно останавливает сценарий, чтобы отчёт не посчитал неудачный перезапуск или
# восстановление успешным.
def command(*args, input=None):
    result = subprocess.run(
        [*compose, *args], cwd=root, input=input, capture_output=True, check=True
    )
    return result.stdout


suffix = secrets.token_hex(6)
user = client.signup(options.email, secrets.token_urlsafe(20))
token = user["token"]
thread = api("/v1/threads", token=token, method="POST")["id"]
command("stop", "worker")
try:
    job = api(
        "/v1/jobs",
        dict(thread_id=thread, prompt="Recovery check", model="image", aspect="1:1", seed=42),
        token,
        key="recovery-operation-" + suffix,
    )
    command("restart", "api")
    for _ in range(60):
        try:
            api("/health")
            break
        except Exception:
            time.sleep(0.5)
    assert api("/v1/jobs/" + job["id"], token=token)["status"] == "queued"
finally:
    command("start", "worker")
for _ in range(120):
    state = api("/v1/jobs/" + job["id"], token=token)
    if state["status"] == "succeeded":
        break
    time.sleep(0.5)
assert state["status"] == "succeeded", state
backup = command("exec", "-T", "db", "pg_dump", "-U", "studio", "--clean", "--if-exists", "studio")
path = root / "work/recovery-backup.sql"
path.parent.mkdir(exist_ok=True)
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as output:
    os.fchmod(output.fileno(), 0o600)
    output.write(backup)
restored = "studio_restore_" + suffix
command(
    "exec",
    "-T",
    "db",
    "psql",
    "-U",
    "studio",
    "-d",
    "postgres",
    "-c",
    f"CREATE DATABASE {restored}",
)
try:
    command(
        "exec",
        "-T",
        "db",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "studio",
        "-d",
        restored,
        input=backup,
    )
    count = command(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "studio",
        "-d",
        restored,
        "-Atc",
        f"SELECT count(*) FROM jobs WHERE id='{job['id']}'",
    ).strip()
    assert count == b"1"
finally:
    command(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "studio",
        "-d",
        "postgres",
        "-c",
        f"DROP DATABASE {restored}",
    )
print(
    json.dumps(
        dict(
            ok=True,
            api_restart_preserved_job=True,
            worker_resume=True,
            postgres_backup_restore=True,
            job_id=job["id"],
        )
    )
)
