# Передаёт короткие сигналы о зафиксированных изменениях. Таблицы остаются источником данных,
# поэтому после разрыва соединения читатель может догнать очередь.

"""PostgreSQL доставляет сигнал при фиксации транзакции. Сами данные остаются в таблицах."""

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock
from typing import TypedDict
from uuid import uuid4

import psycopg
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)


def notify(db: Session, change: str) -> None:
    """Доставляет отметку об изменении после фиксации транзакции."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_notify('studio_changes', :change)"), {"change": change})
    else:
        db.info.setdefault("changes", set()).add(change)


def forward_local_commits(factory: sessionmaker[Session], feed: "RemovalFeed") -> None:
    # Забирает накопленные сигналы только после успешной записи и отправляет их ожидающим. Удаляет
    # их из сеанса, чтобы при следующей записи они не ушли повторно.
    """Фиксация в местной базе отправляет накопленные сигналы. Откат их сбрасывает."""

    @event.listens_for(factory, "after_commit")
    def committed(db: Session) -> None:
        for change in db.info.pop("changes", set()):
            feed.publish(change)

    # Стирает сигналы отменённой записи. Иначе следующая успешная запись отправила бы уведомление
    # об изменении, которого в базе нет.
    @event.listens_for(factory, "after_rollback")
    def rolled_back(db: Session) -> None:
        db.info.pop("changes", None)


async def listen(database_url: str, publish: Callable[[str], None]) -> None:
    """Подписывается на сигналы PostgreSQL и запрашивает обновления после переподключения."""
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
                await connection.execute("LISTEN studio_changes")
                # Подписка уже действует. Теперь можно прочитать накопленное за время разрыва.
                publish("catch_up")
                async for notice in connection.notifies():
                    publish(notice.payload)
        except psycopg.Error as error:
            log.warning("PostgreSQL notification connection lost (%s)", type(error).__name__)
            await asyncio.sleep(1)


class RemovalSnapshot(TypedDict):
    cursor: str
    changes: list[str]


class RemovalFeed:
    def __init__(self) -> None:
        """Создаёт поток уведомлений с памятью последних событий."""
        self.epoch = uuid4().hex
        self.sequence = 0
        self.changes = deque(maxlen=512)
        self.waiters = {}
        self.owner_versions = OrderedDict()
        self.lock = Lock()

    def publish(self, change: str) -> None:
        """Будит читателей нужного потока и обновляет отметку событий."""
        if change == "queue":
            return
        with self.lock:
            self.sequence += 1
            self.changes.append((self.sequence, change))
            if change == "catch_up":
                self.owner_versions.clear()
            elif change.startswith("owner:"):
                self.owner_versions.pop(change, None)
            for signal, (loop, topic) in self.waiters.items():
                if topic is None or change in {topic, "catch_up"}:
                    loop.call_soon_threadsafe(signal.set)

    def snapshot(self, cursor: str, topic: str | None) -> RemovalSnapshot:
        """Возвращает изменения или сигнал перечитать данные, если отметка устарела."""
        if topic is not None:
            version = self.owner_versions.get(topic)
            if version is None:
                version = uuid4().hex
                self.owner_versions[topic] = version
                if len(self.owner_versions) > 2048:
                    self.owner_versions.popitem(last=False)
            else:
                self.owner_versions.move_to_end(topic)
            return {"cursor": version, "changes": [] if cursor == version else ["catch_up"]}
        current = f"{self.epoch}:{self.sequence}"
        epoch, _, number = cursor.partition(":")
        if cursor == current:
            return {"cursor": current, "changes": []}
        if epoch == self.epoch and number.isdecimal() and len(number) <= 20 and self.changes:
            position = int(number)
            if self.changes[0][0] - 1 <= position < self.sequence:
                changes = {change for seq, change in self.changes if seq > position}
                return {"cursor": current, "changes": sorted(changes)}
        return {"cursor": current, "changes": ["catch_up"]}

    async def wait(self, cursor: str, topic: str | None = None) -> RemovalSnapshot:
        """Ждёт новых событий и снимает ожидание при ответе, отмене или истечении времени."""
        signal = asyncio.Event()
        with self.lock:
            result = self.snapshot(cursor, topic)
            if result["changes"]:
                return result
            self.waiters[signal] = (asyncio.get_running_loop(), topic)
        try:
            try:
                await asyncio.wait_for(signal.wait(), 60)
            except TimeoutError:
                pass
            with self.lock:
                return self.snapshot(cursor, topic)
        finally:
            with self.lock:
                del self.waiters[signal]
