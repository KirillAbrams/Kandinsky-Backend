"""Ответы об ошибках для телефонов и закрытого модератора."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.Exceptions.accounts import ErasedAccount


async def mobile_error_response(_request: Request, error: HTTPException) -> JSONResponse:
    """Передаёт код ошибки в ожидаемом телефоном поле."""
    return JSONResponse({"error": {"code": str(error.detail)}}, status_code=error.status_code)


async def account_erased(_request: Request, _error: ErasedAccount) -> JSONResponse:
    """Отличает удалённый аккаунт от обычного отказа во входе."""
    return JSONResponse({"error": {"code": "account_deleted"}}, status_code=401)


async def invalid_classifier_input(
    _request: Request, _error: RequestValidationError
) -> JSONResponse:
    """Возвращает отказ без исходного текста и изображения."""
    return JSONResponse({"detail": "Invalid classifier input"}, status_code=422)
