# Проверяет обмен между клиентом, связующей службой и сервером генерации на временных базах.
# Сверяет номера чатов и заданий, историю, права владельца, вложения и доставку событий при
# повторах.

import asyncio
import hashlib
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.Models import Job
from app.Services.Generation.process_generation import process_one
from app.Services.Generation.queue import claim, record_progress
from tests.support.fixtures import BRIDGE_TEST_PASSWORD as PASSWORD
from tests.support.fixtures import MOBILE_FIXTURES, create_bridge, integration_clients
from tests.support.fixtures import register_mobile as register


# Создаёт первый чат с выбранным номером и повторяет тот же запрос. Связующая служба возвращает
# прежний чат, в базе остаётся одна запись, другому владельцу доступ закрыт.
def test_first_prompt_thread_identity_survives_retry_without_foreign_access(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "first-prompt@example.com")
        _, foreign = register(bridge, "other-first-prompt@example.com")
        thread_id = str(uuid.uuid4())
        assert bridge.get("/v1/kandinsky/threads", headers=owner).json()["threads"] == []
        initial = bridge.post("/v1/demo/chats", headers=owner, json={"id": thread_id})
        assert initial.status_code == 200
        replay = bridge.post("/v1/demo/chats", headers=owner, json={"id": thread_id})
        assert replay.json()["chatId"] == initial.json()["chatId"]
        assert (
            bridge.post("/v1/demo/chats", headers=foreign, json={"id": thread_id}).status_code
            == 404
        )
        login = upstream.post(
            "/v1/auth/login",
            json={"email": "first-prompt@example.com", "password": PASSWORD},
        )
        rows = upstream.get(
            "/v1/threads", headers={"Authorization": "Bearer " + login.json()["token"]}
        ).json()
        assert [row["id"] for row in rows] == [thread_id]


# Удаляет чат и перечитывает события с самого начала. Вместо старых строк приходят отметки об
# удалении с прежними номерами. Клиент продвигает позицию, не восстанавливая содержимое.
def test_retired_history_keeps_cursor_without_replaying_deleted_content(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "replay-after-deletion@example.com")
        retired = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json(
                {
                    "type": "send",
                    "requestId": "first",
                    "messages": [
                        {
                            "clientId": str(uuid.uuid4()),
                            "chatId": retired,
                            "text": "Previously removed prompt",
                            "attachments": [],
                        }
                    ],
                }
            )
            assert ws.receive_json()["type"] == "accepted"
            assert ws.receive_json()["cursor"] == 2
        assert bridge.delete(f"/v1/kandinsky/threads/{retired}", headers=auth).status_code == 204
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            replay = ws.receive_json()
            assert replay["cursor"] == 3
            assert replay["events"] == [
                {"seq": seq, "chatId": retired, "kind": "kandinsky.retired"} for seq in (1, 2, 3)
            ]
            assert replay["chats"] == []
            for _ in range(2):
                ws.send_json({"type": "applied", "cursor": replay["cursor"]})
                assert ws.receive_json() == {"type": "applied", "cursor": replay["cursor"]}


# Сверяет изменение названия, закрепление, поиск в архиве, восстановление и удаление чата.
# Действия чужого владельца отклоняются, чтобы список не зависел от чужих запросов.
def test_studio_drawer_uses_real_owner_search_archive_and_actions(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "drawer-owner@example.com")
        _, foreign = register(bridge, "drawer-foreign@example.com")
        chat = bridge.post("/v1/demo/chats", headers=owner, json={}).json()["chatId"]
        target = f"/v1/kandinsky/threads/{chat}"
        renamed = bridge.patch(target, headers=owner, json={"title": "Белая магнолия"})
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Белая магнолия"
        assert bridge.patch(target, headers=foreign, json={"title": "Чужой"}).status_code == 404
        assert bridge.patch(target, headers=owner, json={"pinned": True}).json()["pinned"]
        recent = bridge.get("/v1/kandinsky/threads", headers=owner, params={"q": "МАГНОЛИЯ"})
        assert [row["id"] for row in recent.json()["threads"]] == [chat]
        archived = bridge.patch(target, headers=owner, json={"archived": True}).json()
        assert archived["archived"] and not archived["pinned"]
        assert bridge.get("/v1/kandinsky/threads", headers=owner).json()["threads"] == []
        archive = bridge.get("/v1/kandinsky/threads", headers=owner, params={"view": "archive"})
        assert [row["id"] for row in archive.json()["threads"]] == [chat]
        assert bridge.patch(target, headers=owner, json={"archived": False}).status_code == 200
        assert bridge.delete(target, headers=foreign).status_code == 404
        assert bridge.delete(target, headers=owner).status_code == 204
        assert (
            bridge.get("/v1/kandinsky/threads", headers=owner, params={"view": "all"}).json()[
                "threads"
            ]
            == []
        )


