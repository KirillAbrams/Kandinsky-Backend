# Запускает пробный обработчик с заданным сбоем четвёртого результата. Так проверяется повтор
# одной части запроса без настоящего вычисления.

"""Запуск местного показа одной неудачной ячейки в группе из четырёх."""

import asyncio
from unittest.mock import patch

from app.Jobs import worker
from app.Services.Gpu.provider import DemoProvider
from config.studio import Settings


class RetryDemoProvider(DemoProvider):
    # На первой попытке останавливает только четвёртый результат. Остальные позиции и повтор
    # четвёртой обрабатывает обычный пробный генератор. Без такого сбоя проверка не покажет,
    # сохраняются ли уже готовые файлы при повторе.
    async def generate(self, job, output, reference):
        if job.batch_index == 3 and job.run_id is None:
            raise RuntimeError("Проверка повтора четвёртого результата")
        return await super().generate(job, output, reference)


if __name__ == "__main__":
    if Settings().provider != "demo":
        raise SystemExit("Для показа укажи STUDIO_PROVIDER=demo в местных настройках.")
    with patch.object(worker, "make_provider", return_value=RetryDemoProvider()):
        asyncio.run(worker.main())
