# Проверяет доступные настройки генерации, ограничения исходников и описание файлов. Сверяет то,
# что получает клиент, с сохранёнными параметрами и размерами.

"""Проверяем параметры в ответе сервера и отдельно в записи базы"""

import base64
import io

from PIL import Image
from sqlalchemy import select

from app.Models import Asset, Job, User
from tests.support.fixtures import account, job


# Запрашивает повышение разрешения при выключенной модели и ждёт отказа. Иначе сервер мог бы
# принять задание, которое обработчик не сможет выполнить.
def test_video_sr_unavailable_without_configured_checkpoint(client):
    headers = account(client)
    assert job(client, headers, model="video_sr", input_id="missing").status_code == 503


# Включает улучшение видео и готовит два ролика разной длины. Проверяет удвоение в списке
# возможностей, пропорции короткого ролика и отказ для чужого или слишком длинного исходника.
def test_video_sr_accepts_owned_short_mp4_and_derives_display_aspect(client):
    client.app.state.settings.provider = "kandinsky"
    client.app.state.settings.video_sr_enabled = True
    headers = account(client)
    other = account(client, "other@example.com")
    with client.app.state.factory() as db:
        owner = db.scalar(select(User.id).where(User.email == "one@example.com"))
        clip = Asset(
            user_id=owner,
            mime="video/mp4",
            size=4096,
            filename="clip.mp4",
            preview_info={"width": 320, "height": 192, "duration_seconds": 3.0, "has_audio": True},
        )
        too_long = Asset(
            user_id=owner,
            mime="video/mp4",
            size=4096,
            filename="long.mp4",
            preview_info={"width": 320, "height": 192, "duration_seconds": 8.0},
        )
        db.add_all([clip, too_long])
        db.flush()
        source_id, long_id = clip.id, too_long.id
        db.commit()
    models = client.get("/v1/models", headers=headers).json()
    assert models[-1]["id"] == "video_sr"
    assert models[-1]["capabilities"]["scale"] == 2
    response = job(client, headers, model="video_sr", input_id=source_id)
    assert response.status_code == 201
    assert response.json()["aspect"] == "5:3"
    assert response.json()["input_id"] == source_id
    assert job(client, other, model="video_sr", input_id=source_id).status_code == 404
    assert (
        job(
            client, headers, key="clip-too-long-0000001", model="video_sr", input_id=long_id
        ).status_code
        == 422
    )


# Отправляет запрос с качеством, размером и дополнительными настройками. Проверяет их в ответе и в
# базе, повторяет запрос и сверяет номер. Изменение размера с тем же номером должно вызвать
# конфликт.
def test_generation_options_survive_submit_and_idempotent_retry(client):
    headers = account(client)
    thread = client.post("/v1/threads", headers=headers).json()["id"]
    headers["Idempotency-Key"] = "generation-options-key-0001"
    body = dict(
        thread_id=thread,
        prompt="Магнолия",
        model="image",
        aspect="1:1",
        seed=42,
        quality="draft",
        resolution="compact",
        negative_prompt="text",
        guidance_scale=4.0,
    )
    response = client.post("/v1/jobs", headers=headers, json=body)
    assert response.status_code == 201, response.text
    payload = response.json()
    replay = client.post("/v1/jobs", headers=headers, json=body)
    assert replay.status_code in (200, 201)
    assert replay.json()["id"] == payload["id"]
    changed = client.post("/v1/jobs", headers=headers, json={**body, "resolution": "standard"})
    assert changed.status_code == 409
    assert (
        payload["quality"],
        payload["resolution"],
        payload["negative_prompt"],
        payload["guidance_scale"],
    ) == ("draft", "compact", "text", 4.0)
    with client.app.state.factory() as db:
        stored = db.scalar(select(Job).where(Job.id == payload["id"]))
        assert stored.quality == "draft"
        assert stored.resolution == "compact"


# Сверяет размеры фотографий и доступную длительность роликов в списке моделей. Проверяет, что три
# секунды принимаются, а шесть отклоняются. Клиенту нужны те же границы, что и серверу.
def test_video_controls_and_model_capability_catalog(client):
    headers = account(client)
    models = client.get("/v1/models", headers=headers).json()
    assert "capabilities" in models[0]
    image_sizes = models[0]["capabilities"]["resolutions"]
    assert [(row["id"], row["sizes"]) for row in image_sizes] == [
        ("standard", {"1:1": [1024, 1024], "5:3": [1280, 768], "3:5": [768, 1280]})
    ]
    assert models[1]["capabilities"]["duration_seconds"] == [1, 3, 5]
    response = job(client, headers, model="video", duration_seconds=3, resolution="compact")
    assert response.status_code == 201, response.text
    assert response.json()["duration_seconds"] == 3
    invalid = job(
        client, headers, key="another-operation-key-001", model="video", duration_seconds=6
    )
    assert invalid.status_code == 422


# Загружает изображение известного размера и открывает оба варианта миниатюры. Сверяет пропорции,
# предел размера и признаки исходного файла. Иначе увеличение миниатюры могло бы изменить
# ожидаемую контрольную сумму скачивания.
def test_metadata_contains_real_poster_and_dimensions(client):
    headers = account(client)
    stream = io.BytesIO()
    Image.new("RGB", (600, 400), "#1d9f71").save(stream, "PNG")
    uploaded = client.post(
        "/v1/assets", headers=headers, files={"file": ("photo.png", stream.getvalue(), "image/png")}
    )
    assert uploaded.status_code == 201
    data = client.get(f"/v1/assets/{uploaded.json()['id']}/metadata", headers=headers).json()
    assert (data["width"], data["height"], data["has_audio"]) == (600, 400, False)
    poster = Image.open(io.BytesIO(base64.b64decode(data["poster"])))
    assert poster.format == "JPEG" and max(poster.size) <= 256
    path = f"/v1/assets/{uploaded.json()['id']}/metadata"
    display = client.get(path, headers=headers, params={"display_preview": True}).json()
    preview_bytes = base64.b64decode(display["poster"])
    preview = Image.open(io.BytesIO(preview_bytes))
    assert preview.format == "JPEG" and preview.size == (512, 341)
    assert len(preview_bytes) <= 64 * 1024
    assert (display["size"], display["sha256"]) == (data["size"], data["sha256"])


# Передаёт фотографию в улучшение видео и в чистую генерацию изображения. Обе неподходящие
# операции должны получить отказ до записи задания.
def test_video_upscaling_rejects_photo_and_video_creation_requires_a_photo(client):
    settings = client.app.state.settings
    settings.provider = "kandinsky"
    settings.video_sr_enabled = True
    headers = account(client)
    stream = io.BytesIO()
    Image.new("RGB", (30, 40), "green").save(stream, "PNG")
    image = client.post(
        "/v1/assets", headers=headers, files={"file": ("photo.png", stream.getvalue(), "image/png")}
    ).json()["id"]
    rejected = job(client, headers, model="video_sr", input_id=image)
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Для повышения разрешения нужен MP4"
    rejected = job(client, headers, model="image", input_id=image)
    assert rejected.status_code == 422
    with client.app.state.factory() as db:
        assert list(db.scalars(select(Job))) == []
