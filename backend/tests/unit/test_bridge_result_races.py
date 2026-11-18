# Проверяет, как служба связи ведёт себя, когда результат, чат или аккаунт меняются во время
# чтения. Обновление из старого ответа не должно возвращать удалённое или отменять уже завершённое
# состояние.

import asyncio
import json
import sqlite3
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.Enums.moderation import BLOCKED_GENERATION
from app.Exceptions.accounts import ErasedAccount
from app.Http.Controllers.mobile_chats_controller import GenerationAction
from app.Http.Requests.generation import JobRequest
from app.Http.Resources.Mobile.messages import message_pair, preview_blob_id, set_result_attachments
from app.Services.Database.Mobile.state import State, moderated_answer
from app.Services.Mobile.adapter import Adapter


# Готовит известный успешный ответ с постоянными номерами, текстом и временем. Сценарии делают его
# устаревшим и сравнивают, что попало в местную историю.
@pytest.fixture
def saved_result():
    return {
        "id": str(UUID(int=10)),
        "thread_id": str(UUID(int=20)),
        "output_id": str(UUID(int=30)),
        "status": "succeeded",
        "operation_key": "original-operation",
        "prompt": "Магнолия",
        "model": "image",
        "aspect": "1:1",
        "created_at": 1700000000.0,
    }


# Теряет файл после ответа о готовом задании и перечитывает состояние. Проверяет удаление,
# блокировку, исчезнувший файл и временную ошибку, чтобы прежняя обложка не вернулась.
@pytest.mark.parametrize(
    "latest", ["erased", "moderated", "removed-output", "unchanged", "unavailable"]
)
def test_disappearing_studio_result_is_never_published_from_old_job_state(
    tmp_path, saved_result, latest
):
    owner = str(UUID(int=40))
    requests = []
    job_path = "/v1/jobs/" + saved_result["id"]
    metadata_path = "/v1/assets/" + saved_result["output_id"] + "/metadata"

    # Возвращает отсутствие сведений о файле, а затем выбранное свежее состояние задания.
    # Записывает адреса и проверяет вход, чтобы сценарий увидел повторное чтение в нужном порядке.
    def upstream(request):
        requests.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer isolated-session"
        if request.url.path == metadata_path:
            return httpx.Response(404, json={"detail": "Файл не найден"})
        assert request.url.path == job_path
        if latest == "erased":
            return httpx.Response(410, json={"detail": "Запрос удалён"})
        if latest == "unavailable":
            return httpx.Response(503, json={"detail": "Недоступно"})
        changed = dict(saved_result)
        if latest == "moderated":
            changed.update(status="failed", moderated=True, output_id=None)
        elif latest == "removed-output":
            changed.update(
                status="cancelled", output_id=None, removed_output_id=saved_result["output_id"]
            )
        return httpx.Response(200, json=changed)

    # Проводит старое задание через обычную выдачу строк и событий в отдельной базе. Тест может
    # проверить либо новые строки без файла, либо ошибку вместо сомнительного результата.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            return await adapter.rows(owner, "isolated-session", saved_result, publish=True)

    if latest in {"unchanged", "unavailable"}:
        with pytest.raises(HTTPException) as error:
            asyncio.run(load())
        assert error.value.status_code == {"unchanged": 404, "unavailable": 503}[latest]
        assert requests == [metadata_path, job_path]
    else:
        rows = asyncio.run(load())
        assert requests == [metadata_path, job_path, job_path]
        if latest == "erased":
            assert rows == []
        else:
            prompt, answer = rows
            assert prompt["text"] == saved_result["prompt"]
            assert answer["attachments"] == []
            assert answer["metadata"]["kandinskyStatus"] == (
                "failed" if latest == "moderated" else "cancelled"
            )


