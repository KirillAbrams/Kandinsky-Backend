# Проверяет описание приватных файлов, ранние миниатюры и восстановление старых миниатюр. Сверка
# размера и контрольной суммы помогает отличать исходник от производного изображения.

import asyncio
import base64
import hashlib
import io
import random
import runpy

import pytest
from PIL import Image

from app.Console.Commands import preview_backfill
from app.Console.Commands.preview_backfill import backfill_previews
from app.Models import Asset
from app.Services.Generation.process_generation import process_one
from tests.support.fixtures import account, job


# Сверяет размер и контрольную сумму в описании с байтами загруженного файла. Чужому аккаунту это
# описание недоступно. Иначе телефон мог бы принять другую версию файла или раскрыть сведения о
# чужом вложении.
def test_private_metadata_binds_download_size_and_digest(client):
    h = account(client)
    image = io.BytesIO()
    Image.new("RGB", (17, 13), "#38e3b2").save(image, "PNG")
    asset = client.post(
        "/v1/assets", files={"file": ("photo.png", image.getvalue(), "image/png")}, headers=h
    ).json()
    path = f"/v1/assets/{asset['id']}"
    response = client.get(path + "/metadata", headers=h)
    assert response.status_code == 200
    meta = response.json()
    raw = client.get(path, headers=h).content
    assert meta["size"] == len(raw)
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()
    other = account(client, "other@example.com")
    assert client.get(path + "/metadata", headers=other).status_code == 404


# Проверяет, что история сразу несёт маленькое размытое изображение для фото и видео. Сверяет его
# размер, пропорции и наличие в описании файла, затем проверяет повторное заполнение для старой
# записи.
@pytest.mark.parametrize("model", ["image", "video"])
def test_history_has_blurred_preview_before_requesting_asset(client, model):
    owner = account(client)
    created = job(client, owner, model=model, aspect="5:3").json()
    assert asyncio.run(process_one(client.app.state.factory, client.app.state.settings))
    url = f"/v1/threads/{created['thread_id']}/history"
    entry = client.get(url, headers=owner).json()["messages"][0]
    assert entry["status"] == "succeeded"

    tiny = base64.b64decode(entry["output_preview"], validate=True)
    assert len(tiny) <= 2048
    with Image.open(io.BytesIO(tiny)) as frame:
        assert frame.format == "JPEG"
        assert max(frame.size) == 48
        assert abs(frame.width / frame.height - 5 / 3) < 0.08
        assert len(set(frame.get_flattened_data())) > 8
    ready = client.get(f"/v1/jobs/{created['id']}", headers=owner).json()
    assert ready["output_preview"] == entry["output_preview"]
    assert client.get(url, headers=account(client, "other@example.com")).status_code == 404

    factory = client.app.state.factory
    with factory() as db:
        asset = db.get(Asset, entry["output_id"])
        asset.preview_info = {k: v for k, v in asset.preview_info.items() if k != "tiny"}
        db.commit()
    assert client.get(url, headers=owner).json()["messages"][0]["output_preview"] is None
    assert backfill_previews(factory, client.app.state.settings.media_dir) == (1, 0)
    assert backfill_previews(factory, client.app.state.settings.media_dir) == (0, 0)
    assert (
        client.get(url, headers=owner).json()["messages"][0]["output_preview"]
        == entry["output_preview"]
    )


# Удаляет один из двух исходников и запускает заполнение миниатюр. Отсутствующий файл отмечается,
# обработка второго продолжается, а повтор даёт тот же результат. Иначе один пропуск остановил бы
# всю работу.
def test_preview_backfill_skips_missing_file_and_keeps_rebuilding_other_results(
    client, monkeypatch, capsys
):
    owner = account(client)
    factory = client.app.state.factory
    settings = client.app.state.settings
    missing = job(client, owner, key="missing-output-backfill").json()
    readable = job(client, owner, key="readable-output-backfill").json()
    for _ in range(2):
        assert asyncio.run(process_one(factory, settings))
    output_ids = [
        client.get(f"/v1/jobs/{entry['id']}", headers=owner).json()["output_id"]
        for entry in (missing, readable)
    ]
    with factory() as db:
        missing_asset, readable_asset = [db.get(Asset, key) for key in output_ids]
        (settings.media_dir / missing_asset.filename).unlink()
        missing_asset.preview_info = None
        readable_asset.preview_info = None
        db.commit()

    monkeypatch.setenv("STUDIO_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STUDIO_MEDIA_DIR", str(settings.media_dir))
    runpy.run_path(
        preview_backfill.__file__,
        run_name="__main__",
    )
    assert capsys.readouterr().out == "updated=1 skipped=1\n"
    assert backfill_previews(factory, settings.media_dir) == (0, 1)
    with factory() as db:
        assert db.get(Asset, output_ids[0]).preview_info is None
        assert db.get(Asset, output_ids[1]).preview_info["tiny"]


# Запрашивает крупную миниатюру для большого изображения. Если она не укладывается в предел,
# остаётся меньший кадр, а контрольная сумма исходника не меняется. Так запрос миниатюры не лишает
# клиента обоих вариантов.
def test_large_display_preview_keeps_the_smaller_poster_and_original_digest(client):
    owner = account(client)
    pixels = random.Random(42).randbytes(512 * 512 * 3)
    image = io.BytesIO()
    Image.frombytes("RGB", (512, 512), pixels).save(image, "PNG")
    created = client.post(
        "/v1/assets", headers=owner, files={"file": ("photo.png", image.getvalue(), "image/png")}
    )
    assert created.status_code == 201
    path = f"/v1/assets/{created.json()['id']}"
    regular = client.get(path + "/metadata", headers=owner).json()
    display = client.get(path + "/metadata", headers=owner, params={"display_preview": True}).json()
    assert display == regular
    assert display["sha256"] == hashlib.sha256(client.get(path, headers=owner).content).hexdigest()
    with Image.open(io.BytesIO(base64.b64decode(display["poster"]))) as poster:
        assert poster.size == (256, 256)
