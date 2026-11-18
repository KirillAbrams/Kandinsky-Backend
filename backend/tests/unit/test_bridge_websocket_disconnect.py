# Проверяет закрытие постоянного канала при обрыве и отдельно проверяет ошибки работающего канала.
# Обрыв может считаться обычным завершением, а ошибка обработчика должна оставаться заметной.

import asyncio
import logging
import socket

import pytest
import uvicorn
import websockets
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect, WebSocketState
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from bootstrap.bridge import create_app


# Запускает сценарии закрытого и ещё открытого соединения и отказа службы. Проверяет, что штатный
# обрыв завершает ожидания, а другая ошибка выходит наружу.
@pytest.mark.parametrize(
    "failure",
    [
        "closed_transport",
        "transport_closes_before_receiver",
        "connected_transport",
        "closed_adapter",
    ],
)
def test_websocket_disconnect_only_normalizes_transport_failure(tmp_path, failure):
    # Подключает управляемый канал и запоминает уже работающие задачи. После вызова обработчика
    # проверяет вид ответа и отсутствие оставшихся ожиданий именно этого подключения.
    async def scenario():
        disconnected = asyncio.Event()
        packets = []

        class Socket:
            headers = {}
            client_state = WebSocketState.CONNECTED
            reads = 0

            # Разрешает проверочному обработчику начать обмен без настоящей сети. Сценарий
            # наблюдает дальнейшее чтение и отправку отдельно.
            async def accept(self):
                pass

            # Сначала передаёт сохранённую отметку, затем ждёт сигнала обрыва и сообщает о
            # закрытии. Так проверка управляет тем, когда чтение узнает о конце связи.
            async def receive_json(self):
                self.reads += 1
                if self.reads == 1:
                    return {"type": "resume", "cursor": 0}
                await disconnected.wait()
                self.client_state = WebSocketState.DISCONNECTED
                raise WebSocketDisconnect()

            # Принимает первый ответ, а при выдаче события возвращает ошибку нужного вида.
            # Сравнение вариантов показывает, что ошибка живого канала не скрылась как обычный
            # обрыв.
            async def send_json(self, packet):
                if packet["type"] == "resumed":
                    packets.append(packet)
                    return
                if (
                    self.client_state == WebSocketState.DISCONNECTED
                    or failure == "transport_closes_before_receiver"
                ):
                    raise RuntimeError(
                        "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."
                    )
                raise RuntimeError("connected transport failed")

        class Adapter:
            # Возвращает одного проверочного владельца и его сеанс. Это убирает из сценария
            # сетевую проверку входа, оставляя проверку закрытия канала.
            async def owner(self, headers):
                return "fixture-owner", "fixture-token"

            # Завершает сверку удалений без записей. Так начальное подключение доходит до чтения
            # событий без подготовки лишних данных.
            async def sync_content_removals(self, owner, token):
                pass

            # Возвращает событие для отправки либо имитирует отказ службы после закрытия. Это
            # различает ошибку при чтении данных и ошибку из-за закрытой отправки.
            async def event_page(self, owner, token, cursor):
                if failure not in {"connected_transport", "transport_closes_before_receiver"}:
                    disconnected.set()
                    await asyncio.sleep(0)
                if failure == "closed_adapter":
                    raise RuntimeError("adapter failed")
                return {"type": "events", "cursor": 1, "events": []}

        app = create_app("http://unused.test", tmp_path / "bridge.sqlite", erasure_key="fixture")
        app.state.adapter = Adapter()
        endpoint = next(
            route.endpoint
            for route in app.routes
            if isinstance(route, WebSocketRoute) and route.path == "/v1/ws"
        )
        existing = asyncio.all_tasks()
        if failure in {"closed_transport", "transport_closes_before_receiver"}:
            await endpoint(Socket())
        else:
            message = (
                "adapter failed" if failure == "closed_adapter" else "connected transport failed"
            )
            with pytest.raises(RuntimeError, match=message):
                await endpoint(Socket())
        assert [p["type"] for p in packets] == ["resumed"]
        assert not {task for task in asyncio.all_tasks() - existing if not task.done()}

    asyncio.run(scenario())


