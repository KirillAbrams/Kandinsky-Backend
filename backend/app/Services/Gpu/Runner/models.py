# Хранит версии весов и размеры, доступные для разных режимов. Закреплённые версии и таблица
# размеров нужны, чтобы подготовка образа и исполнение задания использовали одинаковые ожидания.

"""Номера версий закрепляют точный набор весов для каждой задачи
При подготовке заранее загружаем их в образ
"""

from pathlib import Path

MODELS = {
    "image": (
        "kandinskylab/Kandinsky-5.0-T2I-Lite-sft-Diffusers",
        "c0158b5b20858d1fc02088db7ed4d805a296498b",
    ),
    "video": (
        "kandinskylab/Kandinsky-5.0-T2V-Lite-distilled16steps-5s-Diffusers",
        "3889255b1e76b0f40d57d4123fcd32ff69ced139",
    ),
    "image_video": (
        "kandinskylab/Kandinsky-5.0-I2V-Lite-5s-Diffusers",
        "8d965626ab76734922b00e359ca2d8faab2e0417",
    ),
}
DIFFUSERS_REVISION = "423ab90580b2847998591669b8326f47b6d14d8b"
VIDEO_SR_MODEL = (
    "kandinskylab/Kandinsky-6.0-VSR-distilled2steps-5s-Diffusers",
    "3d6672923a4dfcd387961864654e123dd53b8ee4",
)
VIDEO_SR_PYTHON = "/opt/kandinsky-vsr/bin/python"


# Не загружает материалы описания и примеры из репозитория, чтобы в образ попадали нужные для выполнения
# файлы.
# Загружает точные версии весов для обычных режимов до запуска видеокарты. Исключает изображения
# примеров и описания, чтобы не загружать лишнее перед вычислениями.
def prepare_models(root="/models"):
    """Скачивает веса на этапе подготовки образа до аренды видеокарты"""
    from huggingface_hub import snapshot_download

    for mode, (repo, revision) in MODELS.items():
        snapshot_download(
            repo_id=repo,
            revision=revision,
            local_dir=str(Path(root) / mode),
            ignore_patterns=["assets/*", "*.md", ".gitattributes"],
        )


# Для уменьшенного изображения сохраняет исходный размер вычисления, поскольку уменьшение выполняется уже
# после генерации.
# Проверяет сочетание режима, качества, длительности и подсказок, затем рассчитывает размеры и
# число кадров. Для уменьшенного фото сохраняет родной размер вычисления, иначе модель получила бы
# неподходящий размер.
def generation_spec(body):
    is_image = body["model"] == "image"
    if body["model"] not in ("image", "video") or (is_image and body.get("image")):
        raise ValueError("Unsupported generation mode")
    mode = "image" if is_image else ("image_video" if body.get("image") else "video")
    sizes = (
        {"1:1": (1024, 1024), "5:3": (1280, 768), "3:5": (768, 1280)}
        if is_image
        else {"1:1": (512, 512), "5:3": (800, 480), "3:5": (480, 800)}
    )
    quality = body.get("quality", "standard")
    resolution = body.get("resolution", "standard")
    duration = body.get("duration_seconds", 5)
    guidance = body.get("guidance_scale")
    if (
        quality not in ("draft", "standard")
        or resolution not in ("compact", "standard")
        or duration not in (1, 3, 5)
        or (is_image and duration != 5)
    ):
        raise ValueError("Unsupported generation settings")
    if mode == "video" and (
        quality != "standard" or guidance not in (None, 1.0) or body.get("negative_prompt")
    ):
        raise ValueError("Distilled video uses 16 steps and guidance=1")
    if guidance is not None and not 1 <= guidance <= 8:
        raise ValueError("Unsupported guidance")
    native_width, native_height = sizes[body["aspect"]]
    if resolution == "compact":
        sizes = (
            {"1:1": (768, 768), "5:3": (960, 576), "3:5": (576, 960)}
            if is_image
            else {"1:1": (384, 384), "5:3": (640, 384), "3:5": (384, 640)}
        )
    width, height = sizes[body["aspect"]]
    steps = 16 if mode == "video" else (28 if is_image else 30) if quality == "draft" else 50
    return {
        "mode": mode,
        "width": width,
        "height": height,
        "render_width": native_width if is_image else width,
        "render_height": native_height if is_image else height,
        "steps": steps,
        "guidance": guidance
        if guidance is not None
        else (1.0 if mode == "video" else 3.5 if is_image else 5.0),
        "num_frames": duration * 24 + 1,
        "output_frames": duration * 24,
    }
