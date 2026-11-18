"""Проверяет связь обработчика с моделями до запуска стенда."""

from urllib.parse import urlsplit

import httpx
from app.Services.Gpu.Runner.models import DIFFUSERS_REVISION, MODELS, VIDEO_SR_MODEL
from config.studio import Settings


def check_models(health, expected) -> None:
    """Сверяет движок и закреплённые версии весов."""
    if (
        health.get("ok") is not True
        or health.get("engine") != "diffusers"
        or health.get("diffusers_revision") != DIFFUSERS_REVISION
    ):
        raise ValueError("Служба использует другой движок или версию генератора")
    for name, (repo, revision) in expected.items():
        model = health["models"][name]
        if model.get("present") is not True or (
            model.get("repo"),
            model.get("revision"),
        ) != (
            repo,
            revision,
        ):
            raise ValueError(f"Модель {name} отсутствует или имеет другую версию")


def main() -> None:
    settings = Settings()
    try:
        address = urlsplit(settings.gpu_url)
        _ = address.port
    except ValueError:
        raise SystemExit("Укажи действующий адрес службы генерации") from None
    local = address.scheme == "http" and address.hostname == "host.docker.internal"
    if (
        (address.scheme != "https" and not local)
        or not address.hostname
        or address.path not in ("", "/")
        or address.username is not None
        or address.password is not None
        or address.query
        or address.fragment
        or len(settings.gpu_token) < 32
    ):
        raise SystemExit("Укажи адрес HTTPS и ключ совместимой службы генерации")

    expected = dict(MODELS)
    if settings.video_sr_enabled:
        expected["video_sr"] = VIDEO_SR_MODEL
    try:
        response = httpx.get(
            settings.gpu_url.rstrip("/") + "/health",
            headers={"Authorization": "Bearer " + settings.gpu_token},
            timeout=15,
            follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise SystemExit(
            f"Служба генерации ответила кодом {error.response.status_code}. Проверь адрес и ключ."
        ) from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise SystemExit(
            "Служба генерации недоступна из Docker. Проверь её подключение."
        ) from None

    try:
        check_models(response.json(), expected)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SystemExit(
            "Служба генерации не подтвердила нужные версии моделей и готовность весов."
        ) from None
    print("Кандинский готов к генерации фото и видео")


if __name__ == "__main__":
    main()