# Сравнивает удалённую группу и временный отказ запроса группы. Только удаление означает пустую
# выдачу, иначе ошибка могла бы выглядеть как пропажа истории.
@pytest.mark.parametrize("status", [410, 503])
def test_erased_batch_is_omitted_but_transport_failure_remains_visible(
    tmp_path, saved_result, status
):
    owner = str(UUID(int=40))
    saved_result["batch_id"] = str(UUID(int=50))

    # Сверяет, что запрос относится к общей группе, и выдаёт выбранный отказ. Одиночный адрес
    # задания не должен подменить проверку группы.
    def upstream(request):
        assert request.url.path == f"/v1/jobs/{saved_result['batch_id']}/batch"
        return httpx.Response(status, json={"detail": "Отчёт о группе недоступен"})

    # Открывает подставную сеть и читает строки группы через обычную службу связи. Возвращает
    # фактический результат или даёт исключению дойти до проверки.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            return await Adapter(client, State(tmp_path / "bridge.sqlite")).rows(
                owner, "isolated-session", saved_result
            )

    if status == 410:
        assert asyncio.run(load()) == []
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(load())
        assert error.value.status_code == 503


# Держит ответ о файле открытым и тем временем удаляет файл или владельца. После выпуска ответа
# проверяет отказ и отсутствие новой записи в памяти.
@pytest.mark.parametrize("erasure", ["result", "account"])
def test_metadata_response_cannot_reintroduce_removed_content(tmp_path, saved_result, erasure):
    owner = str(UUID(int=40))
    asset = saved_result["output_id"]
    state = State(tmp_path / "bridge.sqlite")
    started, release = asyncio.Event(), asyncio.Event()

    # Подтверждает начало чтения нужного файла и ждёт разрешения теста. Так содержимое меняется
    # между отправкой запроса и его ответом.
    async def upstream(request):
        assert request.url.path == f"/v1/assets/{asset}/metadata"
        started.set()
        await release.wait()
        return httpx.Response(200, json={"width": 640, "height": 384})

    # Запускает чтение, ждёт сигнал начала и делает выбранное удаление. Затем выпускает ответ и
    # проверяет, что старые сведения не попали в память.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            bridge = Adapter(client, state)
            request = asyncio.create_task(bridge.asset_metadata(owner, "isolated-session", asset))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                if erasure == "account":
                    await bridge.erase_owners([owner])
                else:
                    state.erase_assets(owner, [{"blobId": UUID(asset).hex}])
            finally:
                release.set()
            if erasure == "account":
                with pytest.raises(ErasedAccount):
                    await request
                assert state.is_erased(owner)
            else:
                with pytest.raises(HTTPException) as error:
                    await request
                assert error.value.status_code == 404
                assert state.media_removed(owner, UUID(asset).hex)
            assert (owner, asset) not in bridge.metadata_cache

    asyncio.run(load())


# Заполняет предел сохранённых сведений и запрашивает один файл от другого владельца. После
# удаления первого проверяет сохранность записи второго и отсутствие нового чтения по сети.
def test_metadata_reuses_recent_fetches_and_evicts_erased_owner_only(tmp_path):
    owner, other = (str(UUID(int=i)) for i in range(400, 402))
    assets = [str(UUID(int=i)) for i in range(1, 257)]
    calls = []

    # Записывает вход и адрес каждого вызова и возвращает одинаковые размеры. По записям видно,
    # что один адрес не означает общие сведения для разных владельцев.
    def upstream(request):
        calls.append((request.headers["Authorization"], request.url.path))
        return httpx.Response(200, json={"width": 640, "height": 384})

    # Создаёт записи до предела, затем удаляет одного владельца и повторно читает файл второго.
    # Сверяет вытеснение старой записи и число обращений.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            bridge = Adapter(client, State(tmp_path / "bridge.sqlite"))
            for asset in assets:
                await bridge.asset_metadata(owner, "owner-session", asset)
            await bridge.asset_metadata(other, "other-session", assets[-1])
            assert len(bridge.metadata_cache) == 256
            assert (owner, assets[0]) not in bridge.metadata_cache
            assert (owner, assets[-1]) in bridge.metadata_cache
            assert len(calls) == 257
            assert calls[-2][0] == "Bearer owner-session"
            assert calls[-1][0] == "Bearer other-session"
            assert calls[-1][1] == calls[-2][1]
            await bridge.erase_owners([owner])
            assert list(bridge.metadata_cache) == [(other, assets[-1])]
            assert await bridge.asset_metadata(other, "other-session", assets[-1]) == {
                "width": 640,
                "height": 384,
            }
            assert len(calls) == 257

    asyncio.run(load())


