"""Готовность службы связи и её соединения с основным сервером."""

from fastapi import Request


# Подтверждает доступность основного сервера через общее соединение.
async def health(request: Request):
    return {
        "ok": True,
        "upstream": await request.app.state.adapter.studio.request("GET", "/health"),
    }