# Отправляет запрос на три результата через постоянное соединение. Проверяет три задания с разными
# начальными числами и две строки ленты. Повтор возвращает прежние строки, иначе в чате появились
# бы дубли.
def test_generation_count_keeps_each_server_job_and_seed_on_replay(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "multiple-results@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        operation = str(uuid.uuid4())
        packet = {
            "type": "send",
            "requestId": "multiple",
            "messages": [
                {
                    "clientId": operation,
                    "chatId": chat,
                    "kind": "text",
                    "text": "Горное озеро",
                    "attachments": [],
                    "generationOptions": {
                        "model": "image",
                        "count": 3,
                        "quality": "draft",
                        "seed": 2147483647,
                    },
                },
            ],
        }
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json(packet)
            ack = ws.receive_json()
            assert ack["messages"][0]["clientId"] == operation
            native = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()
            assert len(native["messages"]) == 2
            events = ws.receive_json()["events"]
            assert len(events) == 2
            assert {event["message"]["id"] for event in events} == {
                row["id"] for row in native["messages"]
            }
            ws.send_json(packet)
            assert ws.receive_json()["messages"][0]["id"] == ack["messages"][0]["id"]
        native = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()
        assert len(native["messages"]) == 2
        login = upstream.post(
            "/v1/auth/login",
            json={"email": "multiple-results@example.com", "password": PASSWORD},
        )
        studio = {"Authorization": "Bearer " + login.json()["token"]}
        thread = upstream.get("/v1/threads", headers=studio).json()[0]
        jobs = upstream.get(f"/v1/threads/{thread['id']}/jobs", headers=studio).json()
        assert len(jobs) == 3
        assert {row["seed"] for row in jobs} == {2147483647, 0, 1}
        assert {row["quality"] for row in jobs} == {"draft"}


# Отправляет один текст для четырёх результатов и проходит завершение, отмену и повтор отдельных
# частей. Сверяет номера строк, ход, версии и порядок вложений, чтобы перегрузка страницы не
# разорвала общую группу.
@pytest.mark.parametrize(
    "completion", ["complete", "cancel", "retry_first_attempt", "retry_later_attempt"]
)
def test_album_keeps_one_prompt_and_cursor_as_four_jobs_finish(tmp_path, completion):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "album@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        packets = [
            {"clientId": str(uuid.uuid4()), "chatId": chat, "text": "Первый запрос"},
            {
                "clientId": str(uuid.uuid4()),
                "chatId": chat,
                "text": "Четыре варианта озера",
                "generationOptions": {"model": "image", "count": 4, "seed": 200},
            },
        ]
        accepted = []
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            for packet in packets:
                ws.send_json(
                    {"type": "send", "requestId": packet["clientId"], "messages": [packet]}
                )
                accepted.append(ws.receive_json()["messages"][0])
                assert len(ws.receive_json()["events"]) == 2
        endpoint = f"/v1/chats/{chat}/history"
        latest = bridge.get(endpoint, headers=auth, params={"limit": 2}).json()
        first_prompt, first_answer = latest["messages"]
        assert first_prompt["id"] == accepted[1]["id"]
        assert first_answer["attachments"] == []
        slots = first_answer["metadata"].get("kandinskySlots")
        assert slots is not None, "all four cells must be present before any result is ready"
        assert len(slots) == 4 and len({slot["id"] for slot in slots}) == 4
        assert all(slot["status"] == "queued" and slot["createdAtMs"] > 0 for slot in slots)
        slot_ids = [slot["id"] for slot in slots]
        assert latest["hasOlder"]
        before = bridge.get(
            endpoint, headers=auth, params={"limit": 2, "before": first_prompt["seq"]}
        ).json()
        assert before["messages"][0]["id"] == accepted[0]["id"]
        assert not before["hasOlder"]

        factory, settings = upstream.app.state.factory, upstream.app.state.settings
        assert asyncio.run(process_one(factory, settings))
        leased = claim(factory, 90)
        assert leased.id == slot_ids[0]
        record_progress(factory, leased.id, leased.lease_owner, 37)
        active = bridge.get(endpoint, headers=auth, params={"limit": 2}).json()["messages"][1]
        states = active["metadata"]["kandinskySlots"]
        assert [slot["status"] for slot in states] == ["running", "queued", "queued", "queued"]
        assert [slot["progress"] for slot in states] == [37, None, None, None]
        assert (
            active["seq"] == first_answer["seq"] and active["revision"] > first_answer["revision"]
        )
        with factory.begin() as db:
            db.get(Job, leased.id).lease_until = 0
        for count in range(1, 5):
            assert asyncio.run(process_one(factory, settings))
            page = bridge.get(endpoint, headers=auth, params={"limit": 2}).json()
            prompt, answer = page["messages"]
            assert (prompt["id"], answer["id"]) == (first_prompt["id"], first_answer["id"])
            assert answer["seq"] == first_answer["seq"]
            assert answer["revision"] > first_answer["revision"]
            assert len(answer["attachments"]) == count
            slots = answer["metadata"]["kandinskySlots"]
            assert [slot["id"] for slot in slots] == slot_ids
            assert [slot["blobId"] for slot in slots if slot["status"] == "succeeded"] == [
                item["blobId"] for item in answer["attachments"]
            ]
            assert all(slot["startedAtMs"] for slot in slots if slot["status"] == "succeeded")
            assert len({item["blobId"] for item in answer["attachments"]}) == count
            if completion in {"retry_first_attempt", "retry_later_attempt"}:
                failed = slot_ids[2]
                previous_run = str(uuid.uuid4()) if completion == "retry_later_attempt" else None
                with factory.begin() as db:
                    for slot_id in slot_ids[1:]:
                        item = db.get(Job, slot_id)
                        item.status, item.error = "failed", "GPU model failed"
                        if slot_id == failed:
                            item.run_id = previous_run
                terminal = bridge.get(endpoint, headers=auth, params={"limit": 2}).json()[
                    "messages"
                ][1]
                assert terminal["metadata"]["kandinskyStatus"] == "succeeded"
                assert [slot["status"] for slot in terminal["metadata"]["kandinskySlots"]] == [
                    "succeeded",
                    "failed",
                    "failed",
                    "failed",
                ]
                action = f"/v1/kandinsky/threads/{chat}/generations/{answer['id']}/action"
                payload = {
                    "action": "retry_slot",
                    "slot": failed,
                    "operation": str(uuid.uuid4()),
                }
                if previous_run is not None:
                    payload["expected_run"] = previous_run
                result = bridge.post(action, headers=auth, json=payload)
                assert result.status_code == 200
                retried = result.json()["messages"][1]
                assert (retried["id"], retried["seq"], retried["attachments"]) == (
                    answer["id"],
                    answer["seq"],
                    answer["attachments"],
                )
                updated = retried["metadata"]["kandinskySlots"]
                assert [slot["id"] for slot in updated] == slot_ids
                assert [slot["status"] for slot in updated] == [
                    "succeeded",
                    "failed",
                    "queued",
                    "failed",
                ]
                assert retried["revision"] > terminal["revision"]
                assert updated[2]["runId"]
                assert (
                    bridge.get(endpoint, headers=auth, params={"limit": 2}).json()["messages"][1]
                    == retried
                )
                assert (
                    bridge.post(action, headers=auth, json=payload).json()["messages"][1] == retried
                )
                assert (
                    bridge.post(
                        action, headers=auth, json={**payload, "operation": str(uuid.uuid4())}
                    ).status_code
                    == 409
                )
                return
            if completion != "complete":
                action = f"/v1/kandinsky/threads/{chat}/generations/{answer['id']}/action"
                stopped = bridge.post(action, headers=auth, json={"action": "cancel"})
                assert stopped.status_code == 200
                current = stopped.json()["messages"][1]
                assert (current["id"], current["seq"]) == (answer["id"], answer["seq"])
                assert current["attachments"] == answer["attachments"]
                assert [slot["status"] for slot in current["metadata"]["kandinskySlots"]] == [
                    "succeeded",
                    "cancelled",
                    "cancelled",
                    "cancelled",
                ]
                assert claim(factory, 90) is None
                return

        login = upstream.post(
            "/v1/auth/login", json={"email": "album@example.com", "password": PASSWORD}
        )
        studio = {"Authorization": "Bearer " + login.json()["token"]}
        thread = upstream.get("/v1/threads", headers=studio).json()[0]
        jobs = upstream.get(f"/v1/threads/{thread['id']}/jobs", headers=studio).json()
        expected = [
            uuid.UUID(job["output_id"]).hex
            for job in sorted(jobs, key=lambda job: job["seed"])
            if job["prompt"] == packets[1]["text"]
        ]
        assert [item["blobId"] for item in answer["attachments"]] == expected


