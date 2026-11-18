"""Помечает ответы номером запроса и пишет в журнал результат и время."""

import logging
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint

log = logging.getLogger("studio.api")


async def request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Связывает ответ с записью журнала и защищает его от сохранения по пути к клиенту."""
    start = time.monotonic()
    request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    log.info(
        "request id=%s method=%s status=%s ms=%.1f",
        request_id,
        request.method,
        response.status_code,
        (time.monotonic() - start) * 1000,
    )
    return response
