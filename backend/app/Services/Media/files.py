# Проверяет загруженные фото и видео и готовит уменьшенные изображения для ленты. Имя выбирает
# сервер, а фото заново сохраняется без лишних сведений об устройстве и месте.

"""Сервер проверяет содержимое файла и выбирает имя сам
При подготовке фото удаляет данные о камере и месте съёмки"""

import base64
import io
import json
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 24_000_000


# Получает длительность, размеры и виды дорожек через отдельную программу с пределом времени.
# Плохой файл не должен заставить обработчик ждать бесконечно.
def probe_video(path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def store_upload(content: bytes, declared: str, directory: Path) -> tuple[Path, str]:
    """Проверяет файл, сохраняет его и возвращает путь и тип содержимого."""
    key = str(uuid.uuid4())
    if declared.startswith("image/"):
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise HTTPException(415, "Изображение больше 24 мегапикселей")
                image.load()
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((2048, 2048))
                output = io.BytesIO()
                image.save(output, "JPEG", quality=90)
                content = output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise HTTPException(415, "Не удалось прочитать изображение") from exc
        mime, suffix = "image/jpeg", ".jpg"
    elif declared == "video/mp4":
        mime, suffix = "video/mp4", ".mp4"
    else:
        raise HTTPException(415, "Поддерживаются изображения и MP4")
    path = directory / (key + suffix)
    path.write_bytes(content)
    if mime == "video/mp4":
        try:
            info = probe_video(path)
            streams = [s for s in info["streams"] if s["codec_type"] == "video"]
            if (
                not streams
                or float(info["format"]["duration"]) > 60
                or any(s.get("width", 0) > 4096 or s.get("height", 0) > 4096 for s in streams)
            ):
                raise ValueError("video bounds")
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(415, "Нужен MP4 до 60 секунд и 4K") from exc
    return path, mime


def display_photo_preview(path: Path) -> str | None:
    """Готовит фото для ленты. Возвращает пустой результат, если обложка превышает предел."""
    with Image.open(path) as original:
        frame = ImageOps.exif_transpose(original).convert("RGB")
        frame.thumbnail((512, 512), Image.Resampling.LANCZOS)
        for quality in (76, 60):
            output = io.BytesIO()
            frame.save(output, "JPEG", quality=quality, optimize=True)
            if output.tell() <= 64 * 1024:
                return base64.b64encode(output.getvalue()).decode("ascii")
    return None


def tiny_preview(poster: str) -> str:
    """Уменьшает обложку для первого показа в ленте."""
    with Image.open(io.BytesIO(base64.b64decode(poster, validate=True))) as original:
        frame = original.convert("RGB")
        frame.thumbnail((48, 48), Image.Resampling.LANCZOS)
        frame = frame.filter(ImageFilter.GaussianBlur(radius=1.6))
        output = io.BytesIO()
        frame.save(output, "JPEG", quality=40, optimize=True)
    if output.tell() > 2048:
        raise ValueError("Invalid tiny preview size")
    return base64.b64encode(output.getvalue()).decode("ascii")


# Читает размеры, длительность и наличие звука и добавляет обложку с маленькой копией. Размер
# обложки ограничен, чтобы подготовленные сведения не раздували ответы и хранение.
def preview_metadata(path: Path, mime: str):
    """Уменьшенный кадр сохраняется вместе с размером и длительностью файла"""
    if mime.startswith("image/"):
        with Image.open(path) as original:
            width, height = original.size
            original.thumbnail((256, 256))
            output = io.BytesIO()
            original.convert("RGB").save(output, "JPEG", quality=70)
            poster = output.getvalue()
        duration, has_audio = None, False
    else:
        info = probe_video(path)
        video = next(
            (stream for stream in info["streams"] if stream["codec_type"] == "video"), None
        )
        if video is None:
            raise ValueError("Missing video stream")
        width, height = video["width"], video["height"]
        duration = float(info["format"]["duration"])
        has_audio = any(stream["codec_type"] == "audio" for stream in info["streams"])
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=256:256:force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=15,
            check=True,
        )
        poster = result.stdout
    if not poster or len(poster) > 64 * 1024:
        raise ValueError("Invalid poster size")
    encoded = base64.b64encode(poster).decode("ascii")
    return {
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "has_audio": has_audio,
        "poster": encoded,
        "tiny": tiny_preview(encoded),
    }
