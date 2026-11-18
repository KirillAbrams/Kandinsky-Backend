# Измеряет время ответа на постановку ста местных заданий и сразу их отменяет. Отчёт помогает
# заметить задержку при приёме, отдельно от входа, работы модели и показа результата.

"""Измеряет время от отправки задания до ответа местного сервера"""

import argparse
import json
import secrets
import statistics
import time

from client import DeploymentClient

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--email", required=True, help="Свободный адрес проверочного ящика")
parser.add_argument("--url", default="http://127.0.0.1:8098")
options = parser.parse_args()
client = DeploymentClient(options.url, timeout=10)
request = client.request

suffix = secrets.token_hex(6)
token = client.signup(options.email, secrets.token_urlsafe(20))["token"]
thread = request("/v1/threads", token=token, method="POST")["id"]
measurements = []
for i in range(100):
    start = time.perf_counter()
    job = request(
        "/v1/jobs",
        dict(thread_id=thread, prompt=f"Benchmark {i}", model="image", aspect="1:1", seed=42),
        token,
        f"benchmark-{suffix}-{i}",
    )
    measurements.append((time.perf_counter() - start) * 1000)
    request("/v1/jobs/" + job["id"] + "/cancel", {}, token)
measurements.sort()
report = dict(
    samples=100,
    provider=job["provider"],
    median_ms=round(statistics.median(measurements), 2),
    p95_ms=round(measurements[94], 2),
    max_ms=round(max(measurements), 2),
    scope="local HTTP submit, excludes login/GPU/rendering",
)
print(json.dumps(report))
assert report["p95_ms"] < 2000
