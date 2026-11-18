# Здесь задание на создание фото или видео превращается в сообщения для ленты. Телефон получает
# понятные подписи состояния, постоянный порядок сообщений и отдельные номера обложек.

"""Сообщения для телефона: запрос, ответ и готовые файлы."""

import hashlib
import time
import uuid

SYSTEM_SENDER = 0
STATUS = {
    "queued": "В очереди",
    "running": "Генерация",
    "failed": "Не удалось создать",
    "cancelled": "Генерация отменена",
    "succeeded": "Результат готов",
}


# Возвращает подпись для текущего состояния задания. Если оно простояло в очереди или продолжало
# выполняться пять минут, показывает, что время вышло, чтобы телефон не оставлял пользователя в
# ожидании.
def phase(job, now):
    status = job["status"]
    return (
        "Время вышло"
        if status in {"queued", "running"} and now >= job["created_at"] + 300
        else STATUS.get(status, status)
    )


# Собирает из одного задания сообщение пользователя и ответ приложения. Даёт им соседние номера
# порядка и указывает состояние ответа, чтобы запрос и результат шли рядом при чтении истории и
# при обновлении.
def message_pair(state, owner, job):
    chat_id = state.identity(owner, "thread", job["thread_id"])
    job_id = job["id"]
    saved_at_ms = round(job["created_at"] * 1000)
    created_at_us = round(job["created_at"] * 1_000_000)
    first_id_byte = int(uuid.UUID(job_id).hex[:2], 16)
    prompt_sequence = created_at_us * 1024 + 2 * first_id_byte
    common = {
        "chatId": chat_id,
        "savedAtMs": saved_at_ms,
        "kind": "text",
        "caption": "",
        "attachments": [],
    }
    prompt = {
        **common,
        "id": state.identity(owner, "prompt", job_id),
        "seq": prompt_sequence,
        "senderId": state.identity(owner, "user", owner),
        "clientId": job["operation_key"],
        "text": job["prompt"],
    }
    answer = {
        **common,
        "id": state.identity(owner, "answer", job_id),
        "seq": prompt_sequence + 1,
        "senderId": SYSTEM_SENDER,
        "clientId": job_id,
        "text": phase(job, time.time()),
        "metadata": {
            "kandinskyJobId": job_id,
            "kandinskyStatus": job["status"],
            "kandinskyModel": job["model"],
            "kandinskyAspect": job["aspect"],
            "kandinskyStartedAtMs": round(job["started_at"] * 1000)
            if job.get("started_at")
            else None,
            "kandinskyError": str(job["error"])[:400] if job.get("error") else None,
        },
    }
    if job.get("run_id") is not None:
        answer["metadata"]["kandinskyRunId"] = job["run_id"]
    if job.get("progress") is not None:
        answer["metadata"]["kandinskyProgress"] = job["progress"]
    return prompt, answer


# По номеру исходного файла вычисляет постоянный номер его обложки. Отдельный номер нужен, чтобы
# телефон мог отличить лёгкую обложку от полного файла при скачивании и хранении.
def preview_blob_id(asset_id: str) -> str:
    return hashlib.sha256(("preview:" + asset_id).encode()).hexdigest()[:32]


# По первому вложению определяет, что показывает ответ, фото или видео. Заменяет текст состояния
# готовыми вложениями, чтобы телефон показал результат в той же строке.
def set_result_attachments(answer, attachments):
    kind = "photo" if attachments[0]["mime"].startswith("image/") else "video"
    answer.update(text="", kind=kind, attachments=attachments)
