# Заполняет только отсутствующие настройки местного запуска.
# Повторный вызов сохраняет заданные пароли и адреса, поэтому службы не теряют доступ после перезапуска.

"""Создаёт настройки серверных служб, сохраняя уже заданные значения."""

import os
import secrets
import sys
from pathlib import Path

defaults = {
    # Пароль начальной роли для подготовки базы.
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(32),
    # Отдельный пароль роли, которая меняет таблицы.
    "STUDIO_MIGRATION_PASSWORD": lambda: secrets.token_urlsafe(32),
    # Отдельный пароль роли, которая обслуживает запросы.
    "STUDIO_API_PASSWORD": lambda: secrets.token_urlsafe(32),
    # Отдельный пароль роли, которая обрабатывает задания.
    "STUDIO_WORKER_PASSWORD": lambda: secrets.token_urlsafe(32),
    # Выбирает настоящий генератор при обычном запуске.
    "STUDIO_PROVIDER": lambda: "kandinsky",
    # Задаёт адрес службы генерации на компьютере, с которого запускаются контейнеры.
    "STUDIO_GPU_URL": lambda: "http://host.docker.internal:8099",
    # Создаёт отдельный ключ доступа к службе генерации.
    "STUDIO_GPU_TOKEN": lambda: secrets.token_urlsafe(32),
    # Создаёт общий закрытый ключ для передачи команды удаления между службами.
    "STUDIO_BRIDGE_ERASURE_KEY": lambda: secrets.token_urlsafe(32),
}

if sys.argv[1:] == ["--review"]:
    # Передаёт настройки на компьютер, когда скрипт запущен внутри контейнера.
    for name, generate in defaults.items():
        if name not in {"STUDIO_PROVIDER", "STUDIO_GPU_URL", "STUDIO_GPU_TOKEN"}:
            print(f"{name}={generate()}")
    print(f"KANDINSKY_REVIEW_PASSWORD={secrets.token_urlsafe(24)}")
    print("KANDINSKY_SERVER_IMAGE=kandinsky-review-api:local")
    raise SystemExit

# При создании ограничивает доступ владельцем. Читает уже записанные имена и дописывает
# недостающие значения в конец, чтобы повторный запуск не сменил настроенные ключи.
path = Path(__file__).resolve().parents[1] / ".env"
with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+") as settings:
    content = settings.read()
    keys = {
        line.split("=", 1)[0].strip() for line in content.splitlines() if "=" in line
    }
    missing = [name for name in defaults if name not in keys]
    if missing:
        if content and not content.endswith("\n"):
            settings.write("\n")
        for name in missing:
            settings.write(f"{name}={defaults[name]()}\n")
print("Настройки сервера готовы")
