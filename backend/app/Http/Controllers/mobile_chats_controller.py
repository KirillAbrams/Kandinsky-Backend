# Здесь находятся команды для списка чатов, истории и заданий. Номера из телефона переводятся в
# номера основного сервера, а изменившееся содержимое можно доставить на другие устройства.

"""Чаты, история и команды генерации от мобильных приложений."""

import asyncio
import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import Query, Request
from fastapi.responses import Response

from app.Http.Requests.chats import ThreadPatch, ThreadRequest
from app.Http.Requests.generation import GenerationReportRequest
from app.Http.Requests.Mobile.chats import ChatStateLookup, CreateNativeChat, GenerationAction
from app.Services.Database.Mobile.state import State
from app.Services.Mobile import erasure_sync
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.removal_delivery import RemovalDelivery


class MobileChatsController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Подключает команды чатов, истории и создания медиа. Передаёт им общие средства проверки входа,
    # хранения номеров и доставки удалений, иначе разные запросы видели бы разное состояние.
    def __init__(
        self,
        adapter: Callable[[], Adapter],
        state: State,
        removals: RemovalDelivery,
        erasure_key: str,
    ) -> None:
        self._adapter = adapter
        self._state = state
        self._removals = removals
        self._erasure_key = erasure_key

    # Проверяет вход и под ключом владельца запрашивает доступные модели. Телефон получает список
    # от основного сервера, поэтому изменение настроек видно без выпуска новой сборки.
    async def models(self, request: Request):
        _, token = await self._adapter().owner(request.headers)
        return {"models": await self._adapter().studio.request("GET", "/v1/models", token)}

    # Удаляет запрос или выбранный результат в указанном чате от имени владельца. После успешной
    # очистки будит остальные устройства этого аккаунта, чтобы они убрали удалённое из ленты.
    async def delete_studio_content(
        self,
        request: Request,
        chat: int,
        message: int,
        target: Literal["prompt", "result"],
        blob: uuid.UUID | None = None,
    ):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        await service.delete_content(
            owner, token, chat, message, target, str(blob) if blob else None
        )
        self._removals.changed(owner)
        return Response(status_code=204)

    # Запрашивает нужную часть списка с учётом поиска и архива. Переводит номера чатов в формат
    # телефона, чтобы открытие строки вело к правильной истории.
    async def studio_threads(
        self,
        request: Request,
        view: Literal["recent", "archive", "all"] = "recent",
        q: str = Query("", max_length=100),
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=200),
    ):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        threads = await service.studio.request(
            "GET",
            "/v1/threads",
            token,
            params={"view": view, "q": q, "offset": offset, "limit": limit},
        )
        return {"threads": [service.studio_thread(owner, row) for row in threads]}

    # Находит чат владельца и передаёт только изменённые поля основному серверу. Пустые поля
    # исключаются, чтобы отдельное действие не сбрасывало остальные свойства чата.
    async def change_studio_thread(self, request: Request, chat: int, change: ThreadPatch):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        thread = service.thread(owner, chat)
        row = await service.studio.request(
            "PATCH",
            f"/v1/threads/{thread}",
            token,
            json=change.model_dump(exclude_none=True),
        )
        return service.studio_thread(owner, row)

    # Сначала удаляет чат на основном сервере. Затем вычищает его местную копию и связанные
    # записи. Уведомляет устройства только после записи изменений, чтобы чат исчез у каждого.
    async def delete_studio_thread(self, request: Request, chat: int):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        thread = service.thread(owner, chat)
        await service.studio.request("DELETE", f"/v1/threads/{thread}", token)
        await asyncio.to_thread(self._state.erase_chat, owner, chat)
        await service.sync_content_removals(owner, token)
        if self._erasure_key:
            await erasure_sync.sync_thread_erasures(service, self._erasure_key)
        self._removals.changed(owner)
        return Response(status_code=204)

    # Проверяет вход и передаёт команду для конкретного сообщения и, при необходимости, отдельного
    # результата. Проверка владельца и номера нужна, чтобы действие не затронуло другую генерацию.
    async def generation_action(
        self, request: Request, chat: int, message: int, body: GenerationAction
    ):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        return await service.generation_action(owner, token, chat, message, body)

    # Связывает жалобу с владельцем, чатом и конкретным ответом. Модератор получает сведения о
    # нужной генерации, даже если в чате было несколько похожих ответов.
    async def studio_generation_report(
        self,
        request: Request,
        chat: int,
        message: int,
        body: GenerationReportRequest,
    ):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        return await service.report_generation(owner, token, chat, message, body)

    # Передаёт основной службе номер нового чата, если телефон уже его выбрал, и возвращает
    # созданный чат. Сохранённый номер помогает повторить отправку при потере ответа без второго
    # чата.
    async def create_chat(self, request: Request, body: CreateNativeChat | None = None):
        owner, token = await self._adapter().owner(request.headers)
        identity = ThreadRequest(id=body.id).model_dump(mode="json") if body and body.id else None
        thread = await self._adapter().studio.request("POST", "/v1/threads", token, json=identity)
        chat = self._adapter().chat(owner, thread)
        return {"chatId": chat["id"], "chat": chat, "chats": [chat]}

    # Запрашивает недавние чаты вошедшего пользователя и переводит их в формат телефона. Архив
    # запрашивается отдельной командой, поэтому он не смешивается с обычным списком.
    async def chats(self, request: Request):
        owner, token = await self._adapter().owner(request.headers)
        rows = await self._adapter().studio.request(
            "GET", "/v1/threads", token, params={"view": "recent"}
        )
        return {
            "chats": [self._adapter().studio_thread(owner, row) for row in rows],
            "contentReceipts": [],
        }

    # Переводит выбранные номера в номера основного сервера и возвращает их текущее состояние
    # одной порцией. Если список пуст, обращение на сервер не требуется.
    async def chat_states(self, request: Request, body: ChatStateLookup):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        threads = [service.thread(owner, chat) for chat in body.ids]
        if not threads:
            return {"chats": []}
        page = await service.studio.request(
            "POST", "/v1/threads/states", token, json={"ids": threads}
        )
        return {"chats": [service.studio_thread(owner, row) for row in page["threads"]]}

    # Собирает первые данные после входа. Вместе со списком чатов отдаёт начальную историю первого
    # чата, чтобы телефон мог показать её без отдельного ожидания. Читает номер последнего события
    # до списка чатов, чтобы события, пришедшие во время чтения, дождались телефона.
    async def bootstrap(self, request: Request):
        owner, token = await self._adapter().owner(request.headers)
        cursor = self._state.cursor(owner)
        rows = await self._adapter().studio.request(
            "GET", "/v1/threads", token, params={"view": "recent"}
        )
        summaries = [self._adapter().studio_thread(owner, row) for row in rows]
        messages = []
        if summaries:
            messages = (await self._adapter().history(owner, token, summaries[0]["id"], 64))[
                "messages"
            ]
        return {
            "version": 1,
            "userId": self._state.identity(owner, "user", owner),
            "cursor": cursor,
            "chats": summaries,
            "messages": messages,
            "contentReceipts": [],
        }

    # Проверяет владельца и просит ограниченную порцию истории до указанной границы. Так телефон
    # может читать старые сообщения частями вместо скачивания всего длинного чата.
    async def history(
        self,
        request: Request,
        chat: int,
        limit: int = Query(128, ge=1, le=128),
        before: int | None = None,
    ):
        owner, token = await self._adapter().owner(request.headers)
        return await self._adapter().history(owner, token, chat, limit, before)
