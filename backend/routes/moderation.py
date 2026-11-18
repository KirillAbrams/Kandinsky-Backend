"""Адреса закрытой службы проверки содержимого."""

from fastapi import Depends, FastAPI

from app.Http.Controllers.moderation_controller import ModerationController


def register_moderation_routes(app: FastAPI, token: str) -> None:
    """Подключает обработчики с общей проверкой доступа."""
    controller = ModerationController(app, token)
    access = [Depends(controller.authorize)]
    app.add_api_route("/health", controller.health, methods=["GET"], dependencies=access)
    app.add_api_route("/v1/text", controller.text, methods=["POST"], dependencies=access)
    app.add_api_route("/v1/images", controller.images, methods=["POST"], dependencies=access)
