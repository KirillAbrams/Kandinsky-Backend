# Здесь служба связи узнаёт об удалениях и будит подключённые телефоны после записи изменений.
# После разрыва она возвращается к началу выдачи, чтобы получить пропущенное.

"""Служба связи дочитывает очередь после уведомления или восстановления соединения."""

import asyncio
import logging
import sqlite3
from collections import defaultdict

from fastapi import HTTPException

from app.Services.Mobile import erasure_sync

log = logging.getLogger(__name__)


class RemovalDelivery:
    # Создаёт наборы сигналов по владельцам. Такое разделение позволяет уведомлять только
    # устройства того аккаунта, в котором что-то удалилось.
    def __init__(self):
        self.sockets = defaultdict(set)

    # Добавляет отдельный сигнал для нового соединения. Если у владельца несколько устройств,
    # каждое сможет самостоятельно дочитать свои изменения.
    def subscribe(self, owner):
        change = asyncio.Event()
        self.sockets[owner].add(change)
        return change

    # Убирает сигнал закрытого соединения и удаляет пустой набор владельца. Иначе после новых
    # подключений оставались бы сигналы для уже отключённых устройств.
    def unsubscribe(self, owner, change):
        self.sockets[owner].discard(change)
        if not self.sockets[owner]:
            del self.sockets[owner]

    # Включает сигналы всех открытых соединений владельца. Телефоны смогут перечитать сведения об
    # удалении, как только служба закончит местную запись.
    def changed(self, owner):
        for signal in self.sockets.get(owner, ()):
            signal.set()

    # Держит запрос к основному серверу открытым до изменений или ответа по времени. Применяет
    # нужные виды удаления, затем будит телефоны. При сбое заново просит догнать изменения, чтобы
    # не пропустить уже подтверждённую границу.
    async def follow(self, adapter, key, retry_seconds):
        cursor = ""
        while True:
            try:
                page = await adapter.studio.request(
                    "GET",
                    "/v1/internal/removal-changes",
                    headers={"X-Kandinsky-Erasure-Key": key},
                    params={"cursor": cursor},
                    timeout=75,
                )
                changes = page["changes"]
                catch_up = "catch_up" in changes
                if catch_up or "accounts" in changes:
                    await adapter.sync_account_erasures(key)
                if catch_up or "results" in changes:
                    await adapter.sync_result_removals(key)
                if catch_up or "threads" in changes:
                    await erasure_sync.sync_thread_erasures(adapter, key)
                if catch_up or "contents" in changes:
                    await erasure_sync.sync_content_erasures(adapter, key)
                # Телефон должен читать журнал после записи событий удаления.
                for owner in self.sockets:
                    if catch_up or "owner:" + owner in changes:
                        self.changed(owner)
                cursor = page["cursor"]
            except (HTTPException, sqlite3.Error, ValueError, KeyError, TypeError) as error:
                log.warning("Removal delivery interrupted (%s)", type(error).__name__)
                cursor = ""
                await asyncio.sleep(retry_seconds)

    # Ждёт уведомления об удалениях под ключом конкретного владельца и включает сигнал его
    # соединения. При отзыве доступа останавливается, иначе запросы с недействующим ключом
    # продолжались бы.
    async def follow_owner(self, adapter, token, signal, retry_seconds):
        cursor = ""
        while True:
            try:
                page = await adapter.studio.request(
                    "GET",
                    "/v1/jobs/native-removal-changes",
                    token,
                    params={"cursor": cursor},
                    timeout=75,
                )
                if page["changes"]:
                    signal.set()
                cursor = page["cursor"]
            except HTTPException as error:
                if error.status_code in {401, 403}:
                    return
                cursor = ""
                await asyncio.sleep(retry_seconds)
