# Проверяет тексты, исходники и готовые медиа перед передачей пользователю. Отдельно различает
# запрет содержимого и сбой службы, чтобы не выдавать непроверенный результат.

"""Проверка исходных данных и всех кадров готового результата через закрытую службу анализа"""

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import tempfile
from itertools import batched
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from app.Exceptions.moderation import ContentDenied, FilterUnavailable

MAX_SIDE = 896
MAX_FRAME_BYTES = 512 * 1024
MAX_FRAMES = 320
FRAME_BATCH = 4


class ContentFilter:
    # Берёт адрес, закрытый ключ и общий срок ожидания из настроек. Эти параметры одинаковы при
    # проверке входа и результата.
    def __init__(self, settings):
        self.origin = settings.moderation_origin.rstrip("/")
        self.token = settings.moderation_token.get_secret_value()
        self.timeout = settings.moderation_timeout_seconds

    # Открывает соединение с заданной службой и ограничивает ожидание отдельных частей запроса.
    # Перенаправления и настройки окружения отключены, чтобы закрытый ключ уходил по указанному
    # адресу.
    def client(self):
        return httpx.AsyncClient(
            base_url=self.origin,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(self.timeout, connect=3, write=15, pool=3),
            follow_redirects=False,
            trust_env=False,
        )

    # Сначала проверяет описание и исключения и затем проверяет приложенный исходник. Если служба
    # не успела ответить, сообщает о недоступности, а исполнитель не начинает работу без проверки.
    async def check_input(self, prompt, negative_prompt, reference, mime):
        items = [prompt]
        if negative_prompt:
            items.append(negative_prompt)
        try:
            async with self.client() as client, asyncio.timeout(self.timeout):
                await require_allowed(client, "/v1/text", items)
                if reference is not None:
                    await check_media(client, reference, mime, result=False)
        except TimeoutError as error:
            raise FilterUnavailable from error

    # Проверяет готовый файл с общим пределом ожидания. Это предотвращает выдачу того, что модель
    # создала уже после проверки входного текста.
    async def check_output(self, path, mime):
        try:
            async with self.client() as client, asyncio.timeout(self.timeout):
                await check_media(client, path, mime, result=True)
        except TimeoutError as error:
            raise FilterUnavailable from error


# Передаёт порцию текста или кадров и читает ответ с ограничением размера. Проверяет число и вид
# решений. Ошибка связи и явный запрет различаются, чтобы обработчик показал правильную причину
# отказа.
async def require_allowed(client, endpoint, items):
    try:
        async with client.stream("POST", endpoint, json={"items": items}) as reply:
            reply.raise_for_status()
            body = bytearray()
            async for part in reply.aiter_bytes(chunk_size=1024):
                body.extend(part)
                if len(body) > 4096:
                    raise ValueError("Classifier response too large")
        verdicts = json.loads(body)["allowed"]
        if not isinstance(verdicts, list) or len(verdicts) != len(items):
            raise ValueError("Incorrect verdict count")
        if any(type(verdict) is not bool for verdict in verdicts):
            raise ValueError("Invalid verdict")
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        raise FilterUnavailable from error
    if not all(verdicts):
        raise ContentDenied


# Проверяет, что фото состоит из одного кадра допустимого размера, исправляет поворот и уменьшает
# его. Служба проверки получает нормализованное фото и не тратит память на исходный размер.
def photo_bytes(path):
    with Image.open(path) as image:
        if (
            getattr(image, "n_frames", 1) != 1
            or image.width * image.height > Image.MAX_IMAGE_PIXELS
        ):
            raise ValueError("Unsupported image")
        photo = ImageOps.exif_transpose(image).convert("RGB")
        photo.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        photo.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()


# Запускает отдельную программу чтения файла с пределом времени и без ввода с клавиатуры. При
# отмене или задержке завершает процесс и ждёт его выхода, иначе тот мог бы остаться работать.
async def inspect_file(*args, stdout=subprocess.DEVNULL):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(45):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode != 0:
        raise ValueError("Unreadable video")
    return output


# Проверяет дорожки и извлекает кадры без пропусков, уменьшая размеры для проверки. Если число
# кадров выходит за предел, возвращает ошибку, чтобы ролик не получил разрешение по неполной
# выборке.
async def extract_frames(path, folder, result):
    stream_info = await inspect_file(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mov",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        str(path),
        stdout=subprocess.PIPE,
    )
    streams = [row["codec_type"] for row in json.loads(stream_info)["streams"]]
    if streams.count("video") != 1 or (result and "audio" in streams):
        raise ValueError("Unsupported video streams")
    scale = (
        f"scale=w='min({MAX_SIDE},iw)':h='min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease"
    )
    await inspect_file(
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-xerror",
        "-threads",
        "2",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mov",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-filter_threads",
        "1",
        "-vf",
        scale,
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(MAX_FRAMES + 1),
        "-threads",
        "2",
        "-q:v",
        "4",
        str(folder / "%04d.jpg"),
    )
    frames = sorted(folder.glob("*.jpg"))
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError("Unsupported video frame count")
    return frames


# Проверяет вес одного кадра и переводит байты в строку для сетевого запроса. Предел не даёт
# одному кадру превратить маленькую порцию в большой запрос.
def as_base64(data):
    if not 1 <= len(data) <= MAX_FRAME_BYTES:
        raise ValueError("Invalid moderation frame size")
    return base64.b64encode(data).decode("ascii")


# Проверяет фото одним вызовом, а ролик кадрами из временной папки. Пропускает точные копии уже
# проверенных кадров, чтобы одинаковое содержимое не занимало модель несколько раз.
async def check_media(client, path, mime, *, result):
    try:
        if mime.startswith("image/"):
            frame = await asyncio.to_thread(photo_bytes, path)
            await require_allowed(client, "/v1/images", [as_base64(frame)])
        elif mime == "video/mp4":
            with tempfile.TemporaryDirectory(prefix="studio-safety-") as scratch:
                frames = await extract_frames(path, Path(scratch), result)
                seen = set()
                for batch in batched(frames, FRAME_BATCH):
                    images = []
                    for frame in batch:
                        data = frame.read_bytes()
                        key = hashlib.sha256(data).digest()
                        if key not in seen:
                            seen.add(key)
                            images.append(as_base64(data))
                    if images:
                        await require_allowed(client, "/v1/images", images)
        else:
            raise ValueError("Unsupported moderation media type")
    except (OSError, ValueError, KeyError, TypeError, Image.DecompressionBombError) as error:
        raise FilterUnavailable from error