# Заполняет очередь так, что четыре задания целиком не помещаются, и проверяет отсутствие
# частичной вставки. После освобождения места повтор возвращает те же четыре номера, чужое чтение
# и изменение числа отклоняются.
def test_batch_queue_admission_and_replay_are_atomic(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "batch-admission@example.com")
        _, other = register(bridge, "batch-other@example.com")
        bridge.post("/v1/demo/chats", headers=owner, json={}).raise_for_status()

        # Выполняет обычный вход в основной сервер по адресу владельца и возвращает данные
        # доступа. Это позволяет сверить очередь напрямую у каждого владельца.
        def studio_auth(email):
            login = upstream.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
            return {"Authorization": "Bearer " + login.json()["token"]}

        owner, other = (
            studio_auth("batch-admission@example.com"),
            studio_auth("batch-other@example.com"),
        )
        thread = upstream.get("/v1/threads", headers=owner).json()[0]["id"]
        options = {"thread_id": thread, "prompt": "Проверка очереди"}
        for _ in range(8):
            upstream.post(
                "/v1/jobs",
                json=options,
                headers={
                    **owner,
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            ).raise_for_status()

        headers = {**owner, "Idempotency-Key": str(uuid.uuid4())}
        group = {**options, "count": 4}
        assert upstream.post("/v1/jobs/batch", headers=headers, json=group).status_code == 429
        list_path = f"/v1/threads/{thread}/jobs"
        existing = upstream.get(list_path, headers=owner).json()
        assert len(existing) == 8
        for job in existing[:2]:
            upstream.post(f"/v1/jobs/{job['id']}/cancel", headers=owner).raise_for_status()
        created = upstream.post("/v1/jobs/batch", headers=headers, json=group)
        assert created.status_code == 201
        ids = [job["id"] for job in created.json()]
        assert len(set(ids)) == 4
        replay = upstream.post("/v1/jobs/batch", headers=headers, json=group)
        assert [job["id"] for job in replay.json()] == ids
        assert (
            upstream.post("/v1/jobs/batch", headers=headers, json={**group, "count": 3}).status_code
            == 409
        )
        assert upstream.get(f"/v1/jobs/{ids[0]}/batch", headers=other).status_code == 404
        cancelled = upstream.post(f"/v1/jobs/{ids[0]}/batch/cancel", headers=owner).json()
        assert [job["status"] for job in cancelled] == ["cancelled"] * 4
        assert len(upstream.get(list_path, headers=owner).json()) == 12


# Проверяет чтение и отмену по прежнему номеру строки, затем повтор с отдельным номером операции.
# Сверяет исходник и настройки видео с серверной записью, чтобы повтор не поменял запрос или не
# создал лишнюю работу.
def test_generation_actions_keep_row_and_retry_exact_job_with_one_operation(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "generation-actions@example.com")
        _, foreign = register(bridge, "other-generation-actions@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        operation = str(uuid.uuid4())
        with bridge.websocket_connect("/v1/ws", headers=auth) as socket:
            socket.send_json({"type": "resume", "cursor": 0})
            assert socket.receive_json()["type"] == "resumed"
            socket.send_json(
                {
                    "type": "send",
                    "requestId": "original",
                    "messages": [
                        {
                            "clientId": operation,
                            "chatId": chat,
                            "text": "Туман над озером",
                            "attachments": [],
                            "generationOptions": {
                                "model": "image",
                                "aspect": "5:3",
                                "quality": "draft",
                                "resolution": "compact",
                                "seed": 76213,
                                "negative_prompt": "лодка",
                                "guidance_scale": 4.5,
                            },
                        }
                    ],
                }
            )
            assert socket.receive_json()["type"] == "accepted"
            assert socket.receive_json()["cursor"] == 2
        rows = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()["messages"]
        answer = rows[1]
        action = f"/v1/kandinsky/threads/{chat}/generations/{answer['id']}/action"
        retry = {"action": "retry", "operation": str(uuid.uuid4())}
        assert bridge.post(action, headers=auth, json={"action": "retry_slot"}).status_code == 422
        assert (
            bridge.post(
                action,
                headers=auth,
                json={
                    "action": "retry_slot",
                    "slot": str(uuid.uuid4()),
                    "operation": str(uuid.uuid4()),
                },
            ).status_code
            == 404
        )
        missing_action = f"/v1/kandinsky/threads/{chat}/generations/999999/action"
        assert (
            bridge.post(missing_action, headers=auth, json={"action": "check"}).status_code == 404
        )
        assert (
            bridge.delete(
                f"/v1/kandinsky/threads/{chat}/generations/{answer['id']}",
                headers=auth,
                params={"target": "prompt"},
            ).status_code
            == 404
        )
        assert bridge.post(action, headers=foreign, json={"action": "cancel"}).status_code == 404
        assert bridge.post(action, headers=auth, json=retry).status_code == 409
        checked = bridge.post(action, headers=auth, json={"action": "check"}).json()["messages"][1]
        assert (checked["id"], checked["text"]) == (answer["id"], "В очереди")
        assert checked["metadata"]["kandinskyAspect"] == "5:3"
        cancelled = bridge.post(action, headers=auth, json={"action": "cancel"}).json()["messages"][
            1
        ]
        assert (cancelled["id"], cancelled["text"]) == (
            answer["id"],
            "Генерация отменена",
        )
        assert bridge.post(action, headers=auth, json={"action": "retry"}).status_code == 422
        assert (
            bridge.post(
                action, headers=auth, json={"action": "retry", "operation": operation}
            ).status_code
            == 409
        )
        repeated = bridge.post(action, headers=auth, json=retry).json()["messages"]
        assert repeated[1]["id"] != answer["id"]
        assert bridge.post(action, headers=auth, json=retry).json()["messages"] == repeated
        session = upstream.post(
            "/v1/auth/login",
            json={"email": "generation-actions@example.com", "password": PASSWORD},
        ).json()["token"]
        owner = {"Authorization": "Bearer " + session}
        thread = upstream.get("/v1/threads", headers=owner).json()[0]
        jobs = upstream.get(f"/v1/threads/{thread['id']}/jobs", headers=owner).json()
        assert len(jobs) == 2
        saved = {j["operation_key"]: j for j in jobs}
        original, new = saved[operation], saved[retry["operation"]]
        for key in (
            "prompt",
            "model",
            "aspect",
            "quality",
            "resolution",
            "seed",
            "input_id",
            "negative_prompt",
            "guidance_scale",
            "duration_seconds",
        ):
            assert new[key] == original[key]
        assert original["status"] == "cancelled" and new["status"] == "queued"

        photo = MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
        upload = upstream.post(
            "/v1/assets",
            headers=owner,
            files={"file": ("reference.jpg", photo.read_bytes(), "image/jpeg")},
        )
        assert upload.status_code in {200, 201}
        input_id = upload.json()["id"]
        video_operation = str(uuid.uuid4())
        video = upstream.post(
            "/v1/jobs",
            headers={**owner, "Idempotency-Key": video_operation},
            json={
                "thread_id": thread["id"],
                "prompt": "Slow pan over the original photo",
                "model": "video",
                "aspect": "3:5",
                "resolution": "compact",
                "duration_seconds": 3,
                "seed": 147,
                "input_id": input_id,
            },
        )
        assert video.status_code in {200, 201, 202}
        messages = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()["messages"]
        video_answer = next(
            row
            for row in messages
            if row.get("metadata", {}).get("kandinskyJobId") == video.json()["id"]
        )
        video_action = f"/v1/kandinsky/threads/{chat}/generations/{video_answer['id']}/action"
        assert bridge.post(video_action, headers=auth, json={"action": "cancel"}).status_code == 200
        video_retry = str(uuid.uuid4())
        assert (
            bridge.post(
                video_action,
                headers=auth,
                json={"action": "retry", "operation": video_retry},
            ).status_code
            == 200
        )
        jobs = upstream.get(f"/v1/threads/{thread['id']}/jobs", headers=owner).json()
        replayed = next(row for row in jobs if row["operation_key"] == video_retry)
        assert (
            replayed["input_id"],
            replayed["model"],
            replayed["aspect"],
            replayed["duration_seconds"],
            replayed["seed"],
        ) == (input_id, "video", "3:5", 3, 147)


# Открывает два сеанса одного владельца и выходит из первого. Первый больше не читает список,
# второй продолжает работать, иначе выход с телефона завершал бы соседний сеанс.
def test_logout_revokes_only_the_current_token(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "logout@example.com")
        login = bridge.post(
            "/v1/demo/login", json={"email": "logout@example.com", "password": PASSWORD}
        )
        other = {"Authorization": "Bearer " + login.json()["token"]}
        assert bridge.post("/v1/kandinsky/logout", headers=auth).status_code == 204
        assert bridge.get("/v1/kandinsky/threads", headers=auth).status_code == 401
        assert bridge.get("/v1/kandinsky/threads", headers=other).status_code == 200


# Проходит вход, добавление устройства, отправку, повтор и выдачу файла по обе стороны связи.
# Сверяет один запрос в основной базе, новую версию ответа в той же строке, чтение части файла и
# запрет для другого владельца.
def test_integration_replay_and_upstream_crosscheck(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        profile, auth = register(bridge, "one@example.com")
        login = bridge.post(
            "/v1/demo/login", json={"email": "one@example.com", "password": PASSWORD}
        )
        assert login.json()["userId"] == profile["userId"]
        enrolled = bridge.post("/v1/devices/enroll", json={"invitation": profile["token"]})
        assert enrolled.status_code == 200
        assert enrolled.json()["userId"] == profile["userId"]
        assert bridge.post("/v1/devices/enroll", json={"invitation": "0" * 64}).status_code == 401
        created = bridge.post("/v1/demo/chats", headers=auth, json={}).json()
        chat = created["chat"]["id"]
        assert chat == created["chatId"]
        bootstrap = bridge.get("/v1/bootstrap", headers=auth).json()
        assert bootstrap["userId"] == profile["userId"]
        assert [r["id"] for r in bootstrap["chats"]] == [chat]
        operation = str(uuid.uuid4())
        send = {
            "type": "send",
            "requestId": "one",
            "messages": [
                {
                    "clientId": operation,
                    "chatId": chat,
                    "kind": "text",
                    "text": "Магнолия",
                    "attachments": [],
                },
            ],
        }
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": bootstrap["cursor"]})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json(send)
            assert ws.receive_json()["messages"][0]["clientId"] == operation
            updates = ws.receive_json()
            assert [r["message"]["text"] for r in updates["events"]] == [
                "Магнолия",
                "В очереди",
            ]
            prompt, waiting = [r["message"] for r in updates["events"]]
            ws.send_json(send)
            assert ws.receive_json()["messages"][0]["id"] == prompt["id"]
            assert asyncio.run(process_one(upstream.app.state.factory, upstream.app.state.settings))
            ws.send_json({"type": "kandinsky.check", "chatId": chat, "messageId": waiting["id"]})
            ready = ws.receive_json()["events"][0]["message"]
            assert ready["id"] == waiting["id"] and ready["kind"] == "photo"
            assert ready["revision"] > waiting["revision"]
            blob = ready["attachments"][0]["blobId"]
        page = bridge.get(f"/v1/chats/{chat}/history", headers=auth).json()
        assert [x["id"] for x in page["messages"]] == [prompt["id"], waiting["id"]]
        assert page["hasOlder"] is False
        image = bridge.get("/v1/media/" + blob, headers={**auth, "Range": "bytes=0-7"})
        assert image.status_code == 206 and image.content == b"\x89PNG\r\n\x1a\n"
        studio_login = upstream.post(
            "/v1/auth/login", json={"email": "one@example.com", "password": PASSWORD}
        )
        studio_auth = {"Authorization": "Bearer " + studio_login.json()["token"]}
        threads = upstream.get("/v1/threads", headers=studio_auth).json()
        jobs = upstream.get(f"/v1/threads/{threads[0]['id']}/jobs", headers=studio_auth).json()
        assert len(jobs) == 1 and jobs[0]["operation_key"] == operation
        assert jobs[0]["status"] == "succeeded"
        _, foreign = register(bridge, "two@example.com")
        assert bridge.get(f"/v1/chats/{chat}/history", headers=foreign).status_code == 404
        assert bridge.get("/v1/media/" + blob, headers=foreign).status_code == 404


# Старит незавершённое задание до сообщения об истекшем времени, затем доводит его до результата.
# Связующая служба заменяет сообщение в прежней строке и убирает ожидание, чтобы готовая работа не
# потерялась.
def test_poll_delivers_generation_that_finishes_after_deadline(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, headers = register(bridge, "late-result@example.com")
        chat = bridge.post("/v1/demo/chats", headers=headers, json={}).json()["chatId"]
        adapter = bridge.app.state.adapter
        owner, token = bridge.portal.call(adapter.owner, httpx.Headers(headers))
        studio_headers = {"Authorization": "Bearer " + token}
        thread = upstream.get("/v1/threads", headers=studio_headers).json()[0]["id"]
        operation = str(uuid.uuid4())
        response = upstream.post(
            "/v1/jobs",
            headers={**studio_headers, "Idempotency-Key": operation},
            json={"thread_id": thread, "prompt": "Белый маяк", "model": "image"},
        )
        assert response.status_code == 201
        job = response.json()["id"]
        factory = upstream.app.state.factory
        with factory.begin() as db:
            db.execute(update(Job).where(Job.id == job).values(created_at=time.time() - 301))
        history = bridge.get(f"/v1/chats/{chat}/history", headers=headers).json()
        waiting = history["messages"][1]
        assert waiting["text"] == "Время вышло"
        assert asyncio.run(process_one(factory, upstream.app.state.settings))

        bridge.portal.call(adapter.poll, owner, token)
        result = adapter.state.events_after(owner, 0)[-1]["message"]
        assert result["id"] == waiting["id"]
        assert result["revision"] > waiting["revision"]
        assert result["metadata"]["kandinskyJobId"] == job
        assert result["kind"] == "photo" and result["attachments"]
        assert adapter.state.pending(owner) == []


# Отправляет жалобу на фото или ролик и сверяет отзыв результата и миниатюры после решения.
# Подписчик получает новую версию той же строки, а повтор событий не возвращает старые вложения.
@pytest.mark.parametrize("model,result_kind", [("image", "photo"), ("video", "video")])
def test_report_and_reconcile_revoked_result_without_replaying_its_preview(
    tmp_path, model, result_kind
):
    from app.Services.Database.Mobile.state import State
    from app.Services.Moderation.reports import resolve_report

    key = "isolated-removal-delivery-key"
    upstream, bridge = integration_clients(tmp_path, erasure_key=key)
    with upstream, bridge:
        _, auth = register(bridge, "report-result@example.com")
        _, foreign = register(bridge, "report-foreign@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        adapter = bridge.app.state.adapter
        owner, token = bridge.portal.call(adapter.owner, httpx.Headers(auth))
        command = {
            "clientId": str(uuid.uuid4()),
            "chatId": chat,
            "text": "Белый маяк",
            "attachments": [],
        }
        if model == "video":
            command["generationOptions"] = {
                "model": "video",
                "aspect": "5:3",
                "resolution": "compact",
                "duration_seconds": 3,
            }
        prompt = bridge.portal.call(adapter.submit, owner, token, command)
        factory = upstream.app.state.factory
        settings = upstream.app.state.settings
        assert asyncio.run(process_one(factory, settings))
        bridge.portal.call(adapter.poll, owner, token)
        ready = adapter.state.events_after(owner, 0)[-1]["message"]
        assert ready["kind"] == result_kind
        attachment = ready["attachments"][0]
        preview = f"/v1/media/{attachment['previewBlobId']}"
        assert attachment["previewBlobId"] and bridge.get(preview, headers=auth).status_code == 200
        route = f"/v1/kandinsky/threads/{chat}/generations"
        report = f"{route}/{ready['id']}/report"
        assert bridge.post(report, headers=foreign, json={"reason": "other"}).status_code == 404
        assert (
            bridge.post(
                f"{route}/{prompt['id']}/report", headers=auth, json={"reason": "other"}
            ).status_code
            == 404
        )
        response = bridge.post(report, headers=auth, json={"reason": "other"})
        assert response.status_code == 200
        job = response.json()["job_id"]
        before = adapter.state.cursor(owner)
        with factory() as db:
            resolve_report(db, settings.media_dir, job, "remove", "operator")
        for blob in (attachment["blobId"], attachment["previewBlobId"]):
            if blob:
                assert bridge.get(f"/v1/media/{blob}", headers=auth).status_code == 404
        feed = "/v1/internal/result-removals"
        assert upstream.get(feed).status_code == 403
        delivery = upstream.get(feed, headers={"X-Kandinsky-Erasure-Key": key}).json()["items"]
        restarted = State(tmp_path / "bridge.sqlite")
        restarted.remove_results(
            [(row["owner_id"], row["job_id"], row["asset_id"]) for row in delivery]
        )
        bridge.portal.call(adapter.sync_result_removals, key)
        assert upstream.get(feed, headers={"X-Kandinsky-Erasure-Key": key}).json() == {"items": []}
        updated = adapter.state.events_after(owner, before)
        assert len(updated) == 1
        blocked = updated[0]["message"]
        assert blocked["id"] == ready["id"] and blocked["revision"] == ready["revision"] + 1
        assert blocked["kind"] == "text" and not blocked["attachments"]
        assert blocked["metadata"]["kandinskyModerated"] is True
        replay = bridge.portal.call(adapter.event_page, owner, token, 0)
        assert all(not event["message"]["attachments"] for event in replay["events"])
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": before})
            assert ws.receive_json()["type"] == "resumed"
            assert ws.receive_json()["events"][0]["message"] == blocked


# Создаёт три пары текста и ответа и читает отдельные страницы. Сверяет порядок, отсутствие
# повторов номеров и признак следующих страниц, чтобы граница не отделяла ответ от запроса.
def test_history_cursor_keeps_request_result_pairs(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "pages@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            for i in range(3):
                ws.send_json(
                    {
                        "type": "send",
                        "requestId": str(i),
                        "messages": [
                            {
                                "clientId": str(uuid.uuid4()),
                                "chatId": chat,
                                "text": f"Запрос {i}",
                            }
                        ],
                    }
                )
                assert ws.receive_json()["type"] == "accepted"
                assert len(ws.receive_json()["events"]) == 2
        seen = []
        before = None
        for i in range(3):
            params = {"limit": 2}
            if before is not None:
                params["before"] = before
            result = bridge.get(f"/v1/chats/{chat}/history", params=params, headers=auth).json()
            assert len(result["messages"]) == 2
            assert result["hasOlder"] == (i < 2)
            prompt, answer = result["messages"]
            assert prompt["text"] == f"Запрос {2 - i}" and answer["text"] == "В очереди"
            seen.extend([prompt["id"], answer["id"]])
            before = prompt["seq"]
        assert len(set(seen)) == 6


# Загружает файлы через обе службы и сверяет общую историю по номерам и байтам. Повтор загрузки и
# перезапуск сохраняют тот же файл, изменённые байты вызывают конфликт, другие владельцы не видят
# список и файл.
def test_recent_uploads_share_original_assets_and_preserve_owner_boundary(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "uploads@example.com")
        _, other = register(bridge, "foreign-uploads@example.com")
        login = upstream.post(
            "/v1/auth/login",
            json={"email": "uploads@example.com", "password": PASSWORD},
        )
        upstream_owner = {"Authorization": "Bearer " + login.json()["token"]}
        photo = (
            MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
        ).read_bytes()
        video = (
            MOBILE_FIXTURES / "real-media/backend-990da02cd31e46cfb8fd630c293f3489.mp4"
        ).read_bytes()

        ticket = upstream.post("/v1/uploads/ticket", headers=upstream_owner).json()["path"]
        direct_upload = upstream.post(ticket, files={"file": ("photo.jpg", photo, "image/jpeg")})
        assert direct_upload.status_code == 201
        direct_asset_id = direct_upload.json()["id"]
        endpoint = f"/v1/uploads/{uuid.uuid4()}"
        headers = {
            **owner,
            "Content-Type": "video/mp4",
            "X-Content-Sha256": hashlib.sha256(video).hexdigest(),
            "X-Kandinsky-Source": "1",
        }
        sent = bridge.put(endpoint, headers=headers, content=video)
        assert sent.status_code == 200
        native_id = sent.json()["blobId"]
        assert bridge.put(endpoint, headers=headers, content=video).json()["blobId"] == native_id
        conflict = bridge.put(
            endpoint,
            headers={
                **headers,
                "Content-Type": "image/jpeg",
                "X-Content-Sha256": hashlib.sha256(photo).hexdigest(),
            },
            content=photo,
        )
        assert conflict.status_code == 409

        incidental = bridge.put(
            f"/v1/uploads/{uuid.uuid4()}",
            content=photo,
            headers={
                **owner,
                "Content-Type": "image/jpeg",
                "X-Content-Sha256": hashlib.sha256(photo).hexdigest(),
            },
        )
        assert incidental.status_code == 200
        route = "/v1/kandinsky/uploads/recent"
        recent = bridge.get(route, headers=owner).json()["uploads"]
        assert [(row["blobId"], row["mime"]) for row in recent] == [
            (native_id, "video/mp4"),
            (uuid.UUID(direct_asset_id).hex, "image/jpeg"),
        ]
        upstream_recent = upstream.get("/v1/uploads", headers=upstream_owner).json()
        assert [uuid.UUID(row["id"]).hex for row in upstream_recent] == [
            native_id,
            uuid.UUID(direct_asset_id).hex,
        ]
        assert bridge.get(route, headers=other).json() == {"uploads": []}
        assert bridge.get(f"/v1/media/{native_id}", headers=other).status_code == 404
        assert bridge.get(f"/v1/media/{native_id}", headers=owner).content == video
        assert bridge.get(route).status_code == 401

        restarted = create_bridge(upstream.app, tmp_path / "bridge.sqlite")
        with TestClient(restarted) as second:
            assert (
                second.put(endpoint, headers=headers, content=video).json()["blobId"] == native_id
            )
            assert second.get(route, headers=owner).json()["uploads"] == recent
            assert second.get(route, headers=other).json() == {"uploads": []}


# Удаляет один из трёх результатов и проверяет сохранность соседних файлов и прежней строки. Затем
# удаляет текст из другого сеанса и сверяет пустую историю, отзыв всех файлов и события очистки
# для устройств.
def test_owner_removes_one_album_item_then_prompt_on_other_session(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "remove-album@example.com")
        _, foreign = register(bridge, "remove-foreign@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        command = {
            "type": "send",
            "requestId": "album",
            "messages": [
                {
                    "clientId": str(uuid.uuid4()),
                    "chatId": chat,
                    "text": "Три законченных результата",
                    "generationOptions": {"model": "image", "count": 3},
                }
            ],
        }
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json(command)
            assert ws.receive_json()["type"] == "accepted"
            cursor = ws.receive_json()["cursor"]
        for _ in range(3):
            assert asyncio.run(process_one(upstream.app.state.factory, upstream.app.state.settings))
        history = f"/v1/chats/{chat}/history"
        prompt, answer = bridge.get(history, headers=auth).json()["messages"]
        blobs = [item["blobId"] for item in answer["attachments"]]
        assert len(blobs) == 3
        item_path = f"/v1/kandinsky/threads/{chat}/generations/{answer['id']}"
        params = {"target": "result", "blob": blobs[1]}
        assert bridge.delete(item_path, params=params, headers=foreign).status_code == 404
        assert bridge.delete(item_path, params=params, headers=auth).status_code == 204
        assert bridge.delete(item_path, params=params, headers=auth).status_code == 204
        same_prompt, reduced = bridge.get(history, headers=auth).json()["messages"]
        assert same_prompt == prompt and reduced["id"] == answer["id"]
        assert [item["blobId"] for item in reduced["attachments"]] == [blobs[0], blobs[2]]
        assert bridge.get(f"/v1/media/{blobs[1]}", headers=auth).status_code == 404
        assert bridge.get(f"/v1/media/{blobs[0]}", headers=auth).status_code == 200

        prompt_path = f"/v1/kandinsky/threads/{chat}/generations/{prompt['id']}"
        assert (
            bridge.delete(prompt_path, params={"target": "prompt"}, headers=auth).status_code == 204
        )
        assert bridge.get(history, headers=auth).json()["messages"] == []
        for blob in blobs:
            assert bridge.get(f"/v1/media/{blob}", headers=auth).status_code == 404
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": cursor})
            assert ws.receive_json()["type"] == "resumed"
            page = ws.receive_json()["events"]
            assert all(event["kind"] == "deleted" for event in page)
            assert {event["messageId"] for event in page} == {prompt["id"], answer["id"]}
            evicted = {item["blobId"] for event in page for item in event.get("removedMedia", [])}
            assert evicted == set(blobs)
        with upstream.app.state.factory() as db:
            jobs = list(db.query(Job).all())
            assert jobs == []
            from app.Models import ErasedGeneration

            assert {row.id for row in db.query(ErasedGeneration)} == set(
                answer["metadata"]["kandinskyMemberIds"]
            )


# При повторном чтении пробного текста сохраняет его номер и убирает уже показанный отменённый
# ответ. Повтор не создаёт лишнее событие и не возвращает очищенное тело.
def test_sample_text_keeps_its_id_and_removes_a_previously_shown_cancelled_answer(tmp_path):
    from app.Services.Database.Mobile.state import State
    from app.Services.Mobile.adapter import Adapter

    state = State(tmp_path / "messages.sqlite")
    service = Adapter(None, state)
    owner, job_id, thread, operation = (str(uuid.uuid4()) for _ in range(4))
    job = dict(
        id=job_id,
        thread_id=thread,
        operation_key=operation,
        created_at=time.time(),
        status="cancelled",
        model="image",
        aspect="1:1",
        prompt="Обычный текст",
        output_id=None,
    )
    old_prompt, old_answer = asyncio.run(service.rows(owner, "", job))
    job["provider"] = "sample"
    assert asyncio.run(service.rows(owner, "", job)) == [old_prompt]
    assert asyncio.run(service.rows(owner, "", job)) == [old_prompt]
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT body FROM messages WHERE owner=? AND id=?", (owner, old_answer["id"])
            ).fetchone()[0]
            == "{}"
        )
        assert db.execute("SELECT count(*) FROM events WHERE owner=?", (owner,)).fetchone()[0] == 1


# Удаляет первый запрос при открытом соединении, несмотря на разовый отказ чтения, а второй во
# время разрыва. Оба удаления доходят, позиция чтения продвигается и основная очередь очищается.
@pytest.mark.parametrize("key", ["", "isolated-bridge-key"])
def test_socket_receives_direct_removal_and_catches_up_on_resume(tmp_path, key):
    from app.Services.Mobile.session_tokens import token_from

    failed_reads = []

    class InterruptDelivery(httpx.ASGITransport):
        fail_next_read = False

        # Один раз возвращает временный отказ для чтения списка удалений, остальные обращения
        # пропускает. Это даёт точно один сбой в момент доставки.
        async def handle_async_request(self, request):
            if self.fail_next_read and request.url.path == "/v1/jobs/native-removals":
                self.fail_next_read = False
                failed_reads.append(request.url.path)
                return httpx.Response(503)
            return await super().handle_async_request(request)

    upstream, bridge = integration_clients(
        tmp_path, erasure_key=key, transport_class=InterruptDelivery, retry_seconds=0.05
    )
    with upstream, bridge:
        _, auth = register(bridge, "removal-notification@example.com")
        source_token = token_from({"authorization": auth["Authorization"]})
        source_auth = {"Authorization": "Bearer " + source_token}
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        adapter = bridge.app.state.adapter
        owner = upstream.get("/v1/auth/me", headers=source_auth).json()["user_id"]
        thread = adapter.thread(owner, chat)
        jobs = []
        for prompt in ("Удаление при открытом чате", "Удаление без связи"):
            job = upstream.post(
                "/v1/jobs",
                headers={**source_auth, "Idempotency-Key": str(uuid.uuid4())},
                json={"thread_id": thread, "prompt": prompt},
            )
            assert job.status_code == 201
            jobs.append(job.json()["id"])
        history = bridge.get(f"/v1/chats/{chat}/history", headers=auth)
        assert history.status_code == 200

        # Просит основной сервер удалить текст конкретного задания и проверяет успех. Событие
        # должно затем пройти другой дорогой, через связующую службу.
        def remove(job_id):
            response = upstream.delete(
                f"/v1/jobs/{job_id}/contents", headers=source_auth, params={"target": "prompt"}
            )
            assert response.status_code == 204

        cursor = adapter.state.cursor(owner)
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": cursor})
            assert ws.receive_json()["type"] == "resumed"
            InterruptDelivery.fail_next_read = True
            remove(jobs[0])
            page = ws.receive_json()
            assert page["type"] == "events" and page["cursor"] > cursor
            cursor = page["cursor"]
            assert len(failed_reads) == 1
        remove(jobs[1])
        with bridge.websocket_connect("/v1/ws", headers=auth) as ws:
            ws.send_json({"type": "resume", "cursor": cursor})
            assert ws.receive_json()["type"] == "resumed"
            page = ws.receive_json()
            assert page["type"] == "events" and page["cursor"] > cursor
        assert upstream.get("/v1/jobs/native-removals", headers=source_auth).json() == {"items": []}


# Подписывается на изменения списка и переименовывает чат без запуска генерации. Получает
# уведомление и свежую карточку, а отвергнутое чужое изменение не присылает нового сигнала.
@pytest.mark.parametrize("erasure_key", ["", "directory-delivery-key"])
def test_chat_directory_changes_reach_negotiated_socket_without_generation(tmp_path, erasure_key):
    from queue import Queue
    from threading import Thread

    upstream, bridge = integration_clients(tmp_path, erasure_key=erasure_key)

    # Читает пакет в отдельном потоке с коротким сроком ожидания. Если сигнал пропадёт или чтение
    # упадёт, проверка завершится с ошибкой вместо зависания.
    def receive(socket):
        result = Queue()

        # Передаёт прочитанный пакет или исключение в очередь проверяющего потока. Так причина
        # отказа не теряется внутри фонового чтения.
        def read():
            try:
                result.put(socket.receive_json())
            except Exception as error:
                result.put(error)

        reader = Thread(target=read, daemon=True)
        reader.start()
        packet = result.get(timeout=3)
        if isinstance(packet, Exception):
            raise packet
        reader.join(timeout=1)
        return packet

    with upstream, bridge:
        _, auth = register(bridge, "directory-client@example.com")
        _, foreign = register(bridge, "directory-other@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with bridge.websocket_connect("/v1/ws", headers=auth) as socket:
            socket.send_json({"type": "resume", "cursor": 0, "features": {"chatDirectory": True}})
            assert receive(socket)["type"] == "resumed"
            # Личный подписчик начинает с восстановления пропущенных изменений.
            if not erasure_key:
                assert receive(socket) == {"type": "kandinsky.catalog.changed"}
            path = f"/v1/kandinsky/threads/{chat}"
            assert (
                bridge.patch(path, headers=auth, json={"title": "Updated directory"}).status_code
                == 200
            )
            assert receive(socket) == {"type": "kandinsky.catalog.changed"}
            recent = bridge.get("/v1/chats", headers=auth).json()["chats"]
            assert recent[0]["title"] == "Updated directory"
            assert "activityAtMs" in recent[0] and recent[0]["archived"] is False
            assert bridge.patch(path, headers=foreign, json={"title": "Foreign"}).status_code == 404
            socket.send_json({"type": "ping"})
            assert receive(socket) == {"type": "pong"}


# Архивирует закреплённый чат и проверяет, что он ушёл из общего списка, но доступен по известному
# номеру. Чужой запрос и слишком большой набор отклоняются, пустой даёт пустой ответ.
def test_known_chat_states_include_archived_rows_and_enforce_owner_and_page_limit(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "catalog-states@example.com")
        _, foreign = register(bridge, "catalog-states-other@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        path = f"/v1/kandinsky/threads/{chat}"
        assert bridge.patch(path, headers=auth, json={"pinned": True}).status_code == 200
        assert bridge.patch(path, headers=auth, json={"archived": True}).status_code == 200
        assert chat not in [
            row["id"] for row in bridge.get("/v1/chats", headers=auth).json()["chats"]
        ]
        states = bridge.post("/v1/chats/states", headers=auth, json={"ids": [chat]})
        assert states.status_code == 200
        assert states.json()["chats"][0]["id"] == chat
        assert states.json()["chats"][0]["archived"] is True
        assert states.json()["chats"][0]["pinned"] is False
        assert (
            bridge.post("/v1/chats/states", headers=foreign, json={"ids": [chat]}).status_code
            == 404
        )
        assert (
            bridge.post("/v1/chats/states", headers=auth, json={"ids": [chat] * 129}).status_code
            == 422
        )
        assert bridge.post("/v1/chats/states", headers=auth, json={"ids": []}).json() == {
            "chats": []
        }


# Прерывает передачу после первой части и проверяет промежуточное число байтов на другом
# соединении владельца. После переподключения оно доступно снова. Чужой и старый клиенты не
# получают чужой ход, а по завершении передача исчезает из списка.
def test_source_byte_progress_reaches_only_negotiated_owner_and_survives_socket_reconnect(tmp_path):
    from queue import Queue
    from threading import Thread

    # Получает следующий пакет через отдельный поток и ограничивает время ожидания. Потерянное
    # уведомление не остановит всю проверку.
    def receive(socket):
        result = Queue()
        reader = Thread(target=lambda: result.put(socket.receive_json()), daemon=True)
        reader.start()
        packet = result.get(timeout=3)
        reader.join(timeout=1)
        return packet

    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        profile, auth = register(bridge, "upload-progress@example.com")
        _, foreign = register(bridge, "upload-progress-foreign@example.com")
        payload = (
            MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
        ).read_bytes()
        operation = str(uuid.uuid4())
        with (
            bridge.websocket_connect("/v1/ws", headers=auth) as owner,
            bridge.websocket_connect("/v1/ws", headers=foreign) as other,
            bridge.websocket_connect("/v1/ws", headers=auth) as legacy,
        ):
            for socket, features in [
                (owner, {"uploadProgress": True}),
                (other, {"uploadProgress": True}),
                (legacy, {}),
            ]:
                socket.send_json({"type": "resume", "cursor": 0, "features": features})
                assert receive(socket)["type"] == "resumed"
                if features:
                    assert receive(socket) == {"type": "kandinsky.uploads", "uploads": []}
            proceed = bridge.portal.call(asyncio.Event)

            # Отправляет исходник с его размером, контрольной суммой и номером устройства. Поток
            # байтов можно приостановить, чтобы отдельно проверить состояние незавершённой
            # передачи.
            async def upload():
                # Отдаёт первые тысячу байтов, ждёт разрешения и отдаёт остаток. Между порциями
                # другая сторона может проверить точный ход.
                async def body():
                    yield payload[:1000]
                    await proceed.wait()
                    yield payload[1000:]

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=bridge.app), base_url="http://bridge.test"
                ) as client:
                    return await client.put(
                        f"/v1/uploads/{operation}",
                        headers={
                            **auth,
                            "Content-Type": "image/jpeg",
                            "Content-Length": str(len(payload)),
                            "x-content-sha256": hashlib.sha256(payload).hexdigest(),
                            "x-kandinsky-source": "1",
                            "x-kandinsky-device": profile["deviceId"],
                        },
                        content=body(),
                    )

            future = bridge.portal.start_task_soon(upload)
            try:
                packet = receive(owner)
                if packet["uploads"][0]["received"] == 0:
                    packet = receive(owner)
                expected = {
                    "id": operation,
                    "deviceId": profile["deviceId"],
                    "received": 1000,
                    "total": len(payload),
                    "mime": "image/jpeg",
                }
                assert packet == {"type": "kandinsky.uploads", "uploads": [expected]}
                for socket in (other, legacy):
                    socket.send_json({"type": "ping"})
                    assert receive(socket) == {"type": "pong"}
                with bridge.websocket_connect("/v1/ws", headers=auth) as restored:
                    restored.send_json(
                        {"type": "resume", "cursor": 0, "features": {"uploadProgress": True}}
                    )
                    assert receive(restored)["type"] == "resumed"
                    assert receive(restored) == {"type": "kandinsky.uploads", "uploads": [expected]}
            finally:
                bridge.portal.call(proceed.set)
            assert future.result(timeout=5).status_code == 200
            for _ in range(3):
                if receive(owner) == {"type": "kandinsky.uploads", "uploads": []}:
                    break
            else:
                pytest.fail("Completed upload stayed active")


# Открывает соединение без запроса сообщений о списке, меняет чат и ждёт обычный ответ проверки
# связи. Новая возможность не добавляет незапрошенных сообщений старому клиенту.
def test_existing_socket_without_directory_feature_keeps_original_protocol(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "directory-legacy@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        with bridge.websocket_connect("/v1/ws", headers=auth) as socket:
            socket.send_json({"type": "resume", "cursor": 0})
            assert socket.receive_json()["type"] == "resumed"
            assert (
                bridge.patch(
                    f"/v1/kandinsky/threads/{chat}", headers=auth, json={"title": "Legacy"}
                ).status_code
                == 200
            )
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


# Подставляет неверные длину, номер устройства и контрольную сумму. После отказа нет записи о
# готовом файле и активной передачи, иначе телефон мог бы показывать несостоявшуюся загрузку.
@pytest.mark.parametrize(
    "changed_header,value,status",
    [
        ("Content-Length", "not-a-size", 422),
        ("Content-Length", "-1", 422),
        ("Content-Length", str(20 * 1024 * 1024 + 1), 413),
        ("X-Kandinsky-Device", "not-a-device", 422),
        ("X-Content-Sha256", "0" * 64, 422),
        ("Content-Length", "mismatched", 422),
    ],
)
def test_invalid_source_upload_leaves_no_asset_or_active_progress(
    tmp_path, changed_header, value, status
):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        profile, auth = register(bridge, "invalid-source@example.com")
        photo = (
            MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
        ).read_bytes()
        headers = {
            **auth,
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(photo)),
            "X-Content-Sha256": hashlib.sha256(photo).hexdigest(),
            "X-Kandinsky-Source": "1",
            "X-Kandinsky-Device": profile["deviceId"],
        }
        headers[changed_header] = str(len(photo) + 1) if value == "mismatched" else value
        response = bridge.put(f"/v1/uploads/{uuid.uuid4()}", headers=headers, content=photo)
        assert response.status_code == status
        assert bridge.get("/v1/kandinsky/uploads/recent", headers=auth).json() == {"uploads": []}
        with bridge.websocket_connect("/v1/ws", headers=auth) as socket:
            socket.send_json({"type": "resume", "cursor": 0, "features": {"uploadProgress": True}})
            assert socket.receive_json()["type"] == "resumed"
            assert socket.receive_json() == {"type": "kandinsky.uploads", "uploads": []}


# Передаёт файл из частей без объявленного размера и превышает предел на байт. Сервер
# останавливает передачу, не оставляя вложения в списке.
def test_chunked_source_over_limit_is_rejected_without_trusting_a_declared_size(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "chunked-source@example.com")
        block = b"x" * (1024 * 1024)

        # Отдаёт двадцать одинаковых порций и последний лишний байт. Такой ввод проверяет предел
        # по реально принятым байтам.
        def chunks():
            for _ in range(20):
                yield block
            yield b"x"

        response = bridge.put(
            f"/v1/uploads/{uuid.uuid4()}",
            headers={**auth, "Content-Type": "image/jpeg", "X-Kandinsky-Source": "1"},
            content=chunks(),
        )
        assert response.status_code == 413
        assert bridge.get("/v1/kandinsky/uploads/recent", headers=auth).json() == {"uploads": []}


# Отправляет неполный запрос через открытое соединение. Ошибка указывает исходную операцию и
# запрещает повтор, но обычная проверка связи через то же соединение ещё проходит.
def test_websocket_invalid_command_reports_nonretryable_error_and_keeps_connection(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "socket-invalid-command@example.com")
        with bridge.websocket_connect("/v1/ws", headers=owner) as ws:
            ws.send_json({"type": "resume", "cursor": 0})
            assert ws.receive_json()["type"] == "resumed"
            ws.send_json({"type": "send", "requestId": "invalid-source"})
            assert ws.receive_json() == {
                "type": "error",
                "requestId": "invalid-source",
                "code": "Неверный запрос",
                "retryable": False,
            }
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
        assert bridge.get("/v1/kandinsky/threads", headers=owner).json()["threads"] == []
