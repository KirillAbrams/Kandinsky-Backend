# Проверяет видимость хода отправки, повтор операции и предел числа загрузок. Проверка работает с
# памятью службы без записи файлов.

"""Прогресс исходного файла виден только владельцу и относится к текущей попытке."""

import asyncio

import pytest
from fastapi import HTTPException

from app.Services.Mobile.upload_delivery import UploadDelivery


# Запускает проверку владельца и повтора в одном цикле ожидания. Это позволяет наблюдать сигналы,
# которыми служба сообщает изменения подключённым телефонам.
def test_partial_upload_progress_is_owner_scoped_and_stale_finish_cannot_clear_retry():
    # Подписывает владельца и другого пользователя, затем заменяет отправку с тем же номером.
    # Сравнивает сведения и сигналы, чтобы старая попытка не убрала новую и чужой владелец ничего
    # не увидел.
    async def scenario():
        delivery = UploadDelivery()
        owner = delivery.subscribe("owner")
        foreign = delivery.subscribe("foreign")
        first = delivery.begin("owner", "operation", "phone", 1000, "image/jpeg")
        delivery.progress(first, 250)
        assert owner.is_set()
        assert not foreign.is_set()
        assert delivery.snapshot("foreign") == []
        assert delivery.snapshot("owner") == [
            {
                "id": "operation",
                "deviceId": "phone",
                "received": 250,
                "total": 1000,
                "mime": "image/jpeg",
            }
        ]
        retry = delivery.begin("owner", "operation", "phone", 1000, "image/jpeg")
        delivery.progress(retry, 500)
        owner.clear()
        delivery.progress(first, 900)
        assert not owner.is_set()
        assert delivery.snapshot("owner")[0]["received"] == 500
        delivery.finish(first)
        assert delivery.snapshot("owner")[0]["received"] == 500
        delivery.finish(retry)
        assert delivery.snapshot("owner") == []
        delivery.unsubscribe("owner", owner)
        delivery.unsubscribe("foreign", foreign)

    asyncio.run(scenario())


# Запускает проверку показаний и истечения срока с подставными часами. Реального ожидания
# бездействия не требуется.
def test_progress_is_bounded_monotone_and_expired_uploads_are_removed():
    # Передаёт растущие, меньшие и слишком большие значения и переводит часы за предел
    # бездействия. Ход не уменьшается и не превышает размер, а истёкшая строка исчезает с
    # уведомлением.
    async def scenario():
        now = [10.0]
        delivery = UploadDelivery(clock=lambda: now[0], idle_seconds=90)
        signal = delivery.subscribe("owner")
        handle = delivery.begin("owner", "operation", "phone", 1000, "image/jpeg")
        delivery.progress(handle, 250)
        signal.clear()
        delivery.progress(handle, 200)
        assert not signal.is_set()
        assert delivery.snapshot("owner")[0]["received"] == 250
        delivery.progress(handle, 2000)
        assert delivery.snapshot("owner")[0]["received"] == 1000
        signal.clear()
        now[0] += 91
        delivery.expire()
        assert signal.is_set()
        assert delivery.snapshot("owner") == []

    asyncio.run(scenario())


# Занимает все места владельца и повторяет одну из отправок, затем пробует лишнюю и слишком
# большую. Повтор не расходует место, а остальные отказы различаются.
def test_upload_capacity_is_bounded_and_replacing_an_operation_does_not_consume_a_slot():
    delivery = UploadDelivery()
    for index in range(8):
        delivery.begin("owner", str(index), "phone", 1000, "image/jpeg")
    delivery.begin("owner", "0", "phone", 1000, "image/jpeg")
    with pytest.raises(HTTPException) as full:
        delivery.begin("owner", "overflow", "phone", 1000, "image/jpeg")
    assert full.value.status_code == 429
    with pytest.raises(HTTPException) as oversized:
        delivery.begin("other", "too-large", "phone", 20 * 1024 * 1024 + 1, "image/jpeg")
    assert oversized.value.status_code == 413
    assert len(delivery.snapshot("owner")) == 8


# Занимает общий предел разными владельцами и проверяет отказ новому. После завершения одной
# работы место должно сразу стать доступным.
def test_global_capacity_covers_all_owners_and_recovers_after_a_transfer_finishes():
    delivery = UploadDelivery()
    first = None
    for index in range(256):
        attempt = delivery.begin(str(index), "source", "phone", 1000, "image/jpeg")
        if first is None:
            first = attempt
    with pytest.raises(HTTPException) as full:
        delivery.begin("new-owner", "source", "phone", 1000, "image/jpeg")
    assert full.value.status_code == 429
    delivery.finish(first)
    delivery.begin("new-owner", "source", "phone", 1000, "image/jpeg")
    assert len(delivery.snapshot("new-owner")) == 1
