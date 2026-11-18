# Увеличивает разрешение короткого видео в отдельном окружении модели. Проверяет исходник заранее,
# подгоняет число кадров для модели и затем убирает добавленные кадры из результата.

"""Повышение разрешения через официальную модель в отдельном окружении"""

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path


# Читает размеры и длительность видео с ограничением времени. Отклоняет неподходящий исходник до
# загрузки модели, чтобы повреждённый или слишком большой файл не занял видеокарту.
def source_geometry(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    info = json.loads(result.stdout)
    video = info["streams"][0]
    width, height = video["width"], video["height"]
    duration = float(info["format"]["duration"])
    if not (
        1 <= duration <= 5.1
        and 128 <= min(width, height)
        and max(width, height) <= 960
        and width * height <= 960 * 540
    ):
        raise ValueError("Invalid MP4 for Kandinsky Video SR")
    return width, height


class DenoiseProgress:
    # Запоминает функцию передачи хода работы и задаёт нулевые счётчики. Один экземпляр относится
    # к одной задаче, поэтому проценты от другого видео сюда не попадут.
    def __init__(self, report):
        self.report = report
        self.completed = 0
        self.total = 0

    # Умножает число частей ролика на число шагов обработки каждой и сообщает начальное значение.
    # Без общего числа шагов нельзя получить осмысленный процент.
    def start(self, tiles, steps_per_tile):
        self.total = tiles * steps_per_tile
        self.completed = 0
        self.report(0, self.total)

    # Прибавляет завершённые шаги и сообщает новое значение вместе с общим числом. Телефон видит
    # ход обработки до записи готового ролика.
    def update(self, steps=1):
        self.completed += steps
        self.report(self.completed, self.total)

    # Собственных файлов или соединений у этого счётчика нет, поэтому освобождать здесь нечего.
    # Завершает работу счётчика по вызову модели. Он хранит только числа и функцию уведомления,
    # поэтому файлы или соединения закрывать здесь не требуется.
    def close(self):
        pass


# Временно сохраняет исходник, проверяет его и готовит нужное число кадров. Запускает увеличение
# вдвое, обрезает добавленные кадры, сверяет итоговые размеры и только затем пишет ролик.
def upscale(body, output, report=None):
    import torch
    from kandinsky_sr.core.algo.mux import mux_video_audio
    from kandinsky_sr.pipeline.config import SRConfig
    from kandinsky_sr.pipeline.factory import load_sr_pipeline
    from kandinsky_sr.pipeline.video_io import (
        read_video_tchw_uint8,
        resample_to_target_fps,
    )

    root = Path(os.environ.get("KANDINSKY_MODELS", "/models"))
    config = SRConfig(
        checkpoint_path=str(root / "video_sr"),
        resolution_scale=2,
        num_steps=2,
        device="cuda:0",
    )
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        source = Path(directory) / "source.mp4"
        source.write_bytes(base64.b64decode(body["video"], validate=True))
        width, height = source_geometry(source)
        frames, fps = read_video_tchw_uint8(source)
        frames, fps = resample_to_target_fps(frames, fps)
        count = min(frames.shape[0], 121)
        frames = frames[:count]
        # Модель принимает 1, 9, 17 и далее по восемь кадров. Повторяем последний кадр до нужной длины.
        aligned = 1 + 8 * ((count - 1 + 7) // 8)
        if aligned > count:
            frames = torch.cat([frames, frames[-1:].expand(aligned - count, -1, -1, -1)])
        pipe = load_sr_pipeline(config, "cuda:0", force=True, resolution_scale=2)
        with torch.inference_mode():
            # У результата другая последовательность осей. Обрезаем по времени, чтобы убрать только
            # добавленные кадры.
            progress = {"progress": DenoiseProgress(report)} if report is not None else {}
            result = pipe(video=frames, seed=body["seed"], show_progress=False, **progress).frames[
                0, :, :count
            ]
        expected = (3, count, height * 2, width * 2)
        if result.shape != expected:
            raise ValueError(
                f"Kandinsky Video SR returned {tuple(result.shape)}, expected {expected}"
            )
        mux_video_audio(result, None, output, fps=fps)
