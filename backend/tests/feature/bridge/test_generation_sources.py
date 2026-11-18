# Проверяет постановку задания с исходником без введённого текста и отказы неправильным
# настройкам. Оба сценария проходят через постоянное подключение телефона.

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.Models import AssetDerivative, ChatSource, Job
from tests.support.fixtures import (
    MOBILE_FIXTURES,
    create_bridge,
    create_studio,
    integration_clients,
    register_mobile,
)


# Отправляет исходное фото или ролик, получает описание по умолчанию и повторяет ту же команду.
# Проверяет одну строку задания, связь с чатом и исходником и, при наличии, привязку уменьшенной
# копии.
@pytest.mark.parametrize(
    ("name", "mime", "model", "prompt", "preview"),
    [
        ("photo-000.jpg", "image/jpeg", "video", "Оживить фото", "photo-001.jpg"),
        ("video-01.mp4", "video/mp4", "video_sr", "Повысить разрешение видео", None),
    ],
)
def test_source_only_generation_registers_original_and_preview_and_replays_one_job(
    tmp_path, name, mime, model, prompt, preview
):
    studio = create_studio(
        database_url=f"sqlite:///{tmp_path}/studio.sqlite",
        media_dir=tmp_path / "media",
        provider="kandinsky",
        video_sr_enabled=True,
    )
    with (
        TestClient(studio),
        TestClient(create_bridge(studio, tmp_path / "bridge.sqlite")) as bridge,
    ):
        _, headers = register_mobile(bridge, "generation-source@example.com")
        thread = uuid.uuid4()
        created = bridge.post("/v1/demo/chats", json={"id": str(thread)}, headers=headers)
        assert created.status_code == 200, created.text
        chat = created.json()["chatId"]

        # Читает проверочный файл, передаёт его с контрольной суммой и извлекает номер. Сценарий
        # дальше использует настоящий номер, полученный по обычному пути отправки.
        def upload(file_name, content_type):
            content = (MOBILE_FIXTURES / "media" / file_name).read_bytes()
            reply = bridge.put(
                f"/v1/uploads/{uuid.uuid4()}",
                headers={
                    **headers,
                    "Content-Type": content_type,
                    "X-Content-Sha256": hashlib.sha256(content).hexdigest(),
                },
                content=content,
            )
            assert reply.status_code == 200, reply.text
            return uuid.UUID(reply.json()["blobId"])

        source = upload(name, mime)
        child = upload(preview, "image/jpeg") if preview else None
        attachment = {"blobId": source.hex}
        if child:
            attachment["previewBlobId"] = child.hex
        operation = uuid.UUID(int=913)
        message = {
            "clientId": str(operation),
            "chatId": chat,
            "text": "",
            "caption": "  ",
            "attachments": [attachment],
            "generationOptions": {"model": "video_sr"} if model == "video_sr" else {},
        }
        packet = {"type": "send", "requestId": "source-only", "messages": [message]}
        with bridge.websocket_connect("/v1/ws", headers=headers) as socket:
            socket.send_json({"type": "resume", "cursor": 0})
            assert socket.receive_json()["type"] == "resumed"
            socket.send_json(packet)
            reply = socket.receive_json()
            assert reply["type"] == "accepted", reply
            assert reply["requestId"] == "source-only"
            original = reply["messages"][0]
            assert original["text"] == prompt
            assert original["clientId"] == str(operation)
            assert socket.receive_json()["cursor"] == 2
            socket.send_json(packet)
            assert socket.receive_json()["messages"] == [original]

        with studio.state.factory() as db:
            job = db.scalars(select(Job)).one()
            assert (job.model, job.prompt, job.seed, job.status) == (model, prompt, 913, "queued")
            assert (job.thread_id, job.input_id) == (str(thread), str(source))
            assert db.get(ChatSource, (str(thread), str(source))) is not None
            if child:
                assert db.get(AssetDerivative, str(child)).parent_id == str(source)
            else:
                assert db.scalars(select(AssetDerivative)).all() == []


# Посылает разные недопустимые параметры и сверяет отдельный отказ каждого. Затем проверяет, что
# канал продолжает отвечать, история пуста и работа не попала в базу.
def test_invalid_generation_settings_leave_no_jobs_and_keep_the_connection(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, headers = register_mobile(bridge, "invalid-generation-options@example.com")
        chat = bridge.post("/v1/demo/chats", json={}, headers=headers).json()["chatId"]
        source = {"blobId": uuid.uuid4().hex}
        cases = [
            ({"generationOptions": ["image"]}, "Неверные параметры генерации"),
            ({"generationOptions": {"count": True}}, "Выберите допустимое число результатов"),
            (
                {"generationOptions": {"model": "video_sr", "count": 2}},
                "Выберите допустимое число результатов",
            ),
            ({"attachments": [source, source]}, "Выберите один исходный файл"),
            ({"generationOptions": {"seed": 2**31}}, "Неверное начальное число"),
        ]
        with bridge.websocket_connect("/v1/ws", headers=headers) as socket:
            socket.send_json({"type": "resume", "cursor": 0})
            assert socket.receive_json()["type"] == "resumed"
            for index, (patch, expected) in enumerate(cases):
                message = {
                    "clientId": str(uuid.uuid4()),
                    "chatId": chat,
                    "text": "Горное озеро",
                    "attachments": [],
                    **patch,
                }
                request_id = f"invalid-{index}"
                socket.send_json({"type": "send", "requestId": request_id, "messages": [message]})
                assert socket.receive_json() == {
                    "type": "error",
                    "requestId": request_id,
                    "code": expected,
                    "retryable": False,
                }
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}
        assert bridge.get(f"/v1/chats/{chat}/history", headers=headers).json()["messages"] == []
        with upstream.app.state.factory() as db:
            assert db.scalars(select(Job)).all() == []