# Ставит в очередь несколько ответов с разными исходами. Проверяет снятие исчезнувших заданий,
# сохранение временно недоступного и остановку чтения при утрате входа.
def test_poll_retires_removed_jobs_keeps_temporary_failure_and_stops_on_expiry(
    tmp_path, saved_result
):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    jobs = [str(UUID(int=i)) for i in range(50, 56)]
    statuses = dict(zip(jobs, [404, 410, 503, 200, 401, 200], strict=True))
    calls = []
    for index, job_id in enumerate(jobs):
        state.update_pending(
            owner,
            {"id": job_id, "status": "running", "created_at": saved_result["created_at"] + index},
        )

    # Выбирает ответ по номеру задания и записывает порядок вызовов. Для удачно прочитанного
    # задания выдаёт состояние отмены, остальные возвращают заранее назначенный отказ.
    def upstream(request):
        job_id = request.url.path.rsplit("/", 1)[1]
        calls.append(job_id)
        status = statuses[job_id]
        if status == 200:
            return httpx.Response(
                200, json={**saved_result, "id": job_id, "status": "cancelled", "output_id": None}
            )
        return httpx.Response(status, json={"detail": "Проверочный ответ"})

    # Выполняет опрос, сверяет отказ входа, остаток очереди и отправленные события. Сразу
    # повторяет чтение и проверяет, что временной предел не пустил новые запросы.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            bridge = Adapter(client, state)
            with pytest.raises(HTTPException) as error:
                await bridge.poll(owner, "isolated-session")
            assert error.value.status_code == 401
            assert state.pending(owner) == [jobs[2], jobs[4], jobs[5]]
            assert calls == jobs[:5]
            events = state.events_after(owner, 0)
            assert [e["message"]["text"] for e in events] == ["Магнолия", "Генерация отменена"]
            await bridge.poll(owner, "isolated-session")
            assert calls == jobs[:5]

    asyncio.run(load())


# Занимает почти все места чтения и запускает два запроса одного файла. Второй должен дождаться
# первого и воспользоваться уже сохранённым ответом.
def test_metadata_shared_by_waiters_after_all_network_slots_are_busy(tmp_path, saved_result):
    owner = str(UUID(int=40))
    asset = saved_result["output_id"]
    path = f"/v1/assets/{asset}/metadata"

    # Удерживает три разрешения, запускает два чтения и выпускает первый ответ только после
    # появления второго. В конце освобождает разрешения, даже если проверка не прошла.
    async def load():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        # Запоминает обращение, сообщает готовность и ждёт общего сигнала. Так можно доказать, что
        # второй ожидающий не открыл собственное соединение для того же файла.
        async def upstream(request):
            calls.append(request.url.path)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"size": 83})

        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            for _ in range(3):
                await adapter.metadata_slots.acquire()
            first = asyncio.create_task(adapter.asset_metadata(owner, "token", asset))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                second = asyncio.create_task(adapter.asset_metadata(owner, "token", asset))
                await asyncio.sleep(0)
                assert not second.done()
                release.set()
                both = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
                assert both == [{"size": 83}, {"size": 83}]
                assert calls == [path]
            finally:
                release.set()
                await asyncio.gather(first, return_exceptions=True)
                for _ in range(3):
                    adapter.metadata_slots.release()

    asyncio.run(load())


