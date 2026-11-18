# Здесь хранится в памяти ход текущих загрузок владельца. Другой телефон видит процент, пока файл
# отправляется, а после завершения или долгой паузы эта временная запись исчезает.

"""Передаёт владельцу ход текущих загрузок без записи файла или процентов в базу."""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException

MAX_UPLOAD = 20 * 1024 * 1024


@dataclass
class UploadAttempt:
    owner: str
    operation: str
    device: str
    total: int
    mime: str
    expires: float
    received: int = 0
    notified: int = -1


class UploadDelivery:
    # Задаёт срок бездействия, часы и отдельные списки загрузок и подключений. Часы можно передать
    # при проверке, чтобы воспроизвести истечение срока без ожидания.
    def __init__(self, *, clock=time.monotonic, idle_seconds=90):
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.active = {}
        self.sockets = defaultdict(set)

    # Даёт соединению собственный сигнал об изменении загрузок владельца. Телефон ждёт этот сигнал
    # вместо периодических запросов за процентом.
    def subscribe(self, owner):
        signal = asyncio.Event()
        self.sockets[owner].add(signal)
        return signal

    # Отцепляет сигнал закрытого соединения и удаляет пустой набор. Иначе служба продолжала бы
    # хранить ссылки на уже отключённые телефоны.
    def unsubscribe(self, owner, signal):
        self.sockets[owner].discard(signal)
        if not self.sockets[owner]:
            del self.sockets[owner]

    # Включает сигналы только у соединений выбранного владельца. Тогда изменение загрузки видно на
    # его телефонах и не будит чужие устройства.
    def changed(self, owner):
        for signal in self.sockets.get(owner, ()):
            signal.set()

    # Проверяет размер файла и число одновременных загрузок. Новая попытка под тем же номером
    # заменяет старую, чтобы повтор после обрыва не оставлял на экране две строки.
    def begin(self, owner, operation, device, total, mime):
        self.expire()
        if not 0 < total <= MAX_UPLOAD:
            raise HTTPException(413, "Файл больше 20 МБ")
        key = owner, operation
        if key not in self.active:
            if len(self.active) >= 256 or sum(o == owner for o, _ in self.active) >= 8:
                raise HTTPException(429, "Слишком много одновременных загрузок")
        attempt = UploadAttempt(
            owner, operation, device, total, mime, self.clock() + self.idle_seconds
        )
        self.active[key] = attempt
        self.changed(owner)
        return attempt

    # Обновляет число принятых байтов у действующей попытки и продлевает срок её жизни. Будит
    # соединения только при смене целого процента, чтобы не отправлять событие за каждый кусок
    # файла.
    def progress(self, attempt, received):
        if self.active.get((attempt.owner, attempt.operation)) is not attempt:
            return
        attempt.expires = self.clock() + self.idle_seconds
        count = min(attempt.total, max(attempt.received, received))
        percent = count * 100 // attempt.total
        attempt.received = count
        if percent != attempt.notified:
            attempt.notified = percent
            self.changed(attempt.owner)

    # Убирает завершённую попытку и уведомляет владельца. Сверяет саму попытку, иначе завершение
    # старого запроса убрало бы новую отправку под тем же номером.
    def finish(self, attempt):
        key = attempt.owner, attempt.operation
        if self.active.get(key) is attempt:
            del self.active[key]
            self.changed(attempt.owner)

    # Находит попытки, от которых слишком долго не было данных, и закрывает их обычным способом.
    # На другом телефоне не зависает процент отправки, оборвавшейся без ответа.
    def expire(self):
        now = self.clock()
        for attempt in list(self.active.values()):
            if attempt.expires <= now:
                self.finish(attempt)

    # Сначала убирает просроченные попытки, а затем выдаёт список загрузок только нужного
    # владельца. По списку телефон может восстановить полосы хода после переподключения.
    def snapshot(self, owner):
        self.expire()
        return [
            {
                "id": item.operation,
                "deviceId": item.device,
                "received": item.received,
                "total": item.total,
                "mime": item.mime,
            }
            for (user, _), item in sorted(self.active.items())
            if user == owner
        ]
