# Здесь собирается служба связи между телефонами и основным сервером. В одном месте задаются
# правила обработки ошибок, подключаются команды телефонов и запускается получение сведений об
# удалениях.

"""Собирает службу связи с мобильными приложениями Кандинского."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException

from app.Exceptions.accounts import ErasedAccount
from app.Http.errors import account_erased, mobile_error_response
from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.removal_delivery import RemovalDelivery
from app.Services.Mobile.upload_delivery import UploadDelivery
from routes.mobile import register_mobile_routes


# Собирает приложение из хранилища, связи с основным сервером и обработчиков запросов. Параметры
# позволяют подставить отдельное хранилище и подменить сеть в проверках, чтобы запуск не зависел
# от действующего сервера.
def create_app(
    upstream_url: str,
    db_path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    poll_seconds: float = 2,
    erasure_key: str = "",
    erasure_poll_seconds: float = 15,
) -> FastAPI:
    state = State(db_path)
    removals = RemovalDelivery()
    uploads = UploadDelivery()

    # Открывает одно соединение с общими настройками для запросов и при наличии служебного ключа
    # запускает получение удалений. При остановке ждёт завершения отменённой задачи, иначе она
    # могла бы обратиться к уже закрытому соединению.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=upstream_url,
            transport=transport,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            app.state.adapter = Adapter(client, state)

            recovery = None
            if erasure_key:
                recovery = asyncio.create_task(
                    removals.follow(app.state.adapter, erasure_key, erasure_poll_seconds)
                )
            try:
                yield
            finally:
                if recovery is not None:
                    recovery.cancel()
                    with suppress(asyncio.CancelledError):
                        await recovery

    app = FastAPI(title="Нативный стенд Кандинского", lifespan=lifespan)
    app.add_exception_handler(HTTPException, mobile_error_response)
    app.add_exception_handler(ErasedAccount, account_erased)

    # Берёт общий объект связи, созданный при запуске. Обработчики получают его только при
    # запросе, когда соединение с основным сервером уже открыто.
    def adapter() -> Adapter:
        return app.state.adapter

    register_mobile_routes(
        app,
        adapter,
        state,
        removals,
        uploads,
        poll_seconds=poll_seconds,
        erasure_key=erasure_key,
        erasure_poll_seconds=erasure_poll_seconds,
    )
    return app


# Читает адрес основного сервера и путь к служебной базе при запуске контейнера. Создаёт закрытую
# для других пользователей папку, чтобы хранилище можно было открыть при первом запуске.
def from_environment() -> FastAPI:
    origin = os.environ["KANDINSKY_UPSTREAM"]
    state = Path(os.environ["KANDINSKY_BRIDGE_STATE"])
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return create_app(origin, state, erasure_key=os.environ.get("KANDINSKY_BRIDGE_ERASURE_KEY", ""))