# Проверяет временный отказ файла, сбой второго чтения состояния и действующий файл без обложки. В
# первых случаях история не меняется, а в последнем сохраняется только известный оригинал.
@pytest.mark.parametrize(
    "failure", ["metadata-unavailable", "second-status-unavailable", "no-poster"]
)
def test_result_update_checks_disappearing_metadata_without_exposing_a_stale_preview(
    tmp_path, saved_result, failure
):
    owner = str(UUID(int=40))
    calls = []

    # Подставляет состояние файла и задания по выбранному варианту, считая чтения. При удалении
    # файла второй запрос задания может отказать уже после первого успешного ответа.
    def upstream(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/v1/assets/"):
            if failure == "metadata-unavailable":
                return httpx.Response(503)
            if failure == "second-status-unavailable":
                return httpx.Response(404)
            return httpx.Response(
                200, json={"mime": "image/png", "size": 54, "width": 128, "height": 128}
            )
        assert request.url.path == f"/v1/jobs/{saved_result['id']}"
        if calls.count(request.url.path) == 1:
            return httpx.Response(
                200, json={**saved_result, "removed_output_id": saved_result["output_id"]}
            )
        return httpx.Response(503)

    # Читает старый ответ через службу связи и проверяет отсутствие события при сбое. Для
    # пригодного файла убеждается, что номера несуществующей копии нет в базе.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            if failure == "no-poster":
                response = await adapter.rows(owner, "token", saved_result, publish=True)
                [attachment] = response[1]["attachments"]
                assert attachment["blobId"] == UUID(saved_result["output_id"]).hex
                assert attachment["previewBlobId"] is None
                assert (
                    adapter.state.preview(owner, preview_blob_id(saved_result["output_id"])) is None
                )
            else:
                with pytest.raises(HTTPException) as rejected:
                    await adapter.rows(owner, "token", saved_result, publish=True)
                assert rejected.value.status_code == 503
                assert adapter.state.events_after(owner, 0) == []

    asyncio.run(load())
    assert len(calls) == (3 if failure == "second-status-unavailable" else 1)


# Собирает законченную группу без файлов из отменённой и ошибочной работы. Проверяет, что ответ
# сохраняет причину ошибки или блокировку вместо пустого результата.
@pytest.mark.parametrize("moderated", [True, False])
def test_finished_batch_without_outputs_keeps_the_reason_for_failure(
    tmp_path, saved_result, moderated
):
    owner = str(UUID(int=40))
    root = {
        **saved_result,
        "batch_id": saved_result["id"],
        "status": "cancelled",
        "output_id": None,
    }
    failed = {
        **root,
        "id": str(UUID(int=11)),
        "status": "failed",
        "error": "Model rejected generation",
        "moderated": moderated,
    }

    # Проверяет чтение группы по её номеру и возвращает два её задания. Тест не должен зависеть от
    # случайного состояния одного старого ответа.
    def upstream(request):
        assert request.url.path == f"/v1/jobs/{root['id']}/batch"
        return httpx.Response(200, json=[root, failed])

    # Применяет группу и сверяет признак ошибки, её объяснение и пустые вложения. Сравнение
    # отличает обычный отказ от скрытия результата после проверки.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            _, answer = await adapter.rows(owner, "token", root)
            assert answer["metadata"]["kandinskyStatus"] == "failed"
            assert answer["metadata"]["kandinskyError"] == (
                BLOCKED_GENERATION if moderated else failed["error"]
            )
            assert answer["text"] == (BLOCKED_GENERATION if moderated else "Не удалось создать")
            assert answer["attachments"] == []

    asyncio.run(load())


# Сохраняет события и удаляет владельца во время сетевой сверки чатов. Получатель не должен
# получить уже считанную порцию после удаления.
def test_events_erased_while_upstream_reads_chat_states_are_not_delivered(tmp_path, saved_result):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")

    # Подтверждает, что первоначальные события есть, и стирает владельца до возврата пустого
    # списка. Это ставит удаление между двумя чтениями.
    def upstream(request):
        assert request.url.path == "/v1/threads/states"
        assert len(state.events_after(owner, 0)) == 2
        state.erase_owners([owner])
        return httpx.Response(200, json={"threads": []})

    # Публикует исходные строки и просит порцию событий. Проверяет отсутствие выдачи, сохранённое
    # удаление владельца и пустой журнал.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, state)
            await adapter.rows(
                owner,
                "token",
                {**saved_result, "status": "queued", "output_id": None},
                publish=True,
            )
            assert await adapter.event_page(owner, "token", 0) is None
            assert state.is_erased(owner)
            assert state.events_after(owner, 0) == []

    asyncio.run(load())


# Пишет две пары с одним временем и сверяет последовательные номера, затем применяет устаревшую
# работу и блокировку. Готовый или запрещённый ответ не должен вернуться в работу.
def test_bridge_keeps_message_order_and_terminal_state_when_timestamps_match(
    tmp_path, saved_result
):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job = {**saved_result, "status": "failed", "output_id": None}
    other = {**job, "id": str(UUID(int=11)), "operation_key": "other-operation"}
    saved = []

    for item in (job, other):
        prompt, answer = message_pair(state, owner, item)
        saved.extend(state.remember_pair(owner, item["id"], prompt, answer, True))
    assert [row["seq"] for row in saved] == list(range(saved[0]["seq"], saved[0]["seq"] + 4))

    stale = {**job, "status": "running", "run_id": str(UUID(int=82))}
    assert (
        state.remember_pair(owner, job["id"], *message_pair(state, owner, stale), True) == saved[:2]
    )
    assert state.cursor(owner) == 4

    _, active = message_pair(state, owner, stale)
    assert active["metadata"]["kandinskyRunId"] == stale["run_id"]
    newer = {**job, "moderated": True}
    prompt, answer = message_pair(state, owner, newer)
    _, blocked = state.remember_pair(owner, job["id"], prompt, moderated_answer(answer), True)
    assert blocked["text"] == BLOCKED_GENERATION
    assert (
        state.remember_pair(owner, job["id"], *message_pair(state, owner, job), True)[1] == blocked
    )


# Убирает последнее вложение и повторяет отметки с файлом и без него. Запрос остаётся, ответ и
# обложка не возвращаются, а повтор не добавляет лишнее событие.
def test_deleting_last_attachment_retires_only_answer_and_defeats_late_results(
    tmp_path, saved_result
):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job, asset = saved_result["id"], saved_result["output_id"]
    prompt, answer = message_pair(state, owner, saved_result)
    chat = prompt["chatId"]
    preview = preview_blob_id(asset)
    blob = UUID(asset).hex
    set_result_attachments(
        answer, [{"blobId": blob, "previewBlobId": preview, "mime": "image/png"}]
    )
    state.preview(owner, preview, asset)
    sent = state.remember_pair(owner, job, prompt, answer, True)
    removal = {"id": "one", "member_id": job, "blob": blob, "preview": preview, "prompt": False}
    state.remove_content(owner, chat, job, prompt["id"], answer["id"], [removal], True)
    cursor = state.cursor(owner)
    state.remove_content(owner, chat, job, prompt["id"], answer["id"], [removal], True)
    empty_removal = {**removal, "id": "later", "blob": None, "preview": None}
    state.remove_content(owner, chat, job, prompt["id"], answer["id"], [empty_removal], True)
    assert state.cursor(owner) == cursor
    assert state.remember_pair(owner, job, prompt, answer, True) == sent[:1]
    assert state.preview(owner, preview) is None
    assert state.media_removed(owner, blob)
    state.remove_results([(owner, job, asset)])
    assert state.cursor(owner) == cursor
    state.erase_owners([owner])
    state.remove_results([(owner, job, asset)])
    with state.connection() as db:
        assert db.execute("SELECT count(*) FROM removed_results").fetchone() == (0,)


# Скрывает готовый результат, пока соседний ещё ожидает. Проверяет, что ответ не исчез, показывает
# продолжающуюся работу и больше не содержит удалённого вложения.
def test_bridge_keeps_pending_slot_after_moderation_removes_only_ready_one(tmp_path, saved_result):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job, asset = saved_result["id"], saved_result["output_id"]
    member = str(UUID(int=11))
    prompt, answer = message_pair(state, owner, {**saved_result, "status": "queued"})
    answer["metadata"].update(kandinskyMemberIds=[job, member], kandinskyPendingCount=1)
    blob = UUID(asset).hex
    set_result_attachments(answer, [{"blobId": blob, "mime": "image/png", "resultJobId": job}])
    state.remember_pair(owner, job, prompt, answer, True)

    state.remove_results([(owner, job, None)])
    updated = state.events_after(owner, 2)[0]["message"]
    assert updated["id"] == answer["id"]
    assert updated["attachments"] == []
    assert updated["text"] == "Генерация продолжается"
    assert updated["metadata"]["kandinskyPendingCount"] == 1


# Вызывает повторную запись одного номера внутри общей записи базы. Проверяет исходную ошибку и
# откат первой строки, чтобы ошибка не выглядела как успешное удаление.
def test_bridge_rolls_back_a_non_erasure_sql_failure(tmp_path):
    state = State(tmp_path / "bridge.sqlite")
    owner = str(UUID(int=40))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        with state.connection() as db:
            db.execute("INSERT INTO erased_accounts VALUES (?)", (owner,))
            db.execute("INSERT INTO erased_accounts VALUES (?)", (owner,))
    assert not state.is_erased(owner)


# Заполняет память опроса старыми сроками и одним действующим. При следующей проверке сохраняются
# только новый владелец и действующий срок без сетевого чтения пустой очереди.
def test_poll_discards_expired_throttles_and_keeps_active_ones(tmp_path):
    owner = str(UUID(int=2))

    # Завершает проверку при любой попытке сетевого запроса. Сброс старых сроков не должен сам по
    # себе обращаться к серверу.
    def upstream(request):
        pytest.fail(f"Unexpected network request for an idle owner: {request.url.path}")

    # Создаёт много истёкших и один действующий срок, выполняет обычный опрос и сверяет оставшиеся
    # ключи.
    async def poll():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, State(tmp_path / "bridge.sqlite"))
            adapter.next_poll.update({str(UUID(int=n)): 0 for n in range(10, 522)})
            adapter.next_poll["recent"] = float("inf")
            await adapter.poll(owner, "token")
            assert set(adapter.next_poll) == {owner, "recent"}

    asyncio.run(poll())


