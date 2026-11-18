# Подключает чтение списка, получение страниц истории и изменения чатов. В каждом вызове владелец
# берётся из уже проверенного входа.

"""Запросы чтения и изменения чатов"""

import uuid
from typing import Annotated, Literal

from fastapi import Query

from app.Http.dependencies import CurrentUser, DatabaseSession
from app.Http.Requests.chats import ThreadDeletionLookup, ThreadPatch, ThreadRequest
from app.Http.Resources.chats import HistoryPage
from app.Http.Resources.generation import job_response
from app.Services.Chats.create_chat import create_chat
from app.Services.Chats.delete_chat import delete_chat
from app.Services.Chats.directory import chat_states, deleted_chats, list_chats
from app.Services.Chats.history import chat_history, recent_jobs
from app.Services.Chats.update_chat import update_chat
from config.studio import Settings


class ChatsController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Регистрирует команды чата и передаёт удалению путь к файлам. Маршруты пользуются общими
    # правилами, чтобы чтение и правки одинаково определяли владельца.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # Передаёт сохранённый на телефоне номер при создании. Если ответ потеряется и клиент пошлёт
    # тот же номер, сервер вернёт прежний чат.
    @staticmethod
    def create_thread(user: CurrentUser, db: DatabaseSession, body: ThreadRequest | None = None):
        """Создаёт чат один раз по номеру, который телефон прислал при отправке"""
        return create_chat(db, user, body.id if body else None)

    # Принимает раздел, строку поиска и размер страницы. Передаёт эти значения списку владельца,
    # чтобы приложение не загружало все чаты для поиска.
    @staticmethod
    def threads(
        user: CurrentUser,
        db: DatabaseSession,
        view: Literal["recent", "archive", "all"] | None = None,
        q: Annotated[str, Query(max_length=100)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
    ):
        """Ищет по названиям владельца и отдаёт одну страницу истории"""
        return list_chats(db, user.id, view=view, query=q, offset=offset, limit=limit)

    # Передаёт на сверку сохранённые номера и возвращает свежие карточки и признаки удаления. Это
    # нужно для чатов, не попавших на первую страницу.
    @staticmethod
    def thread_states(body: ThreadDeletionLookup, user: CurrentUser, db: DatabaseSession):
        """Сверяет сохранённые чаты, оказавшиеся за пределами первой страницы"""
        return chat_states(db, user.id, body.ids)

    # Возвращает только удалённые номера из переданного списка. Телефон может вычистить свой
    # старый список, не читая сообщения и весь журнал.
    @staticmethod
    def thread_deletions(body: ThreadDeletionLookup, user: CurrentUser, db: DatabaseSession):
        """Сверяет известные телефону номера без чтения всей истории удалений"""
        return deleted_chats(db, user.id, body.ids)

    # Передаёт новые название, закрепление и архив. Ручное название учитывается отдельно, иначе
    # первый запрос мог бы заменить его автоматически.
    @staticmethod
    def change_thread(thread_id: str, body: ThreadPatch, user: CurrentUser, db: DatabaseSession):
        """Сохраняет правки владельца и снимает закрепление при переносе в архив"""
        return update_chat(
            db, user, thread_id, title=body.title, pinned=body.pinned, archived=body.archived
        )

    # Передаёт номер и владельца службе очистки, включая путь к файлам. Повтор после потери ответа
    # снова доводит отложенную очистку, не возвращая чат.
    def delete_thread(self, thread_id: str, user: CurrentUser, db: DatabaseSession):
        """Удаляет содержимое чата по запросу владельца и принимает повтор"""
        delete_chat(db, user, thread_id, self._settings.media_dir)

    # Отдаёт последнюю порцию заданий для простого чтения без границы. Проверка доступа и отбор
    # удалённых строк остаются в общей службе истории.
    @staticmethod
    def thread_jobs(thread_id: str, user: CurrentUser, db: DatabaseSession):
        return [job_response(snapshot) for snapshot in recent_jobs(db, user, thread_id)]

    # Передаёт границу, размер и вид страницы в общую историю. Один маршрут умеет загружать старые
    # строки, новые строки или область вокруг выбранного файла.
    @staticmethod
    def paged_history(
        thread_id: str,
        user: CurrentUser,
        db: DatabaseSession,
        limit: Annotated[int, Query(ge=1, le=128)] = 64,
        before: uuid.UUID | None = None,
        after: uuid.UUID | None = None,
        around: uuid.UUID | None = None,
        media_only: bool = False,
        grouped: bool = False,
    ):
        page = chat_history(
            db,
            user,
            thread_id,
            limit=limit,
            before=before,
            after=after,
            around=around,
            media_only=media_only,
            grouped=grouped,
        )
        return HistoryPage(
            messages=[job_response(snapshot) for snapshot in page["messages"]],
            has_older=page["has_older"],
            has_newer=page["has_newer"],
        )
