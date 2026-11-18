"""Действия удалённого и демонстрационного исполнителей."""

from pathlib import Path
from typing import Protocol

from app.Models import Job


class Provider(Protocol):
    async def generate(self, job: Job, output: Path, reference: Path | None) -> str:
        """Записывает результат по переданному пути и возвращает его тип."""
        ...

    # Определяет команду остановки по номеру работы. Обработчик может вызывать её после потери
    # права, не зная, где исполнитель запустил вычисление.
    async def cancel(self, job_id: str) -> None:
        """Останавливает работу с указанным номером, если исполнитель ещё её выполняет."""
        ...
