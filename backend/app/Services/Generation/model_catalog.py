# Формирует список режимов и настроек, который получает телефон. Подсказки интерфейса следуют за
# набором возможностей сервера, поэтому не нужно выпускать приложение при одной только смене
# настроек.

"""Список настроек, которые клиент показывает для каждой модели
Служба обработки повторно проверяет запрос перед запуском"""


def capabilities(model: str, reference: bool = False) -> dict[str, object]:
    """Возвращает доступные размеры, качество и длительность для режима и исходника."""
    video = model == "video"
    qualities = [
        {"id": "standard", "name": "Стандарт", "steps": 16 if video and not reference else 50}
    ]
    if not video or reference:
        qualities.insert(0, {"id": "draft", "name": "Быстро", "steps": 30 if video else 28})
    return {
        "quality": qualities,
        "resolutions": [
            {
                "id": "compact",
                "name": "Компактный",
                "sizes": {"1:1": [384, 384], "5:3": [640, 384], "3:5": [384, 640]}
                if video
                else {"1:1": [768, 768], "5:3": [960, 576], "3:5": [576, 960]},
            },
            {
                "id": "standard",
                "name": "Полный",
                "sizes": {"1:1": [512, 512], "5:3": [800, 480], "3:5": [480, 800]}
                if video
                else {"1:1": [1024, 1024], "5:3": [1280, 768], "3:5": [768, 1280]},
            },
        ],
        "aspects": ["1:1", "5:3", "3:5"],
        "duration_seconds": [1, 3, 5] if video else [],
        "max_count": 4,
        "negative_prompt": not video or reference,
        "guidance": {"min": 1.0, "max": 8.0, "default": 5.0 if reference else 3.5}
        if not video or reference
        else None,
        "audio": False,
    }


def model_catalog(provider: str, *, video_sr: bool = False) -> list[dict[str, object]]:
    """Показывает только режимы, которые поддерживает настроенный исполнитель."""
    image = capabilities("image")
    image["resolutions"] = [row for row in image["resolutions"] if row["id"] == "standard"]
    models = [
        {
            "id": "image",
            "name": "Image Lite",
            "kind": "image",
            "description": "Изображения",
            "accepts_image": False,
            "provider": provider,
            "capabilities": image,
        },
        {
            "id": "video",
            "name": "Video Lite",
            "kind": "video",
            "description": "Видео и оживление фото",
            "accepts_image": True,
            "provider": provider,
            "capabilities": capabilities("video"),
            "reference_capabilities": capabilities("video", True),
        },
    ]
    if provider == "kandinsky" and video_sr:
        models.append(
            {
                "id": "video_sr",
                "name": "Улучшить видео",
                "kind": "video",
                "description": "Удвоить разрешение короткого MP4",
                "accepts_image": False,
                "provider": provider,
                "capabilities": {"scale": 2, "max_count": 1, "max_seconds": 5},
            }
        )
    return models
