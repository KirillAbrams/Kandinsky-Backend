# Сверяет закреплённые версии моделей, размеры и подготовку заданий для исполнителя. Проверки не
# загружают веса и не запускают видеокарту.

"""Проверяет доступные размеры, виды обработки и закреплённые версии весов"""

import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from PIL import Image

from app.Services.Gpu.Runner.infer import save_image
from app.Services.Gpu.Runner.models import MODELS, generation_spec, prepare_models


# Сверяет три ожидаемые модели и форму отпечатков их версий. Ссылка на последнюю версию иначе
# могла бы поменять веса без изменения проекта.
def test_official_model_ids_and_revisions():
    assert len(MODELS) == 3
    for repo, revision in MODELS.values():
        assert repo.startswith("kandinskylab/Kandinsky-5.0-")
        assert repo.endswith("-Diffusers")
        assert len(revision) == 40
        int(revision, 16)


# Подменяет загрузчик, проверяет точную версию каждого набора и отдельную папку назначения. Если
# настройки перемешаются, один режим получит файлы другого.
def test_model_preparation_downloads_the_pinned_weights_to_their_own_directories(
    tmp_path, monkeypatch
):
    download = Mock()
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))

    prepare_models(tmp_path)

    assert download.call_args_list == [
        call(
            repo_id=repo,
            revision=revision,
            local_dir=str(tmp_path / mode),
            ignore_patterns=["assets/*", "*.md", ".gitattributes"],
        )
        for mode, (repo, revision) in MODELS.items()
    ]


# Проверяет значения чуть ниже и выше допустимой силы следования тексту. Оба должны быть отклонены
# ещё при подготовке задания.
@pytest.mark.parametrize("guidance", [0.9, 8.1])
def test_image_guidance_rejects_out_of_range_values(guidance):
    with pytest.raises(ValueError, match="Unsupported guidance"):
        generation_spec({"model": "image", "aspect": "1:1", "guidance_scale": guidance})


# Сверяет размеры для каждого формата, режим ролика и фиксированные шаги и силу следования тексту.
# Изменение любого из этих условий обнаружится без запуска модели.
@pytest.mark.parametrize(
    "aspect,expected", [("1:1", (512, 512)), ("5:3", (800, 480)), ("3:5", (480, 800))]
)
def test_video_sizes_and_distillation(aspect, expected):
    spec = generation_spec({"model": "video", "aspect": aspect, "image": None})
    assert (spec["width"], spec["height"]) == expected
    assert spec["mode"] == "video"
    assert spec["steps"] == 16
    assert spec["guidance"] == 1.0


# Проверяет, что исходное фото выбирает режим оживления и запрещено при создании фото из текста.
# Отдельно сверяет размеры и число шагов обычного изображения.
def test_image_reference_selects_i2v_and_never_edits_t2i():
    spec = generation_spec({"model": "video", "aspect": "1:1", "image": "encoded"})
    assert spec["mode"] == "image_video"
    assert spec["steps"] == 50
    assert spec["guidance"] == 5.0
    with pytest.raises(ValueError):
        generation_spec({"model": "image", "aspect": "1:1", "image": "encoded"})
    spec = generation_spec({"model": "image", "aspect": "5:3", "image": None})
    assert (spec["width"], spec["height"]) == (1280, 768)
    assert spec["steps"] == 50


# Проверяет перенос качества, итогового размера и силы запроса в параметры вычисления. Для ролика
# отдельно считает добавочный кадр модели и длительность записываемого файла.
def test_selected_quality_size_duration_and_cfg_reach_inference_spec():
    image = generation_spec(
        {
            "model": "image",
            "aspect": "1:1",
            "quality": "draft",
            "resolution": "compact",
            "guidance_scale": 4.0,
        }
    )
    assert (image["width"], image["height"], image["steps"], image["guidance"]) == (
        768,
        768,
        28,
        4.0,
    )
    assert (image["render_width"], image["render_height"]) == (1024, 1024)
    video = generation_spec(
        {
            "model": "video",
            "aspect": "5:3",
            "duration_seconds": 3,
            "resolution": "compact",
        }
    )
    assert (video["width"], video["height"], video["num_frames"]) == (640, 384, 73)
    assert video["output_frames"] == 72
    assert (video["render_width"], video["render_height"]) == (640, 384)
    with pytest.raises(ValueError):
        generation_spec({"model": "video", "aspect": "1:1", "duration_seconds": 10})


# Записывает изображение из исходного размера в уменьшенный и сверяет формат сохранения. Передача
# неверного размера от модели должна закончиться отказом до записи.
def test_legacy_compact_export_preserves_native_inference(tmp_path):
    spec = generation_spec({"model": "image", "aspect": "3:5", "resolution": "compact"})
    output = tmp_path / "result.png"
    image = Image.new("RGB", (768, 1280), "mintcream")
    save_image(image, output, spec)
    with Image.open(output) as result:
        assert result.format == "PNG"
        assert result.size == (576, 960)
    with pytest.raises(ValueError):
        save_image(Image.new("RGB", (960, 576)), output, spec)
