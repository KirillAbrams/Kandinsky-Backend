# Исполнитель читает одно задание из закрытой папки и сохраняет готовое фото или видео. Отдельный
# процесс позволяет системе вернуть память видеокарты после выхода и не раскрывает запрос в
# командной строке.

"""Для каждого задания запускается отдельный процесс

После выхода процесса система освобождает память видеокарты
Запрос лежит в закрытой папке, а описание и фото не передаются в командной строке
При ошибке модели задание завершается ошибкой
"""

import argparse
import base64
import io
import json
import os
from pathlib import Path

# При запуске файла напрямую выбираем тот же пакет, что при запуске через -m.
# На вычислительной машине содержимое runner монтируется как пакет gpu.
if not __package__:
    __package__ = {
        "Runner": "app.Services.Gpu.Runner",
        "gpu": "gpu",
    }[Path(__file__).resolve().parent.name]


# Проверяет размер, выданный моделью, и при необходимости уменьшает изображение до запрошенного.
# Так ошибочные размеры не уйдут как успешный результат.
def save_image(image, output, spec):
    from PIL import Image

    if image.size != (spec["render_width"], spec["render_height"]):
        raise ValueError("Kandinsky returned an unexpected native image size")
    size = spec["width"], spec["height"]
    if image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    image.save(output, format="PNG")


# Сначала записывает временный файл и затем заменяет общий, чтобы читатель не получил частично записанное
# число.
# Переводит выполненные шаги в целый процент до девяноста девяти и заменяет файл через временную
# запись. Читатель не увидит часть числа, а готовность определит по состоянию законченного
# задания.
def write_progress(output, completed, total):
    if total <= 0:
        return
    progress = min(99, 100 * completed // total)
    temporary = output.parent / "progress.tmp"
    try:
        temporary.write_text(str(progress))
        temporary.replace(output.parent / "progress")
    except OSError:
        # Неудачная запись процента не мешает сохранить готовый результат.
        pass


# Выбирает обработку по виду задания, задаёт размеры, число шагов и начальное число, затем
# сохраняет файл. Переносит части модели на видеокарту по очереди, чтобы ограничить расход памяти.
def run(body, output):
    if body["model"] == "video_sr":
        from .video_sr import upscale

        upscale(body, output, lambda completed, total: write_progress(output, completed, total))
        return

    import torch
    from diffusers import (
        Kandinsky5I2VPipeline,
        Kandinsky5T2IPipeline,
        Kandinsky5T2VPipeline,
    )
    from diffusers.utils import export_to_video
    from PIL import Image

    from .models import generation_spec

    spec = generation_spec(body)
    classes = {
        "image": Kandinsky5T2IPipeline,
        "video": Kandinsky5T2VPipeline,
        "image_video": Kandinsky5I2VPipeline,
    }
    path = Path(os.environ.get("KANDINSKY_MODELS", "/models")) / spec["mode"]
    pipe = classes[spec["mode"]].from_pretrained(
        str(path), torch_dtype=torch.bfloat16, local_files_only=True, use_safetensors=True
    )
    # Переносим части модели на видеокарту по очереди, чтобы они поместились в память.
    pipe.enable_model_cpu_offload()
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    args = {
        "prompt": body["prompt"],
        "width": spec["render_width"],
        "height": spec["render_height"],
        "num_inference_steps": spec["steps"],
        "guidance_scale": spec["guidance"],
        "generator": torch.Generator(device="cuda").manual_seed(body["seed"]),
    }
    if spec["mode"] != "image":
        args["num_frames"] = spec["num_frames"]
    if body.get("negative_prompt"):
        args["negative_prompt"] = body["negative_prompt"]
    if spec["mode"] == "image_video":
        with Image.open(io.BytesIO(base64.b64decode(body["image"], validate=True))) as image:
            args["image"] = image.convert("RGB")

    # Записывает число завершённых шагов после вызова модели и возвращает ей её параметры. Без
    # этой отметки телефон видел бы только состояние работы до появления файла.
    def step_completed(pipeline, index, timestep, callback_kwargs):
        write_progress(output, index + 1, pipeline.num_timesteps)
        return callback_kwargs

    write_progress(output, 0, spec["steps"])
    with torch.inference_mode():
        result = pipe(
            **args,
            callback_on_step_end=step_completed,
            callback_on_step_end_tensor_inputs=[],
        )
    if spec["mode"] == "image":
        save_image(result.image[0], output, spec)
    else:
        # В файл пишем 24 кадра на секунду, чтобы выдержать выбранную длительность.
        export_to_video(result.frames[0][: spec["output_frames"]], str(output), fps=24, quality=8)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    run(json.loads(options.request.read_text()), options.output)
