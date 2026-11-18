# Связывает команды телефона с основным сервером и местной историей. Здесь запросы превращаются в
# задания, а их состояние и готовые файлы возвращаются в ленту с постоянными номерами.

"""Соединяет нативные клиенты Кандинский с существующей службой Кандинского."""

import asyncio
import re
import time
import uuid

from fastapi import HTTPException

from app.Exceptions.accounts import ErasedAccount
from app.Http.Requests.generation import JobRequest
from app.Http.Resources.Mobile.messages import message_pair, preview_blob_id, set_result_attachments
from app.Services.Database.Mobile.state import moderated_answer
from app.Services.Mobile import erasure_sync
from app.Services.Mobile.session_tokens import token_from
from app.Services.Mobile.studio import StudioClient


class Adapter:
    # Готовит связь с сервером, местную базу и общие ограничения для запросов. Одновременные
    # повторы загрузки, чтения одной группы и удаления проходят по очереди, чтобы изменения не
    # перезаписали друг друга.
    def __init__(self, client, state):
        self.studio = StudioClient(client)
        self.state = state
        self.metadata_slots = asyncio.Semaphore(4)
        self.metadata_cache = {}
        self.next_poll = {}
        self.upload_slots = tuple(asyncio.Lock() for _ in range(16))
        self.batch_slots = tuple(asyncio.Lock() for _ in range(32))
        self.removal_slots = tuple(asyncio.Lock() for _ in range(32))

    # Переводит ключ телефона в формат основного сервера и узнаёт владельца по действующему входу.
    # Иначе пришлось бы верить номеру аккаунта, присланному в теле команды.
    async def owner(self, headers):
        token = token_from(headers)
        return (await self.studio.request("GET", "/v1/auth/me", token))["user_id"], token

    # Удаляет данные владельцев в отдельном потоке и затем вычищает сведения из памяти службы.
    # Если убрать только строки базы, ещё можно было бы вернуть старое описание файла из памяти.
    async def erase_owners(self, owners):
        await asyncio.to_thread(self.state.erase_owners, owners)
        owners = set(owners)
        for key in tuple(self.metadata_cache):
            if key[0] in owners:
                self.metadata_cache.pop(key)
        for owner in owners:
            self.next_poll.pop(owner, None)

    # Сначала удаляет аккаунт на основном сервере с проверкой пароля, затем убирает его данные в
    # службе связи. Такой порядок защищает местную историю от очистки при отклонённом пароле.
    async def delete_account(self, owner, token, password):
        await self.studio.request("DELETE", "/v1/auth/me", token, json={"password": password})
        await self.erase_owners([owner])

    # Читает порции записей об удалённых аккаунтах и приводит номера владельцев к виду, принятому
    # в местной базе. После очистки подтверждает порцию. Если подтверждение не пройдёт, основной
    # сервер выдаст её снова.
    async def sync_account_erasures(self, key):
        headers = {"X-Kandinsky-Erasure-Key": key}
        path = "/v1/internal/account-erasures"
        while True:
            page = await self.studio.request("GET", path, headers=headers)
            owners = [str(uuid.UUID(owner)) for owner in page["owners"]]
            if owners:
                await self.erase_owners(owners)
                await self.studio.request(
                    "POST", path + "/ack", headers=headers, json={"owners": owners}
                )
            if len(owners) < 100:
                return

    # Читает порции результатов, скрытых основным сервером. Убирает вложения из истории и их
    # описания из памяти, затем подтверждает порцию, чтобы запоздавший телефон не получил прежний
    # файл.
    async def sync_result_removals(self, key):
        headers = {"X-Kandinsky-Erasure-Key": key}
        path = "/v1/internal/result-removals"
        while True:
            page = await self.studio.request("GET", path, headers=headers)
            items = [
                (
                    str(uuid.UUID(row["owner_id"])),
                    str(uuid.UUID(row["job_id"])),
                    str(uuid.UUID(row["asset_id"])) if row["asset_id"] else None,
                )
                for row in page["items"]
            ]
            if items:
                await asyncio.to_thread(self.state.remove_results, items)
                for owner, _, asset in items:
                    self.metadata_cache.pop((owner, asset), None)
                await self.studio.request(
                    "POST",
                    path + "/ack",
                    headers=headers,
                    json={"jobs": [job for _, job, _ in items]},
                )
            if len(items) < 100:
                return

    # Через ключ владельца забирает очередь удалений и применяет её до выдачи истории. Берёт общую
    # блокировку, иначе два телефона могли бы одновременно очистить и подтвердить одну порцию.
    async def sync_content_removals(self, owner, token):
        path = "/v1/jobs/native-removals"
        slot = self.removal_slots[uuid.UUID(owner).int % len(self.removal_slots)]
        async with slot:
            while True:
                items = (await self.studio.request("GET", path, token))["items"]
                await erasure_sync.apply_items(self, owner, items)
                if items:
                    await self.studio.request(
                        "POST", path + "/ack", token, json=[item["id"] for item in items]
                    )
                if len(items) < 32:
                    return

    # По номеру сообщения находит задание и сверяет вид сообщения с видом удаления. После команды
    # основному серверу дочитывает очередь, чтобы результат пропал и в местной истории.
    async def delete_content(self, owner, token, chat, message, target, blob):
        self.thread(owner, chat)
        job = self.state.boundary(owner, chat, message=message)
        expected = "prompt" if target == "prompt" else "answer"
        if not job or self.state.external(owner, expected, message) != job:
            raise HTTPException(404, "Запрос или результат не найден")
        params = {"target": target, **({"blob": blob} if blob else {})}
        await self.studio.request("DELETE", f"/v1/jobs/{job}/contents", token, params=params)
        await self.sync_content_removals(owner, token)

    # Сверяет отпечаток файла с прежней попыткой и использует ещё действующее разрешение на
    # отправку. Если результат уже записан, возвращает его номер. Так обрыв ответа не даёт
    # загрузить второй файл под той же попыткой.
    async def studio_upload(self, owner, token, operation, fingerprint, file, *, thread=None):
        mime = file[2]
        if not (mime.startswith("image/") or mime == "video/mp4"):
            raise HTTPException(422, "Выберите фото или MP4")
        slot = self.upload_slots[operation.int % len(self.upload_slots)]
        key = str(operation)
        async with slot:
            if self.state.upload_erased(owner, key):
                raise HTTPException(410, "Исходник удалён")
            previous = self.state.studio_upload(owner, key)
            if previous:
                saved_hash, route, expires, blob, size = previous
                if saved_hash != fingerprint:
                    raise HTTPException(409, "Загрузка уже относится к другому файлу")
                if blob:
                    if thread:
                        asset = str(uuid.UUID(blob))
                        await self.studio.request(
                            "POST", f"/v1/threads/{thread}/sources/{asset}", token
                        )
                    return blob, size
            else:
                route, expires = None, 0
            if not route or expires <= time.time():
                grant = await self.studio.request(
                    "POST",
                    "/v1/uploads/ticket",
                    token,
                    params={"thread_id": thread} if thread else {},
                )
                route = grant["path"]
                if not re.fullmatch(r"/v1/uploads/[A-Za-z0-9_-]{43}", route):
                    raise HTTPException(502, "Кандинский вернул неверное разрешение на загрузку")
                expires = time.time() + grant["expires_in"]
                self.state.begin_studio_upload(owner, key, fingerprint, route, expires)
            result = await self.studio.request("POST", route, files={"file": file})
            blob, size = uuid.UUID(result["id"]).hex, result["size"]
            self.state.complete_studio_upload(owner, key, blob, size)
            return blob, size

    # Сначала ищет свойства файла в памяти, при отсутствии читает их с сервера с ограничением
    # числа одновременных запросов. После ожидания проверяет, не удалили ли владельца или файл,
    # чтобы не вернуть старые данные. После ожидания свободного места заново проверяет и память,
    # потому что соседний запрос уже мог сохранить ответ.
    async def asset_metadata(self, owner, token, asset):
        key = owner, asset
        if key in self.metadata_cache:
            return self.metadata_cache[key]
        async with self.metadata_slots:
            if key not in self.metadata_cache:
                metadata = await self.studio.request("GET", f"/v1/assets/{asset}/metadata", token)
                if self.state.is_erased(owner):
                    raise ErasedAccount
                if self.state.media_removed(owner, uuid.UUID(asset).hex):
                    raise HTTPException(404, "Файл удалён")
                if len(self.metadata_cache) >= 256:
                    self.metadata_cache.pop(next(iter(self.metadata_cache)))
                self.metadata_cache[key] = metadata
            return self.metadata_cache[key]

    # Преобразует запись основного сервера в короткую карточку чата для телефона. Постоянный
    # местный номер нужен, чтобы телефон узнал чат при повторном открытии.
    def chat(self, owner, row):
        return {
            "id": self.state.identity(owner, "thread", row["id"]),
            "title": row["title"],
            "lastSeq": 0,
            "unreadCount": 0,
            "pinned": row.get("pinned_at") is not None,
        }

    # Дополняет карточку временем последней активности, отметками архива и генерации. По этим
    # полям телефон понимает, где показать чат и идёт ли в нём работа.
    def studio_thread(self, owner, row):
        return {
            **self.chat(owner, row),
            "activityAtMs": round(row["activity_at"] * 1000),
            "generating": row.get("generating", False),
            "archived": row.get("archived_at") is not None,
        }

    # Для группы результатов собирает готовые вложения и состояние каждой ячейки в один ответ.
    # Проводит чтение и местную запись под общей блокировкой группы, чтобы более медленный
    # читатель не затёр свежий ответ.
    async def rows(self, owner, token, job, publish=False):
        batch = job.get("batch_id")
        if batch is None:
            return await self.single_rows(owner, token, job, publish)
        lock = self.batch_slots[uuid.UUID(batch).int % len(self.batch_slots)]
        async with lock:
            # Читаем группу под общей блокировкой. Иначе поздний ответ истории мог бы затереть новый
            # результат.
            try:
                members = await self.studio.request("GET", f"/v1/jobs/{batch}/batch", token)
            except HTTPException as error:
                if error.status_code == 410:
                    return []
                raise
            root = members[0]
            pending = [member for member in members if member["status"] in {"queued", "running"}]
            resolved = await asyncio.gather(
                *(self.result_attachment(owner, token, member) for member in members)
            )
            attachments = [item for item in resolved if item is not None]
            if pending:
                display = next(
                    (member for member in pending if member["status"] == "running"), pending[0]
                )
            elif attachments:
                display = {"status": "succeeded"}
            else:
                display = next((member for member in members if member["status"] == "failed"), root)
            aggregate = {
                **root,
                "status": display["status"],
                "started_at": display.get("started_at"),
                "error": display.get("error"),
            }
            prompt, answer = message_pair(self.state, owner, aggregate)
            answer["metadata"]["kandinskyBatchSize"] = len(members)
            answer["metadata"]["kandinskyPendingCount"] = len(pending)
            answer["metadata"]["kandinskyMemberIds"] = [member["id"] for member in members]
            answer["metadata"]["kandinskySlots"] = [
                {
                    "id": member["id"],
                    "status": member["status"],
                    "createdAtMs": round(member["created_at"] * 1000),
                    "startedAtMs": round(member["started_at"] * 1000)
                    if member.get("started_at")
                    else None,
                    "progress": member.get("progress"),
                    "runId": member.get("run_id"),
                    **({"moderated": True} if member.get("moderated") else {}),
                    "blobId": item["blobId"] if item else None,
                }
                for member, item in zip(members, resolved, strict=True)
                if item is not None or member["status"] != "succeeded"
            ]
            if attachments:
                set_result_attachments(answer, attachments)
            elif not pending and any(member.get("moderated") for member in members):
                answer = moderated_answer(answer)
            self.state.update_pending(owner, aggregate)
            return self.state.remember_pair(owner, batch, prompt, answer, publish)

    # Собирает размеры, тип и номера файла и обложки готового результата. При пропаже файла
    # сверяет актуальное задание, чтобы отличить уже удалённый результат от неожиданной ошибки.
    # Для участника группы сохраняет номер его задания, чтобы повтор относился к нужной ячейке.
    async def result_attachment(self, owner, token, job):
        if job.get("moderated") or job["status"] != "succeeded" or not job.get("output_id"):
            return None
        asset = job["output_id"]
        try:
            meta = await self.asset_metadata(owner, token, asset)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            try:
                current = await self.studio.request("GET", f"/v1/jobs/{job['id']}", token)
            except HTTPException as latest:
                if latest.status_code == 410:
                    return None
                raise
            if not current.get("moderated") and current.get("removed_output_id") != asset:
                raise
            return None
        blob = uuid.UUID(asset).hex
        preview = None
        if meta.get("poster"):
            preview = preview_blob_id(asset)
            self.state.preview(owner, preview, asset)
        return {
            "blobId": blob,
            "previewBlobId": preview,
            "mime": meta["mime"],
            "byteSize": meta["size"],
            "rawWidth": meta["width"],
            "rawHeight": meta["height"],
            "orientation": 1,
            "durationMs": round((meta.get("duration_seconds") or 0) * 1000) or None,
            "tinyPreview": job.get("output_preview") or meta.get("tiny"),
            **({"resultJobId": job["id"]} if job.get("batch_id") else {}),
        }

    # Готовит запрос и ответ для одиночного задания, а для старого текстового примера сохраняет
    # только запрос. Если вложение успели удалить или скрыть, перечитывает задание, чтобы не
    # вернуть старое состояние.
    async def single_rows(self, owner, token, job, publish):
        prompt, answer = message_pair(self.state, owner, job)
        if (
            job.get("provider") == "sample"
            and job["status"] == "cancelled"
            and not job.get("output_id")
        ):
            return self.state.remember_sample_text(owner, job["id"], prompt, answer["id"], publish)
        if job.get("moderated"):
            answer = moderated_answer(answer)
        else:
            attachment = await self.result_attachment(owner, token, job)
            if attachment is not None:
                set_result_attachments(answer, [attachment])
            elif job["status"] == "succeeded" and job.get("output_id"):
                try:
                    current = await self.studio.request("GET", f"/v1/jobs/{job['id']}", token)
                except HTTPException as error:
                    if error.status_code == 410:
                        return []
                    raise
                return await self.rows(owner, token, current, publish)
        self.state.update_pending(owner, job)
        return self.state.remember_pair(owner, job["id"], prompt, answer, publish)

    # Преобразует местный номер чата в номер основного сервера в пределах текущего владельца.
    # Неизвестный номер останавливает запрос до отправки команды.
    def thread(self, owner, chat):
        thread = self.state.external(owner, "thread", chat)
        if not thread:
            raise HTTPException(404, "История не найдена")
        return thread

    # Проверяет, что задание относится к чату владельца, и выполняет проверку, отмену или повтор
    # задания. Для повтора различает всю группу и отдельную ячейку, чтобы не поменять готовые
    # соседние результаты.
    async def generation_action(self, owner, token, chat, message, action):
        thread = self.thread(owner, chat)
        job_id = self.state.boundary(owner, chat, message=message)
        if not job_id:
            raise HTTPException(404, "Задание не найдено")
        job = await self.studio.request("GET", f"/v1/jobs/{job_id}", token)
        if job["thread_id"] != thread:
            raise HTTPException(404, "Задание не найдено")
        batch = job.get("batch_id")
        if action.action == "cancel":
            if batch:
                await self.studio.request("POST", f"/v1/jobs/{batch}/batch/cancel", token)
            else:
                job = await self.studio.request("POST", f"/v1/jobs/{job_id}/cancel", token)
        elif action.action == "retry_slot":
            if action.slot is None or action.operation is None:
                raise HTTPException(422, "Выберите ячейку и номер повтора")
            members = (
                await self.studio.request("GET", f"/v1/jobs/{batch}/batch", token)
                if batch
                else [job]
            )
            if not any(member["id"] == str(action.slot) for member in members):
                raise HTTPException(404, "Задание не найдено")
            params = {"operation": str(action.operation)}
            if action.expected_run is not None:
                params["expected_run"] = str(action.expected_run)
            job = await self.studio.request(
                "POST", f"/v1/jobs/{action.slot}/retry", token, params=params
            )
        elif action.action == "retry":
            members = (
                await self.studio.request("GET", f"/v1/jobs/{batch}/batch", token)
                if batch
                else [job]
            )
            if any(member.get("moderated") for member in members):
                raise HTTPException(409, "Запрос заблокирован после проверки")
            if action.operation is None:
                raise HTTPException(422, "Укажите номер повтора")
            if any(member["status"] not in {"cancelled", "failed"} for member in members):
                raise HTTPException(409, "Повтор доступен после отмены или ошибки")
            if str(action.operation) == job["operation_key"]:
                raise HTTPException(409, "Для повтора нужен новый номер")
            settings = {key: job[key] for key in JobRequest.model_fields}
            if settings["model"] == "video_sr":
                settings["aspect"] = "1:1"
            job = await self.submit_jobs(token, settings, str(action.operation), len(members))
        return {"messages": await self.rows(owner, token, job, publish=True)}

    # Находит ответ и проверяет, что он принадлежит указанному чату. Передаёт жалобу по номеру
    # задания, чтобы основной сервер получил именно тот результат, на который пожаловались.
    async def report_generation(self, owner, token, chat, message, report):
        self.thread(owner, chat)
        job_id = self.state.external(owner, "answer", message)
        if not job_id or self.state.boundary(owner, chat, message=message) != job_id:
            raise HTTPException(404, "Результат не найден")
        return await self.studio.request(
            "POST", f"/v1/jobs/{job_id}/report", token, json=report.model_dump()
        )

    # Читает порцию изменений после отметки телефона и сверяет, какие чаты ещё существуют. После
    # сетевого запроса читает те же номера повторно, чтобы недавнее удаление не вернуло прежнее
    # содержимое.
    async def event_page(self, owner, token, cursor):
        events = self.state.events_after(owner, cursor)
        if not events:
            return None
        ids = {event["chatId"] for event in events}
        external = [self.state.external(owner, "thread", chat) for chat in ids]
        availability = await self.studio.request(
            "POST",
            "/v1/threads/states",
            token,
            json={"ids": [value for value in external if value]},
        )
        chats = [self.studio_thread(owner, row) for row in availability["threads"]]
        available = {row["id"] for row in chats}
        # Пока проверялся список чатов, удаление могло изменить содержимое этой порции событий.
        last_sequence = events[-1]["seq"]
        events = [
            row for row in self.state.events_after(owner, cursor) if row["seq"] <= last_sequence
        ]
        if not events:
            return None
        return {
            "type": "events",
            "cursor": last_sequence,
            "chats": chats,
            "events": [
                event
                if event["chatId"] in available
                else {
                    "seq": event["seq"],
                    "chatId": event["chatId"],
                    "kind": "kandinsky.retired",
                }
                for event in events
            ],
        }

    # Применяет очередные удаления, переводит границу страницы в номер задания и запрашивает
    # историю. Раскрывает каждое задание в пару сообщений, сохраняя для телефона возможность
    # дочитать предыдущую порцию.
    async def history(self, owner, token, chat, limit, before=None, publish=False):
        await self.sync_content_removals(owner, token)
        params = {"limit": max(1, limit // 2), "grouped": True}
        if before is not None:
            anchor = self.state.boundary(owner, chat, sequence=before)
            if not anchor:
                raise HTTPException(404, "Граница истории не найдена")
            params["before"] = anchor
        thread = self.thread(owner, chat)
        page = await self.studio.request(
            "GET", f"/v1/threads/{thread}/history", token, params=params
        )
        pairs = await asyncio.gather(
            *(self.rows(owner, token, job, publish) for job in page["messages"])
        )
        return {
            "messages": [r for pair in pairs for r in pair],
            "hasOlder": page["has_older"],
            "contentReceipts": [],
        }

    # Проверяет число результатов, исходный файл и параметры задания. Отправляет запрос под
    # постоянным номером попытки и записывает его в ленту, чтобы потеря ответа не превратилась в
    # двойную генерацию.
    async def submit(self, owner, token, message):
        operation = str(uuid.UUID(message["clientId"]))
        prompt = (message.get("text") or message.get("caption") or "").strip()
        generation = message.get("generationOptions") or {}
        if not isinstance(generation, dict):
            raise HTTPException(422, "Неверные параметры генерации")
        count = generation.get("count", 1)
        if (
            type(count) is not int
            or not 1 <= count <= 4
            or (generation.get("model") == "video_sr" and count != 1)
        ):
            raise HTTPException(422, "Выберите допустимое число результатов")
        settings = {
            k: generation[k]
            for k in (
                "model",
                "aspect",
                "seed",
                "quality",
                "resolution",
                "duration_seconds",
                "negative_prompt",
                "guidance_scale",
            )
            if k in generation
        }
        attachments = message.get("attachments") or []
        if len(attachments) > 1:
            raise HTTPException(422, "Выберите один исходный файл")
        if attachments:
            settings["input_id"] = str(uuid.UUID(attachments[0]["blobId"]))
            thread = self.thread(owner, message["chatId"])
            source = settings["input_id"]
            await self.studio.request("POST", f"/v1/threads/{thread}/sources/{source}", token)
            if preview := attachments[0].get("previewBlobId"):
                child = str(uuid.UUID(preview))
                await self.studio.request("POST", f"/v1/assets/{source}/previews/{child}", token)
            settings.setdefault("model", "video")
        seed = settings.pop("seed", int(uuid.UUID(operation)) % (2**31))
        if type(seed) is not int or not 0 <= seed < 2**31:
            raise HTTPException(422, "Неверное начальное число")
        if not prompt and attachments:
            prompt = (
                "Повысить разрешение видео"
                if settings.get("model") == "video_sr"
                else "Оживить фото"
            )
        body = {
            "thread_id": self.thread(owner, message["chatId"]),
            "prompt": prompt,
            **settings,
        }
        job = await self.submit_jobs(token, {**body, "seed": seed}, operation, count)
        rows = await self.rows(owner, token, job, publish=True)
        if not rows:
            raise HTTPException(410, "Запрос удалён")
        return rows[0]

    # Отправляет одно задание либо целую группу и передаёт номер попытки тем же способом. Основной
    # сервер сможет узнать повтор запроса независимо от числа результатов.
    async def submit_jobs(self, token, settings, operation, count):
        headers = {"Idempotency-Key": operation}
        if count == 1:
            return await self.studio.request(
                "POST", "/v1/jobs", token, json=settings, headers=headers
            )
        members = await self.studio.request(
            "POST", "/v1/jobs/batch", token, json={**settings, "count": count}, headers=headers
        )
        return members[0]

    # Проверяет ожидающие задания с общим ограничением частоты на владельца. Удаляет пропавшие
    # задания из списка и прерывает проверку при отзыве входа, чтобы лишние запросы не шли дальше.
    # Временный отказ одного задания не останавливает проверку остальных.
    async def poll(self, owner, token):
        now = time.time()
        if now < self.next_poll.get(owner, 0):
            return
        if len(self.next_poll) >= 512:
            self.next_poll = {user: limit for user, limit in self.next_poll.items() if limit > now}
        self.next_poll[owner] = now + 2
        for job in self.state.pending(owner):
            try:
                state = await self.studio.request("GET", f"/v1/jobs/{job}", token)
                await self.rows(owner, token, state, publish=True)
            except HTTPException as error:
                if error.status_code in {404, 410}:
                    with self.state.connection() as db:
                        db.execute("DELETE FROM pending WHERE owner=? AND id=?", (owner, job))
                elif error.status_code == 401:
                    raise
