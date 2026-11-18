# Проверяет запись процентов, запуск нужной модели и размеры результата. Подставные модели
# проверяют параметры до вычисления, поэтому видеокарта не нужна.

import base64
import io
import json
import runpy
import shutil
import sys
from contextlib import nullcontext
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.Services.Gpu.Runner import infer, video_sr


# Проверяет запись при неизвестном объёме, успешное обновление и сохранение прежнего значения при
# сбое. Последний процент не должен означать готовность файла до завершения работы.
def test_progress_is_bounded_atomic_and_survives_a_failed_update(tmp_path):
    output = tmp_path / "result.mp4"
    progress = tmp_path / "progress"
    temporary = tmp_path / "progress.tmp"
    infer.write_progress(output, 1, 0)
    assert not progress.exists()

    infer.write_progress(output, 1, 3)
    assert progress.read_text() == "33"
    assert not temporary.exists()

    temporary.mkdir()
    infer.write_progress(output, 2, 3)
    assert progress.read_text() == "33"
    temporary.rmdir()

    infer.write_progress(output, 3, 3)
    assert progress.read_text() == "99"
    assert not temporary.exists()


# Запускает вход обработчика с временными путями и подставным увеличением. Сверяет разбор задания,
# путь результата и запись процентов.
@pytest.mark.parametrize("deployed_layout", [False, True], ids=["source-tree", "gpu-mount"])
def test_super_resolution_cli_passes_request_and_reports_progress(
    tmp_path, monkeypatch, deployed_layout
):
    request = tmp_path / "request.json"
    output = tmp_path / "result.mp4"
    body = {"model": "video_sr", "video": "YWJj", "seed": 12}
    request.write_text(json.dumps(body))

    # Проверяет полученный запрос и путь, сообщает известную долю выполнения и пишет проверочные
    # байты. Так тест может проверить проводку команды и обратного вызова отдельно от модели.
    def upscale(parsed, target, report):
        assert parsed == body
        assert target == output
        report(2, 5)
        assert (tmp_path / "progress").read_text() == "40"
        target.write_bytes(b"engine result")

    script = Path(infer.__file__)
    engine = video_sr
    if deployed_layout:
        runner_mount = tmp_path / "gpu"
        shutil.copytree(script.parent, runner_mount, ignore=shutil.ignore_patterns("__pycache__"))
        monkeypatch.syspath_prepend(str(tmp_path))
        engine = import_module("gpu.video_sr")
        script = runner_mount / script.name

    monkeypatch.setattr(engine, "upscale", upscale)
    monkeypatch.setattr(sys, "argv", ["infer", "--request", str(request), "--output", str(output)])
    runpy.run_path(str(script), run_name="__main__")
    assert output.read_bytes() == b"engine result"


