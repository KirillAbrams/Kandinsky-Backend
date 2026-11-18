# Проверяет удаление исходников и уменьшенных копий с учётом связей из других чатов. Файл должен
# исчезать только после снятия последней действующей связи.

"""Загрузка телефона связывает исходник и его копию с выбранным чатом."""

import hashlib
import io
from uuid import UUID, uuid4

from PIL import Image

from app.Models import Asset, AssetDerivative, ChatSource
from tests.support.fixtures import MOBILE_FIXTURES, integration_clients
from tests.support.fixtures import register_mobile as register


# Берёт настоящее проверочное фото и при необходимости делает уменьшенную копию. Отправляет файл с
# отпечатком и нужной привязкой, чтобы тест проверил обычную регистрацию связи.
def upload(bridge, auth, *, chat=None, parent=None):
    photo = (
        MOBILE_FIXTURES / "real-media/backend-824d66cce0d64409ad4888a13792c7d4.jpg"
    ).read_bytes()
    if parent:
        output = io.BytesIO()
        with Image.open(io.BytesIO(photo)) as image:
            image.thumbnail((320, 320))
            image.convert("RGB").save(output, format="JPEG", quality=70)
        photo = output.getvalue()
    headers = {
        **auth,
        "Content-Type": "image/jpeg",
        "x-content-sha256": hashlib.sha256(photo).hexdigest(),
    }
    if chat is not None:
        headers.update({"x-kandinsky-source": "1", "x-kandinsky-chat": str(chat)})
    if parent:
        headers["x-kandinsky-parent"] = parent
    return bridge.put(f"/v1/uploads/{uuid4()}", headers=headers, content=photo)


# Загружает исходник и его копию, убеждается в наличии связей и удаляет чат. Проверяет пустой
# список недавних, отсутствие строк в базе и отсутствие файлов.
def test_phone_draft_and_preview_are_removed_with_chat(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "phone-source-erase@example.com")
        chat = bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"]
        result = upload(bridge, auth, chat=chat)
        assert result.status_code == 200, result.text
        blob = result.json()["blobId"]
        preview = upload(bridge, auth, parent=blob)
        assert preview.status_code == 200, preview.text
        child = str(UUID(preview.json()["blobId"]))
        with upstream.app.state.factory() as db:
            assert db.query(ChatSource).filter_by(asset_id=str(UUID(blob))).count() == 1
            assert db.get(AssetDerivative, child).parent_id == str(UUID(blob))
        assert bridge.delete(f"/v1/kandinsky/threads/{chat}", headers=auth).status_code == 204
        with upstream.app.state.factory() as db:
            assert db.get(Asset, str(UUID(blob))) is None
            assert db.get(Asset, child) is None
        assert bridge.get("/v1/kandinsky/uploads/recent", headers=auth).json() == {"uploads": []}
        assert not list((tmp_path / "media").iterdir())


# Связывает ранее загруженный исходник со вторым чатом. После удаления первого оба файла доступны,
# а после удаления второго исходник должен исчезнуть.
def test_recent_source_binding_preserves_other_chat_draft(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "recent-source-erase@example.com")
        chats = [
            bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"] for _ in range(2)
        ]
        blob = upload(bridge, auth, chat=chats[0]).json()["blobId"]
        child = upload(bridge, auth, parent=blob).json()["blobId"]
        bound = bridge.post(f"/v1/kandinsky/threads/{chats[1]}/sources/{blob}", headers=auth)
        assert bound.status_code == 204, bound.text
        assert bridge.delete(f"/v1/kandinsky/threads/{chats[0]}", headers=auth).status_code == 204
        assert bridge.get(f"/v1/media/{blob}", headers=auth).status_code == 200
        assert bridge.get(f"/v1/media/{child}", headers=auth).status_code == 200
        assert bridge.delete(f"/v1/kandinsky/threads/{chats[1]}", headers=auth).status_code == 204
        assert bridge.get(f"/v1/media/{blob}", headers=auth).status_code == 404


# Пробует добавить исходник с другого аккаунта и в свой уже удалённый чат. Убеждается в разных
# отказах и отсутствии любой записи файла.
def test_foreign_or_erased_chat_cannot_accept_source(tmp_path):
    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, owner = register(bridge, "source-owner@example.com")
        _, other = register(bridge, "source-foreign@example.com")
        chat = bridge.post("/v1/demo/chats", headers=owner, json={}).json()["chatId"]
        assert upload(bridge, other, chat=chat).status_code == 404
        assert bridge.delete(f"/v1/kandinsky/threads/{chat}", headers=owner).status_code == 204
        assert upload(bridge, owner, chat=chat).status_code == 410
        with upstream.app.state.factory() as db:
            assert db.query(Asset).count() == 0


# Использует уменьшенную копию как исходник в другом чате и удаляет этот запрос. Копия остаётся
# доступной, пока нужна действующему оригиналу.
def test_preview_reused_as_input_does_not_break_its_live_parent(tmp_path):
    from app.Services.Mobile.session_tokens import token_from
    from tests.feature.studio.test_full_erasure import submit

    upstream, bridge = integration_clients(tmp_path)
    with upstream, bridge:
        _, auth = register(bridge, "preview-parent@example.com")
        chats = [
            bridge.post("/v1/demo/chats", headers=auth, json={}).json()["chatId"] for _ in range(2)
        ]
        parent = upload(bridge, auth, chat=chats[0]).json()["blobId"]
        child = upload(bridge, auth, parent=parent).json()["blobId"]
        adapter = bridge.app.state.adapter
        canonical = {
            "Authorization": "Bearer " + token_from({"authorization": auth["Authorization"]})
        }
        with upstream.app.state.factory() as db:
            owner = db.query(Asset).first().user_id
        thread = adapter.thread(owner, chats[1])
        job, _, _ = submit(upstream, canonical, thread, str(UUID(child)))
        assert (
            upstream.delete(f"/v1/jobs/{job}/contents?target=prompt", headers=canonical).status_code
            == 204
        )
        assert bridge.get(f"/v1/media/{child}", headers=auth).status_code == 200
