# Здесь живёт постоянный канал между телефоном и службой связи. По нему приходят новые сообщения,
# удаления и ход загрузок, а номер последнего события позволяет продолжить чтение после обрыва.

"""Соединение с телефоном и доставка изменений после подключения."""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.Exceptions.accounts import ErasedAccount
from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.removal_delivery import RemovalDelivery
from app.Services.Mobile.upload_delivery import UploadDelivery

log = logging.getLogger("bridge.app")


class MobileSocketController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Подключает приём постоянных соединений с общим хранилищем событий и подписками владельца.
    # Параметры задают повтор проверки заданий и удалений, чтобы канал мог продолжать работу после
    # временного сбоя.
    def __init__(
        self,
        adapter: Callable[[], Adapter],
        state: State,
        removals: RemovalDelivery,
        uploads: UploadDelivery,
        poll_seconds: float,
        erasure_key: str,
        erasure_poll_seconds: float,
    ) -> None:
        self._adapter = adapter
        self._state = state
        self._removals = removals
        self._uploads = uploads
        self._poll_seconds = poll_seconds
        self._erasure_key = erasure_key
        self._erasure_poll_seconds = erasure_poll_seconds

    # Проверяет вход, ждёт от телефона границу прочитанного и ведёт по одному каналу команды и
    # обновления. Проверяет доступ во время работы и снимает подписки при закрытии, иначе
    # отозванный вход или старое соединение могли бы продолжить получать события.
    async def socket(self, ws: WebSocket):
        # Отправляет сообщение в канал и отдельно распознаёт ситуацию, когда соединение уже
        # закрыто. Для обычных ошибок сохраняет исходную причину, чтобы они не скрылись под видом
        # разрыва связи.
        async def send_packet(packet):
            try:
                await ws.send_json(packet)
            except RuntimeError as error:
                # Сетевой сервер может закрыть канал раньше чтения. Для этого нет отдельного вида ошибки.
                closed_transport = str(error) == (
                    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."
                )
                if ws.client_state != WebSocketState.DISCONNECTED and not closed_transport:
                    raise
                raise WebSocketDisconnect(code=1006) from error

        # Перед закрытием сообщает, что аккаунт удалён и повторять запрос нельзя. Телефон получает
        # явную причину прекратить переподключение по старому ключу.
        async def close_erased():
            await send_packet({"type": "error", "code": "account_deleted", "retryable": False})
            await ws.close(code=1008)

        try:
            owner, token = await self._adapter().owner(ws.headers)
        except HTTPException as error:
            if error.detail == "account_deleted":
                await ws.accept()
                await close_erased()
            else:
                await ws.close(code=1008)
            return
        await ws.accept()
        cursor = None
        directory_updates = False
        upload_updates = False
        valid_until = time.monotonic() + 30
        change = self._removals.subscribe(owner)
        incoming = asyncio.create_task(ws.receive_json())
        changed = asyncio.create_task(change.wait())
        upload_change = self._uploads.subscribe(owner)
        upload_changed = asyncio.create_task(upload_change.wait())
        retry_removal = None
        owner_delivery = None
        if not self._erasure_key:
            owner_delivery = asyncio.create_task(
                self._removals.follow_owner(
                    self._adapter(), token, change, self._erasure_poll_seconds
                )
            )
        next_job_poll = time.monotonic() + self._poll_seconds
        try:
            while True:
                self._uploads.expire()
                if time.monotonic() >= valid_until:
                    await self._adapter().studio.request("GET", "/v1/auth/me", token)
                    valid_until = time.monotonic() + 30
                timeout = max(0, next_job_poll - time.monotonic()) if cursor is not None else 15
                ready, _ = await asyncio.wait(
                    ({incoming, changed, upload_changed} if upload_updates else {incoming, changed})
                    if cursor is not None
                    else {incoming},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cursor is None and not ready:
                    return
                if upload_changed in ready:
                    upload_change.clear()
                    await send_packet(
                        {"type": "kandinsky.uploads", "uploads": self._uploads.snapshot(owner)}
                    )
                    upload_changed = asyncio.create_task(upload_change.wait())
                if changed in ready:
                    change.clear()
                    if retry_removal is not None:
                        retry_removal.cancel()
                        retry_removal = None
                    try:
                        await self._adapter().sync_content_removals(owner, token)
                        if directory_updates:
                            await send_packet({"type": "kandinsky.catalog.changed"})
                    except HTTPException as error:
                        if error.status_code < 500:
                            raise
                        log.warning("Owner removal delivery interrupted (%s)", error.status_code)
                        retry_removal = asyncio.get_running_loop().call_later(
                            self._erasure_poll_seconds, change.set
                        )
                    changed = asyncio.create_task(change.wait())
                if cursor is not None and time.monotonic() >= next_job_poll:
                    if self._state.is_erased(owner):
                        raise ErasedAccount
                    await self._adapter().poll(owner, token)
                    next_job_poll = time.monotonic() + self._poll_seconds
                packet = {}
                if incoming in ready:
                    packet = incoming.result()
                    incoming = asyncio.create_task(ws.receive_json())
                if not isinstance(packet, dict):
                    await ws.close(code=1003)
                    return
                kind = packet.get("type")
                try:
                    if kind == "resume":
                        features = packet.get("features")
                        directory_updates = (
                            isinstance(features, dict) and features.get("chatDirectory") is True
                        )
                        upload_updates = (
                            isinstance(features, dict) and features.get("uploadProgress") is True
                        )
                        await self._adapter().sync_content_removals(owner, token)
                        cursor = max(0, min(int(packet["cursor"]), self._state.cursor(owner)))
                        await send_packet({"type": "resumed", "onlineUserIds": [], "features": {}})
                        if upload_updates:
                            upload_change.clear()
                            await send_packet(
                                {
                                    "type": "kandinsky.uploads",
                                    "uploads": self._uploads.snapshot(owner),
                                }
                            )
                    elif cursor is None:
                        await ws.close(code=1008)
                        return
                    elif kind == "send":
                        messages = packet["messages"]
                        if not 1 <= len(messages) <= 10:
                            raise HTTPException(422, "Слишком много запросов")
                        saved = [
                            await self._adapter().submit(owner, token, row) for row in messages
                        ]
                        await send_packet(
                            {
                                "type": "accepted",
                                "requestId": packet.get("requestId"),
                                "messages": saved,
                            }
                        )
                    elif kind == "kandinsky.open":
                        await self._adapter().history(
                            owner, token, int(packet["chatId"]), 64, publish=True
                        )
                    elif kind in {"kandinsky.check", "kandinsky.cancel"}:
                        job = self._state.boundary(
                            owner,
                            int(packet["chatId"]),
                            message=int(packet["messageId"]),
                        )
                        if not job:
                            raise HTTPException(404, "Задание не найдено")
                        cancel = kind == "kandinsky.cancel"
                        row = await self._adapter().studio.request(
                            "POST" if cancel else "GET",
                            f"/v1/jobs/{job}" + ("/cancel" if cancel else ""),
                            token,
                        )
                        await self._adapter().rows(owner, token, row, publish=True)
                    elif kind == "applied":
                        await send_packet({"type": "applied", "cursor": packet["cursor"]})
                    elif kind == "ping":
                        await send_packet({"type": "pong"})
                    elif kind == "delete":
                        raise HTTPException(422, "Используйте отмену генерации")
                except (KeyError, ValueError, TypeError, HTTPException) as error:
                    if isinstance(error, HTTPException) and error.detail == "account_deleted":
                        raise ErasedAccount from error
                    retryable = isinstance(error, HTTPException) and error.status_code >= 500
                    await send_packet(
                        {
                            "type": "error",
                            "requestId": packet.get("requestId"),
                            "code": str(error.detail)
                            if isinstance(error, HTTPException)
                            else "Неверный запрос",
                            "retryable": retryable,
                        }
                    )
                if cursor is not None:
                    while page := await self._adapter().event_page(owner, token, cursor):
                        cursor = page["cursor"]
                        await send_packet(page)
        except WebSocketDisconnect:
            pass
        except ErasedAccount:
            await close_erased()
        except HTTPException as error:
            if error.detail == "account_deleted":
                await close_erased()
            else:
                await ws.close(code=1008)
        finally:
            if retry_removal is not None:
                retry_removal.cancel()
            self._removals.unsubscribe(owner, change)
            self._uploads.unsubscribe(owner, upload_change)
            tasks = [incoming, changed, upload_changed]
            if owner_delivery is not None:
                tasks.append(owner_delivery)
            for task in tasks:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