# Проверяет три режима с разными размерами, шагами и кадрами. Сверяет также порядок загрузки
# модели, разгрузки памяти и записи результата.
@pytest.mark.parametrize(
    ("model", "mode", "render_size", "output_size", "steps", "guidance", "frames"),
    [
        ("image", "image", (1280, 768), (960, 576), 28, 4.5, None),
        ("video", "video", (640, 384), None, 16, 1.0, 24),
        ("video", "image_video", (640, 384), None, 30, 4.5, 72),
    ],
)
def test_inference_passes_job_options_to_the_correct_model_and_preserves_output_shape(
    tmp_path, monkeypatch, model, mode, render_size, output_size, steps, guidance, frames
):
    body = {
        "model": model,
        "aspect": "5:3",
        "resolution": "compact",
        "prompt": "Дом у озера",
        "seed": 41,
    }
    if mode != "video":
        body.update(quality="draft", guidance_scale=4.5, negative_prompt="Туман")
    if mode == "image_video":
        reference = io.BytesIO()
        Image.new("RGBA", (2, 3), (10, 20, 30, 255)).save(reference, format="PNG")
        body["image"] = base64.b64encode(reference.getvalue()).decode()
    if model == "video":
        body["duration_seconds"] = 1 if mode == "video" else 3

    output = tmp_path / ("result.png" if model == "image" else "result.mp4")
    progress = tmp_path / "progress"
    calls = []

    # Проверяет выбор устройства и сохраняет исходное число в простой отметке. По ней видно, дошло
    # ли число из запроса до модели.
    def generator(*, device):
        assert device == "cuda"
        return SimpleNamespace(manual_seed=lambda seed: ("seed", seed))

    class Pipeline:
        num_timesteps = steps
        vae = (
            object()
            if mode == "video"
            else SimpleNamespace(enable_tiling=lambda: calls.append("tiles"))
        )

        # Отмечает включение освобождения памяти видеокарты перед обработкой. Пропуск настройки
        # или неверный порядок заметен по списку вызовов.
        def enable_model_cpu_offload(self):
            calls.append("offload")

        # Сверяет размер, шаги, текст и другие параметры модели, проверяет обратный вызов
        # процентов и отдаёт изображение либо кадры нужного вида.
        def __call__(self, *, callback_on_step_end, callback_on_step_end_tensor_inputs, **args):
            calls.append("infer")
            assert progress.read_text() == "0"
            assert (args["width"], args["height"]) == render_size
            assert args["num_inference_steps"] == steps
            assert args["prompt"] == "Дом у озера" and args["generator"] == ("seed", 41)
            assert args["guidance_scale"] == guidance
            assert args.get("negative_prompt") == (None if mode == "video" else "Туман")
            assert callback_on_step_end_tensor_inputs == []
            if mode == "image_video":
                assert args["image"].mode == "RGB" and args["image"].size == (2, 3)
                assert args["image"].getpixel((0, 0)) == (10, 20, 30)
            else:
                assert "image" not in args
            assert callback_on_step_end(self, steps - 1, None, {}) == {}
            assert progress.read_text() == "99"
            if frames is None:
                assert "num_frames" not in args
                return SimpleNamespace(image=[Image.new("RGB", render_size)])
            assert args["num_frames"] == frames + 1
            return SimpleNamespace(frames=[list(range(frames + 1))])

    # Создаёт отдельный загрузчик для каждого режима. Это позволяет обнаружить случай, когда
    # оживление фото случайно попадает в модель создания по тексту.
    def factory(pipeline_mode):
        # Проверяет местный путь модели и способ загрузки, затем отдаёт подставную модель. Без
        # этой проверки режим мог бы подтянуть другие веса или выйти в сеть.
        def load(location, **options):
            assert pipeline_mode == mode
            assert Path(location) == tmp_path / "models" / mode
            assert options == {
                "torch_dtype": "bfloat16",
                "local_files_only": True,
                "use_safetensors": True,
            }
            calls.append("load")
            return Pipeline()

        return SimpleNamespace(from_pretrained=load)

    # Сверяет кадры без лишнего последнего, путь и частоту сохранения ролика. Изменение числа
    # кадров проявилось бы как неверная длительность видео.
    def export(video_frames, destination, *, fps, quality):
        assert video_frames == list(range(frames))
        assert (destination, fps, quality) == (str(output), 24, 8)
        calls.append("export")

    monkeypatch.setenv("KANDINSKY_MODELS", str(tmp_path / "models"))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(bfloat16="bfloat16", Generator=generator, inference_mode=nullcontext),
    )
    monkeypatch.setitem(
        sys.modules,
        "diffusers",
        SimpleNamespace(
            Kandinsky5T2IPipeline=factory("image"),
            Kandinsky5T2VPipeline=factory("video"),
            Kandinsky5I2VPipeline=factory("image_video"),
        ),
    )
    monkeypatch.setitem(sys.modules, "diffusers.utils", SimpleNamespace(export_to_video=export))
    infer.run(body, output)

    assert calls[:2] == ["load", "offload"]
    assert (
        calls[2:]
        == {
            "image": ["tiles", "infer"],
            "video": ["infer", "export"],
            "image_video": ["tiles", "infer", "export"],
        }[mode]
    )
    if output_size:
        with Image.open(output) as image:
            assert image.format == "PNG" and image.size == output_size


# Подсовывает изображение уже в итоговом размере вместо размера модели. Проверяет отказ и
# отсутствие записанного файла.
def test_inference_rejects_wrong_native_size_without_saving_a_result(tmp_path):
    output = tmp_path / "result.png"
    spec = {"render_width": 1280, "render_height": 768, "width": 960, "height": 576}
    with pytest.raises(ValueError, match="unexpected native image size"):
        infer.save_image(Image.new("RGB", (960, 576)), output, spec)
    assert not output.exists()


# Записывает маленькое изображение без уменьшения и сверяет каждый байт точек. Так обнаруживается
# ненужная обработка при совпавших размерах.
def test_save_image_at_native_resolution_preserves_pixels(tmp_path):
    output = tmp_path / "result.png"
    spec = {"render_width": 3, "render_height": 2, "width": 3, "height": 2}
    original = Image.new("RGB", (3, 2))
    original.putpixel((1, 1), (12, 77, 184))
    infer.save_image(original, output, spec)
    with Image.open(output) as saved:
        assert saved.format == "PNG" and saved.size == original.size
        assert saved.tobytes() == original.tobytes()
