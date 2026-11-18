# Проверяет отказы плохих файлов и ограничения размера уменьшенных копий. Непригодная загрузка не
# должна оставить лишний файл, а неверная обложка не должна попасть в историю.

import base64
import io
import random
import subprocess

import pytest
from fastapi import HTTPException
from PIL import Image

from app.Services.Media import files


# Создаёт короткую звуковую дорожку без видео во временной папке. Она позволяет проверить, что
# одного подходящего расширения для принятия ролика недостаточно.
@pytest.fixture
def audio_only(tmp_path):
    path = tmp_path / "audio-only.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    return path


# Передаёт запись без изображения в загрузку и подготовку обложки. Проверяет оба отказа и пустую
# папку хранения после попытки.
def test_mp4_without_video_is_refused_without_leaving_a_file(tmp_path, audio_only):
    storage = tmp_path / "storage"
    storage.mkdir()
    with pytest.raises(HTTPException) as failure:
        files.store_upload(audio_only.read_bytes(), "video/mp4", storage)
    assert failure.value.status_code == 415
    assert list(storage.iterdir()) == []
    with pytest.raises(ValueError, match="Missing video stream"):
        files.preview_metadata(audio_only, "video/mp4")


# Сохраняет фото с разной сложностью сжатия и сравнивает обложку с ожидаемым качеством. Если
# допустимый размер не получается, функция должна вернуть отсутствие обложки.
@pytest.mark.parametrize("scale, expected_quality", [(80, 60), (256, None)])
def test_display_preview_respects_its_byte_budget(tmp_path, scale, expected_quality):
    source = tmp_path / "photo.png"
    noise = random.Random(42).randbytes(512 * 512 * 3)
    pixels = bytes(128 + ((value - 128) * scale // 256) for value in noise)
    image = Image.frombytes("RGB", (512, 512), pixels)
    image.save(source)

    poster = files.display_photo_preview(source)

    if expected_quality is None:
        assert poster is None
    else:
        expected = io.BytesIO()
        image.save(expected, "JPEG", quality=expected_quality, optimize=True)
        raw = base64.b64decode(poster, validate=True)
        assert len(raw) <= 64 * 1024
        assert raw == expected.getvalue()


# Подменяет сохранение кадра и заставляет его выйти за предел. Проверяет, что даже уменьшенное
# фото не принимается без проверки числа байтов.
def test_tiny_preview_refuses_an_encoder_result_over_its_budget(monkeypatch):
    photo = io.BytesIO()
    Image.new("RGB", (48, 48), "green").save(photo, "PNG")

    # Сверяет размер подготовленного кадра и запрошенный формат, затем пишет на один байт больше
    # предела. Ожидаем отказ в проверке размера уже подготовленного файла.
    def oversized_jpeg(image, output, format, **kwargs):
        assert image.size == (48, 48)
        assert format == "JPEG"
        output.write(b"x" * 2049)

    monkeypatch.setattr(Image.Image, "save", oversized_jpeg)
    with pytest.raises(ValueError, match="Invalid tiny preview size"):
        files.tiny_preview(base64.b64encode(photo.getvalue()).decode())


# Подменяет чтение файла так, чтобы размеры были найдены, а кадр не получился. Проверяет отказ
# именно от пустой обложки.
def test_video_preview_refuses_an_empty_first_frame(tmp_path, monkeypatch):
    path = tmp_path / "unreadable-frame.mp4"

    # Отвечает размерами и длительностью на первый вызов и пустыми байтами на извлечение. Сценарий
    # воспроизводит сбой кадра независимо от доступных средств обработки видео.
    def no_frame(command, **options):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b'{"streams":[{"codec_type":"video","width":128,"height":128}],'
                b'"format":{"duration":"1"}}',
            )
        assert command[:1] == ["ffmpeg"]
        assert str(path) in command
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    monkeypatch.setattr(files.subprocess, "run", no_frame)
    with pytest.raises(ValueError, match="Invalid poster size"):
        files.preview_metadata(path, "video/mp4")
