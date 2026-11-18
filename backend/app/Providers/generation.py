"""Подключает выбранного в настройках исполнителя генерации."""

from app.Contracts.generation import Provider
from app.Services.Gpu.provider import DemoProvider, GpuProvider
from config.studio import Settings


def make_provider(settings: Settings) -> Provider:
    """Выбирает исполнителя по настройкам сервера."""
    if settings.provider == "demo":
        return DemoProvider()
    if settings.provider == "kandinsky":
        return GpuProvider(settings.gpu_url, settings.gpu_token)
    raise ValueError("Provider must be demo or kandinsky")
