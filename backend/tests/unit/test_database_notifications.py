# Проверяет переподключение, восстановление после потери старой отметки и конец ожидания без
# изменений. Настоящая сеть и длительное ожидание заменены управляемыми ответами.

import asyncio
from types import SimpleNamespace

import psycopg
import pytest

from app.Services.Database import notifications


# Имитирует отказ первого подключения и появление события после второго. Проверяет, что команда
# догнать историю уходит только после подписки и раньше нового события.
def test_notifications_reconnect_and_publish_catch_up_before_next_change(monkeypatch):
    published = []
    attempts = []

    class Connection:
        # Отдаёт подготовленное соединение при входе в блок подписки. Настоящее подключение для
        # этой проверки не открывается.
        async def __aenter__(self):
            return self

        # Завершает блок соединения без дополнительной работы. Отмена проходит наружу, чтобы
        # сценарий мог остановить слушатель после нужного события.
        async def __aexit__(self, *_):
            pass

        # Сверяет, что слушатель подписался на ожидаемую очередь. Так один успешный ответ
        # подключения не заменит проверку подписки.
        async def execute(self, query):
            assert query == "LISTEN studio_changes"

        # Возвращает одно уведомление и затем отменяет ожидание. Этого достаточно, чтобы проверить
        # порядок отправки и закончить бесконечный слушатель.
        async def notifies(self):
            yield SimpleNamespace(payload="owner:fixture")
            raise asyncio.CancelledError

    # Проверяет адрес и режим записи, записывает попытку и ломает первое подключение. По числу
    # вызовов видно, что слушатель действительно повторил подключение.
    async def connect(dsn, *, autocommit):
        assert dsn == "postgresql://local@fixture.test/studio"
        assert autocommit
        attempts.append(dsn)
        if len(attempts) == 1:
            raise psycopg.OperationalError("isolated connection was lost")
        return Connection()

    monkeypatch.setattr(notifications.psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            notifications.listen("postgresql+psycopg://local@fixture.test/studio", published.append)
        )
    assert len(attempts) == 2
    assert published == ["catch_up", "owner:fixture"]


# Вытесняет старые отметки владельцев и обычные события новыми. Проверяет, что устаревший читатель
# получит просьбу догнать очередь, а свежий сможет продолжить обычное чтение.
def test_notifications_recover_from_evicted_owner_and_history_cursors():
    feed = notifications.RemovalFeed()
    expired = feed.snapshot("", "owner:old")["cursor"]
    for index in range(2048):
        feed.snapshot("", f"owner:fixture:{index}")
    assert feed.snapshot(expired, "owner:old")["changes"] == ["catch_up"]
    active = feed.snapshot("", "owner:active")["cursor"]
    assert feed.snapshot(active, "owner:active")["changes"] == []

    empty = feed.snapshot("", None)["cursor"]
    feed.publish("contents")
    assert feed.snapshot(empty, None)["changes"] == ["contents"]
    for index in range(513):
        feed.publish(f"fixture:{index}")
    assert feed.snapshot(empty, None)["changes"] == ["catch_up"]


# Имитирует истечение ожидания без сигнала. Отметка должна сохраниться, а запись ожидающего
# удалиться, чтобы повторное подключение не накапливало память.
def test_notification_long_poll_can_expire_without_changing_cursor(monkeypatch):
    # Сверяет срок ожидания и сразу возвращает его истечение. Проверка не проводит минуту в
    # ожидании настоящих часов.
    async def wait_for(signal, timeout):
        assert timeout == 60
        signal.close()
        raise TimeoutError

    monkeypatch.setattr(notifications.asyncio, "wait_for", wait_for)
    feed = notifications.RemovalFeed()
    initial = feed.snapshot("", None)["cursor"]
    assert asyncio.run(feed.wait(initial)) == {"cursor": initial, "changes": []}
    assert not feed.waiters
