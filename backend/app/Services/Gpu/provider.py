# Задаёт общий способ получить файл от исполнителя. Один вариант нужен для местных проверок,
# другой передаёт работу видеокарте и умеет доставлять реальные удаления.

"""Общий способ получить файл результата для разных обработчиков
Обработчик получает путь, записывает туда файл и сообщает его тип"""

import asyncio
import base64
import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from app.Exceptions.generation import GpuUnavailable
from app.Models import Job


class DemoProvider:
    """Проверочный обработчик рисует картинку или короткий ролик по простым правилам"""

    async def generate(self, job: Job, output: Path, reference: Path | None) -> str:
        """Создаёт локальный проверочный файл и возвращает его тип."""
        await asyncio.sleep(1)
        await asyncio.to_thread(self.render, job, output)
        return "video/mp4" if job.model == "video" else "image/png"

    # Подбирает размеры, рисует картинку по тексту и начальному числу и при необходимости
    # превращает её в короткое видео. Убирает временный кадр даже после сбоя, чтобы проверочные
    # запуски не оставляли файлы.
    @staticmethod
    def render(job: Job, output: Path) -> None:
        """Рисует проверочное изображение или короткий ролик и убирает временный кадр."""
        from app.Services.Generation.model_catalog import capabilities

        options = capabilities(job.model, bool(job.input_id))
        sizes = next(row["sizes"] for row in options["resolutions"] if row["id"] == job.resolution)
        w, h = sizes[job.aspect]
        image = Image.new("RGB", (w, h), "#F3F5F1")
        draw = ImageDraw.Draw(image)
        seed = hashlib.sha256(f"{job.prompt}:{job.seed}".encode()).digest()
        for i in range(8):
            x, y = seed[i] / 255 * w, seed[i + 8] / 255 * h
            radius = 30 + seed[i + 16] / 2
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=["#47DDAE", "#17251E", "#C3DBCC"][i % 3],
            )
        if job.model == "image":
            image.save(output, "PNG")
        else:
            still = output.with_suffix(".source.png")
            image.save(still)
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-loop",
                        "1",
                        "-i",
                        str(still),
                        "-t",
                        str(job.duration_seconds),
                        "-r",
                        "24",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(output),
                    ],
                    check=True,
                    timeout=45,
                )
            finally:
                still.unlink(missing_ok=True)

    async def cancel(self, job_id: str) -> None:
        """Поддерживает общую команду остановки для локального режима."""
        return None


# Отличает временный отказ вычислительной службы от других ошибок ответа. Обработчик может вернуть
# задание в очередь при недоступности и не списывать попытку как окончательный сбой.
def require_gpu(response: httpx.Response) -> None:
    if response.status_code in {404, 408, 429} or response.status_code >= 500:
        raise GpuUnavailable(f"GPU HTTP {response.status_code}")
    response.raise_for_status()


class GpuProvider:
    """Служба обработки запоминает номер и отпечаток запроса до запуска модели
    Повтор с тем же номером возвращает прежнее задание
    """

    def __init__(self, url: str, token: str) -> None:
        """Готовит адрес и заголовок доступа к вычислительной машине."""
        if not token:
            raise ValueError("STUDIO_GPU_TOKEN required for GPU mode")
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def generate(
        self,
        job: Job,
        output: Path,
        reference: Path | None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Ждёт удалённую генерацию, передаёт рост процента и скачивает файл в пределах размера."""
        body = {
            "canonical_job_id": job.id,
            "prompt": job.prompt,
            "model": job.model,
            "aspect": "1:1" if job.model == "video_sr" else job.aspect,
            "seed": job.seed,
            "quality": job.quality,
            "resolution": job.resolution,
            "duration_seconds": job.duration_seconds,
            "negative_prompt": job.negative_prompt,
            "guidance_scale": job.guidance_scale,
        }
        encoded = await asyncio.to_thread(reference.read_bytes) if reference else None
        body["image"] = (
            base64.b64encode(encoded).decode() if encoded and job.model != "video_sr" else None
        )
        if job.model == "video_sr" and encoded is not None:
            body["video"] = base64.b64encode(encoded).decode()
        operation = job.run_id or job.id
        async with httpx.AsyncClient(
            base_url=self.url, headers=self.headers, timeout=30, follow_redirects=False
        ) as client:
            response = await client.post(f"/jobs/{operation}", json=body)
            require_gpu(response)
            progress = -1
            for _ in range(900):
                state = await client.get(f"/jobs/{operation}")
                require_gpu(state)
                payload = state.json()
                status = payload["status"]
                measured = payload.get("progress")
                if status == "running" and type(measured) is int and progress < measured < 100:
                    progress = measured
                    if on_progress is not None:
                        on_progress(progress)
                if status == "succeeded":
                    async with client.stream("GET", f"/jobs/{operation}/result") as result:
                        require_gpu(result)
                        size = 0
                        with output.open("wb") as file:
                            async for chunk in result.aiter_bytes():
                                size += len(chunk)
                                if size > 256 * 1024 * 1024:
                                    raise ValueError("GPU result too large")
                                file.write(chunk)
                    return "image/png" if job.model == "image" else "video/mp4"
                if status in ("failed", "cancelled"):
                    raise RuntimeError("GPU generation failed or was cancelled")
                await asyncio.sleep(2)
        raise TimeoutError("GPU generation timeout")

    async def cancel(self, job_id: str) -> None:
        """Просит вычислительную машину остановить указанную работу."""
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{self.url}/jobs/{job_id}/cancel", headers=self.headers)

    async def erase(self, job_id: str, run_id: str | None = None) -> None:
        """Стирает данные работы на вычислительной машине и требует подтверждение."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.delete(
                f"{self.url}/jobs/{job_id}",
                headers=self.headers,
                params={"run_id": run_id} if run_id else None,
            )
            response.raise_for_status()
            if response.status_code != 204:
                raise GpuUnavailable("GPU did not confirm erasure")
