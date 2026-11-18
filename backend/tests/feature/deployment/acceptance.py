# Проверяет работающую сборку через обычные обращения на отдельном стенде. Сверяет повтор
# постановки, доступ к файлам, фото и видео и отзыв входа. При выборе восстановления передаёт это
# отдельной проверке.

"""Проверяет работающий сервер через запросы без подключения его исходников"""

import argparse
import json
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from client import DeploymentClient

parser = argparse.ArgumentParser()
parser.add_argument("--email", required=True, help="Свободный адрес проверочного ящика")
parser.add_argument("--url", default="http://127.0.0.1:8098")
parser.add_argument("--recovery", action="store_true")
parser.add_argument("--other-email", required=True)
parser.add_argument("--recovery-email")
a = parser.parse_args()
if a.recovery and not a.recovery_email:
    parser.error("--recovery requires --recovery-email")


client = DeploymentClient(a.url)
request = partial(client.request, expected=200)
suffix = secrets.token_hex(5)
token = client.signup(a.email, secrets.token_urlsafe(20))["token"]
other = client.signup(a.other_email, secrets.token_urlsafe(20))["token"]
thread = request("/v1/threads", token=token, method="POST", expected=201)["id"]
key = "acceptance-operation-" + suffix
body = dict(thread_id=thread, prompt="Белая магнолия у окна", model="image", aspect="1:1", seed=42)
start = time.monotonic()
with ThreadPoolExecutor(5) as pool:
    rows = list(pool.map(lambda _: request("/v1/jobs", body, token, key, expected=201), range(5)))
assert len({r["id"] for r in rows}) == 1
job = rows[0]
request("/v1/jobs/" + job["id"], token=other, expected=404)
for _ in range(120):
    job = request("/v1/jobs/" + job["id"], token=token)
    if job["status"] in ["succeeded", "failed"]:
        break
    time.sleep(0.5)
assert job["status"] == "succeeded", job
raw = request("/v1/assets/" + job["output_id"], token=token)
assert raw.startswith(b"\x89PNG")
request("/v1/assets/" + job["output_id"], token=other, expected=404)
ticket = request("/v1/assets/" + job["output_id"] + "/ticket", {}, token)
assert request(ticket["path"]).startswith(b"\x89PNG")
video = request("/v1/jobs", {**body, "model": "video"}, token, key + "-video", expected=201)
for _ in range(150):
    video = request("/v1/jobs/" + video["id"], token=token)
    if video["status"] in ["succeeded", "failed"]:
        break
    time.sleep(0.5)
assert video["status"] == "succeeded", video
assert b"ftyp" in request("/v1/assets/" + video["output_id"], token=token)[:20]
request("/v1/auth/logout", {}, token, expected=204)
request("/v1/auth/me", token=token, expected=401)
seconds = round(time.monotonic() - start, 2)

if a.recovery:
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("recovery.py")),
            "--url",
            a.url,
            "--email",
            a.recovery_email,
        ],
        check=True,
    )

print(
    json.dumps(
        {
            "ok": True,
            "provider": job["provider"],
            "image_job": job["id"],
            "video_job": video["id"],
            "seconds": seconds,
            "recovery_requested": a.recovery,
            "recovery_proven": a.recovery,
        },
        ensure_ascii=False,
    )
)
