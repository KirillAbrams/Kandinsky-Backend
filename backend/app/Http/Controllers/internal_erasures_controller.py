# Подключает выдачу и подтверждение пакетов удалений между сервером и службой связи. Эти вызовы
# отделены от команд пользователя, потому что служба обрабатывает очередь сразу нескольких
# владельцев.

"""Запросы подтверждения удаления данных между службами"""

import secrets
import uuid
from typing import Annotated

from fastapi import Header, HTTPException

from app.Http.dependencies import DatabaseSession
from app.Http.Requests.accounts import AccountErasureBatch
from app.Http.Requests.generation import ResultRemovalAck
from app.Services.Database.notifications import RemovalFeed
from app.Services.Mobile.acknowledge_erasures import (
    acknowledge_account_erasures,
    acknowledge_content_erasures,
    acknowledge_result_removals,
    acknowledge_thread_erasures,
)
from app.Services.Mobile.pending_erasures import (
    pending_account_erasures,
    pending_content_erasures,
    pending_result_removals,
    pending_thread_erasures,
)
from config.studio import Settings


class ErasuresController:
    """Проверяет доступ службы связи и передаёт отметки удаления."""

    # Подключает чтение, ожидание и подтверждение удалений. Проверяет один закрытый ключ для всех этих
    # вызовов, чтобы очередь не была доступна обычному клиенту.
    def __init__(self, settings: Settings, feed: RemovalFeed) -> None:
        self._settings = settings
        self._feed = feed

    # Проверяет, что обмен настроен, и сверяет переданный ключ. При выключенном обмене вызовы
    # скрыты, а неверный ключ не даёт прочитать или подтвердить очередь.
    def require_erasure_key(
        self,
        key: Annotated[str, Header(alias="X-Kandinsky-Erasure-Key")] = "",
    ):
        configured = self._settings.bridge_erasure_key.get_secret_value()
        if not configured:
            raise HTTPException(404)
        if not secrets.compare_digest(key.encode(), configured.encode()):
            raise HTTPException(403)

    # Берёт порцию удалённых чатов, ещё ожидающих очистки, и отдаёт номера чатов и владельцев.
    # Содержимое не нужно, чтобы служба связи убрала старую копию.
    @staticmethod
    def thread_erasures(db: DatabaseSession):
        return {"items": pending_thread_erasures(db)}

    # Проверяет размер подтверждения и снимает ожидание только у удалённых чатов. Повтор
    # завершённого подтверждения не восстанавливает чат и не меняет живые записи.
    @staticmethod
    def ack_thread_erasures(db: DatabaseSession, ids: list[uuid.UUID]):
        acknowledge_thread_erasures(db, ids)

    # Отдаёт старейшие отметки об удалённых запросах и файлах вместе с владельцем. По ним служба
    # связи может почистить подходящие локальные записи.
    @staticmethod
    def content_erasures(db: DatabaseSession):
        return {"items": pending_content_erasures(db)}

    # Удаляет только отметки, чьи номера подтверждены службой связи. Если чтение было прервано,
    # неподтверждённые события останутся для повторной доставки.
    @staticmethod
    def ack_content_erasures(db: DatabaseSession, ids: list[uuid.UUID]):
        acknowledge_content_erasures(db, ids)

    # Ждёт сигнала после переданной отметки. Обновление приходит после появления изменения вместо
    # постоянного чтения всех очередей.
    async def wait_for_removals(self, cursor: str = ""):
        return await self._feed.wait(cursor)

    # Читает порцию номеров удалённых владельцев и оставляет строки до подтверждения. Так потеря
    # ответа при чтении не отменяет очистку на стороне связи.
    @staticmethod
    def account_erasures(db: DatabaseSession):
        return {"owners": pending_account_erasures(db)}

    # Убирает подтверждённые номера владельцев и просроченные отпечатки отозванных сеансов.
    # Действующие отметки сеансов нужны, чтобы телефон ещё мог распознать удаление.
    @staticmethod
    def acknowledge_account_erasures(batch: AccountErasureBatch, db: DatabaseSession):
        acknowledge_account_erasures(db, batch.owners)

    # Выдаёт номера отозванных результатов, их владельцев и файлов. Служба связи получает всё
    # нужное для удаления обложек и правки истории без чтения самих медиа.
    @staticmethod
    def result_removals(db: DatabaseSession):
        return {"items": pending_result_removals(db)}

    # Убирает записи только по подтверждённым номерам заданий. До этого момента они доступны для
    # повторного запроса при обрыве.
    @staticmethod
    def acknowledge_result_removals(batch: ResultRemovalAck, db: DatabaseSession):
        acknowledge_result_removals(db, batch.jobs)