# Проверяет отказ повтора при смене чата или блокировке и успешный повтор увеличения видео.
# Последний отправляет допустимый формат вместо уже вычисленного соотношения кадра.
@pytest.mark.parametrize("stage", ["thread-changed", "moderated", "rerun-video-sr"])
def test_bridge_retry_obeys_latest_job_state_and_maps_video_aspect(tmp_path, saved_result, stage):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    operation = UUID(int=61)
    fields = JobRequest(
        thread_id=saved_result["thread_id"],
        prompt=saved_result["prompt"],
        model="video_sr",
        input_id=saved_result["output_id"],
    ).model_dump()
    job = {**saved_result, **fields, "aspect": "4:3", "status": "failed", "output_id": None}
    prompt, answer = message_pair(state, owner, job)
    state.remember_pair(owner, job["id"], prompt, answer, False)
    replay = {**job, "id": str(UUID(int=62)), "status": "queued", "operation_key": str(operation)}
    calls = []

    # Выдаёт новое состояние при чтении, а при допустимой отправке сверяет сохранённый номер и
    # полные параметры. Это обнаруживает команду, ушедшую после запрета или с неправильным
    # форматом.
    def upstream(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == f"/v1/jobs/{job['id']}"
            if stage == "thread-changed":
                return httpx.Response(200, json={**job, "thread_id": str(UUID(int=43))})
            return httpx.Response(200, json={**job, "moderated": stage == "moderated"})
        assert stage == "rerun-video-sr"
        assert request.url.path == "/v1/jobs"
        assert request.headers["Idempotency-Key"] == str(operation)
        assert json.loads(request.content) == fields
        return httpx.Response(201, json=replay)

    # Вызывает повтор через службу связи. Проверяет новый номер при разрешённой работе и
    # отсутствие второй команды при отказе.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, state)
            action = GenerationAction(action="retry", operation=operation)
            if stage == "rerun-video-sr":
                result = await adapter.generation_action(
                    owner, "token", prompt["chatId"], answer["id"], action
                )
                assert result["messages"][0]["clientId"] == str(operation)
                assert result["messages"][1]["metadata"]["kandinskyJobId"] == replay["id"]
                assert len(calls) == 2
            else:
                with pytest.raises(HTTPException) as error:
                    await adapter.generation_action(
                        owner, "token", prompt["chatId"], answer["id"], action
                    )
                assert error.value.status_code == (409 if stage == "moderated" else 404)
                assert len(calls) == 1

    asyncio.run(load())


# Удаляет чат до возврата ответа о принятии задания. Проверяет отказ и единственную отметку ухода
# чата, иначе опоздавший ответ мог бы вернуть строку.
def test_submit_fails_if_chat_was_erased_before_acceptance_reaches_the_bridge(
    tmp_path, saved_result
):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    thread = state.identity(owner, "thread", saved_result["thread_id"])
    operation = str(UUID(int=61))

    # Сверяет отправку и её постоянный номер, затем стирает чат и отдаёт прежнее принятие. Так
    # собранный ответ действительно поступает после удаления.
    def upstream(request):
        assert request.url.path == "/v1/jobs" and request.method == "POST"
        assert str(request.headers["Idempotency-Key"]) == operation
        state.erase_chat(owner, thread)
        return httpx.Response(201, json={**saved_result, "status": "queued", "output_id": None})

    # Отправляет сообщение обычным способом и проверяет вид отказа и список событий. Принятые
    # ранее данные не должны породить новое событие с текстом.
    async def load():
        async with httpx.AsyncClient(
            base_url="https://studio.test", transport=httpx.MockTransport(upstream)
        ) as client:
            adapter = Adapter(client, state)
            with pytest.raises(HTTPException) as error:
                await adapter.submit(
                    owner, "token", {"clientId": operation, "chatId": thread, "text": "Магнолия"}
                )
            assert error.value.status_code == 410
            assert [event["kind"] for event in state.events_after(owner, 0)] == [
                "kandinsky.retired"
            ]

    asyncio.run(load())


# Применяет удаление до первой записи истории, затем сохраняет старый ответ с файлом. По отметке
# файл должен быть отфильтрован, хотя раньше строки в базе не было.
def test_media_removal_before_history_load_also_filters_late_attachments(tmp_path, saved_result):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job = saved_result["id"]
    prompt, answer = message_pair(state, owner, saved_result)
    blob = UUID(saved_result["output_id"]).hex
    preview = preview_blob_id(saved_result["output_id"])
    set_result_attachments(
        answer, [{"blobId": blob, "previewBlobId": preview, "mime": "image/png"}]
    )
    removal = {
        "id": "untouched-slot",
        "member_id": str(UUID(int=11)),
        "blob": blob,
        "preview": preview,
        "prompt": False,
    }

    state.remove_content(owner, prompt["chatId"], job, prompt["id"], answer["id"], [removal], False)
    assert state.events_after(owner, 0) == []
    cached = state.remember_pair(owner, job, prompt, answer, True)
    assert cached[1]["attachments"] == []
    assert cached[1]["text"] == "Результат удалён"


# Записывает ответ с файлом и убирает соседнюю ячейку, у которой своего файла нет. Прежний
# результат остаётся, а число событий не растёт.
def test_removal_notice_for_outputless_slot_does_not_hide_other_output(tmp_path, saved_result):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job = saved_result["id"]
    prompt, answer = message_pair(state, owner, saved_result)
    blob = UUID(saved_result["output_id"]).hex
    set_result_attachments(answer, [{"blobId": blob, "mime": "image/png"}])
    kept = state.remember_pair(owner, job, prompt, answer, True)
    notice = {
        "id": "failed-slot",
        "member_id": str(UUID(int=11)),
        "blob": None,
        "preview": None,
        "prompt": False,
    }
    state.remove_content(owner, prompt["chatId"], job, prompt["id"], answer["id"], [notice], False)
    assert state.remember_pair(owner, job, prompt, answer, True) == kept
    assert state.cursor(owner) == 2


# Ставит старую отметку удаления поверх сохранённого ответа и получает удаление файла. Файл
# отмечается, а ответ не возвращается даже при наличии прежнего тела в базе.
def test_partial_removal_defers_to_legacy_answer_tombstone_even_with_cached_body(
    tmp_path, saved_result
):
    owner = str(UUID(int=40))
    state = State(tmp_path / "bridge.sqlite")
    job = saved_result["id"]
    prompt, answer = message_pair(state, owner, saved_result)
    blob = UUID(saved_result["output_id"]).hex
    set_result_attachments(answer, [{"blobId": blob, "mime": "image/png"}])
    state.remember_pair(owner, job, prompt, answer, True)
    with state.connection() as db:
        # Имитирует сохранённую старым клиентом метку, добавленную до очистки кэша сообщений.
        db.execute("INSERT INTO deleted_messages VALUES (?,?)", (owner, answer["id"]))
    notice = {"id": "result", "member_id": job, "blob": blob, "preview": None, "prompt": False}
    state.remove_content(owner, prompt["chatId"], job, prompt["id"], answer["id"], [notice], False)
    assert state.media_removed(owner, blob)
    assert state.cursor(owner) == 2
    assert state.remember_pair(owner, job, prompt, answer, True)[0]["id"] == prompt["id"]