# Запускает настоящий сервер на местном адресе и проверяет два способа обрыва. Приложение должно
# закончить обработку без сообщения об ошибке, даже если чтение ещё не узнало о закрытии.
@pytest.mark.parametrize("close_kind", ["abort", "timeout"])
def test_uvicorn_peer_close_can_precede_framework_disconnect(tmp_path, caplog, close_kind):
    # Готовит местный канал, ждёт начала обмена и вызывает обрыв или отсутствие ответа. После
    # проверки останавливает сервер, чтобы открытые соединения не остались после прогона.
    async def scenario():
        disconnected = asyncio.Event()
        handled = asyncio.Event()

        class Adapter:
            # Возвращает подготовленного владельца, чтобы проверка дошла до обмена без обращения к
            # внешнему серверу.
            async def owner(self, headers):
                return "fixture-owner", "fixture-token"

            # Пропускает пустую очередь удалений. Так момент отправки можно проверить отдельно.
            async def sync_content_removals(self, owner, token):
                pass

            # Ждёт фактического обрыва и только затем возвращает сообщение. Отправка попадает в
            # короткий промежуток до того, как приложение узнает о закрытии.
            async def event_page(self, owner, token, cursor):
                await disconnected.wait()
                return {"type": "events", "cursor": 1, "events": []}

        app = create_app("http://unused.test", tmp_path / "bridge.sqlite", erasure_key="fixture")
        app.state.adapter = Adapter()

        class DelayedTransportCallback(WebSocketsSansIOProtocol):
            # Просит настоящий обработчик завершить просроченное соединение и разрешает выдачу
            # события. Проверка не подменяет сам механизм обрыва.
            def keepalive_timeout(self):
                super().keepalive_timeout()
                disconnected.set()

            # Ненадолго задерживает вызов уведомления о потере связи. Это воспроизводит случай,
            # когда канал уже закрыт, а приложение всё ещё считает его открытым.
            def connection_lost(self, error):
                # Откладываем уведомление приложения, сохраняя настоящий порядок закрытия транспорта.
                self.loop.call_later(0.05, super().connection_lost, error)

        # Передаёт приложению чтение с управляемым моментом закрытия и отмечает конец обработки.
        # Сценарий может дождаться результата независимо от способа обрыва.
        async def delayed_receiver(scope, receive, send):
            # При настоящем сообщении о разрыве запускает выдачу события и задерживает сам ответ
            # читающему коду. Без этой паузы отправка могла бы начаться уже после известного
            # приложению закрытия.
            async def receive_after_transport_close():
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    disconnected.set()
                    # Воспроизводим чтение, которое ещё не передало событие закрытия приложению.
                    await asyncio.Event().wait()
                return message

            try:
                await app(scope, receive_after_transport_close, send)
            finally:
                handled.set()

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.setblocking(False)
        server = uvicorn.Server(
            uvicorn.Config(
                delayed_receiver,
                ws=DelayedTransportCallback if close_kind == "timeout" else "websockets-sansio",
                ws_ping_interval=0.1,
                ws_ping_timeout=0.1,
                lifespan="off",
                log_config=None,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(0.005)
                port = listener.getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{port}/v1/ws", proxy=None) as client:
                    await client.send('{"type":"resume","cursor":0}')
                    assert '"type":"resumed"' in await client.recv()
                    if close_kind == "abort":
                        client.transport.abort()
                    else:
                        client.transport.pause_reading()
                    try:
                        await handled.wait()
                    finally:
                        if close_kind == "timeout":
                            client.transport.resume_reading()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=3)
            listener.close()

    caplog.set_level(logging.ERROR, logger="uvicorn.error")
    asyncio.run(scenario())
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
