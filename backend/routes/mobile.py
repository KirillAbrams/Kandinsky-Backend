"""Адреса запросов мобильных приложений и постоянного соединения."""

from collections.abc import Callable

from fastapi import APIRouter, FastAPI

from app.Http.Controllers.mobile_accounts_controller import MobileAccountsController
from app.Http.Controllers.mobile_chats_controller import MobileChatsController
from app.Http.Controllers.mobile_media_controller import MobileMediaController
from app.Http.Controllers.mobile_socket_controller import MobileSocketController
from app.Http.Controllers.mobile_status_controller import health
from app.Http.Controllers.mobile_uploads_controller import MobileUploadsController
from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.removal_delivery import RemovalDelivery
from app.Services.Mobile.upload_delivery import UploadDelivery


def register_mobile_routes(
    app: FastAPI,
    adapter: Callable[[], Adapter],
    state: State,
    removals: RemovalDelivery,
    uploads: UploadDelivery,
    *,
    poll_seconds: float,
    erasure_key: str,
    erasure_poll_seconds: float,
) -> None:
    """Подключает готовность, обычные запросы и постоянное соединение телефонов."""
    app.add_api_route("/health", health, methods=["GET"])
    register_account_routes(app, adapter, state, erasure_key=erasure_key)
    register_chat_routes(app, adapter, state, removals, erasure_key=erasure_key)
    register_upload_routes(app, adapter, state, uploads)
    register_media_routes(app, adapter, state)
    register_websocket(
        app,
        adapter,
        state,
        removals,
        uploads,
        poll_seconds=poll_seconds,
        erasure_key=erasure_key,
        erasure_poll_seconds=erasure_poll_seconds,
    )


def register_account_routes(
    app: FastAPI, adapter: Callable[[], Adapter], state: State, *, erasure_key: str
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = MobileAccountsController(adapter, state)
    router = APIRouter()
    router.add_api_route(
        "/v1/demo/register/request",
        controller.request_registration,
        methods=["POST"],
        status_code=202,
    )
    router.add_api_route(
        "/v1/demo/password-reset/request",
        controller.request_password_reset,
        methods=["POST"],
        status_code=202,
    )
    router.add_api_route(
        "/v1/demo/password-reset/confirm",
        controller.confirm_password_reset,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/demo/register", controller.register, methods=["POST"], status_code=201
    )
    router.add_api_route("/v1/demo/login", controller.sign_in, methods=["POST"])
    router.add_api_route("/v1/devices/enroll", controller.enroll, methods=["POST"])
    router.add_api_route("/v1/kandinsky/activity", controller.user_activity, methods=["POST"])
    router.add_api_route(
        "/v1/kandinsky/logout", controller.studio_logout, methods=["POST"], status_code=204
    )
    if erasure_key:
        router.add_api_route(
            "/v1/kandinsky/account", controller.erase_account, methods=["DELETE"], status_code=204
        )
        router.add_api_route(
            "/account/delete", controller.deletion_form, methods=["GET"], include_in_schema=False
        )
        router.add_api_route(
            "/account/delete",
            controller.submit_deletion_form,
            methods=["POST"],
            include_in_schema=False,
        )
    app.include_router(router)


def register_chat_routes(
    app: FastAPI,
    adapter: Callable[[], Adapter],
    state: State,
    removals: RemovalDelivery,
    *,
    erasure_key: str,
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = MobileChatsController(adapter, state, removals, erasure_key)
    router = APIRouter()
    router.add_api_route("/v1/kandinsky/models", controller.models, methods=["GET"])
    router.add_api_route(
        "/v1/kandinsky/threads/{chat}/generations/{message}",
        controller.delete_studio_content,
        methods=["DELETE"],
        status_code=204,
    )
    router.add_api_route("/v1/kandinsky/threads", controller.studio_threads, methods=["GET"])
    router.add_api_route(
        "/v1/kandinsky/threads/{chat}", controller.change_studio_thread, methods=["PATCH"]
    )
    router.add_api_route(
        "/v1/kandinsky/threads/{chat}",
        controller.delete_studio_thread,
        methods=["DELETE"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/kandinsky/threads/{chat}/generations/{message}/action",
        controller.generation_action,
        methods=["POST"],
    )
    if erasure_key:
        router.add_api_route(
            "/v1/kandinsky/threads/{chat}/generations/{message}/report",
            controller.studio_generation_report,
            methods=["POST"],
        )
    router.add_api_route("/v1/demo/chats", controller.create_chat, methods=["POST"])
    router.add_api_route("/v1/chats", controller.chats, methods=["GET"])
    router.add_api_route("/v1/chats/states", controller.chat_states, methods=["POST"])
    router.add_api_route("/v1/bootstrap", controller.bootstrap, methods=["GET"])
    router.add_api_route("/v1/chats/{chat}/history", controller.history, methods=["GET"])
    app.include_router(router)


def register_media_routes(app: FastAPI, adapter: Callable[[], Adapter], state: State) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = MobileMediaController(adapter, state)
    router = APIRouter()
    router.add_api_route("/v1/media/{blob}", controller.media, methods=["GET"])
    app.include_router(router)


def register_upload_routes(
    app: FastAPI, adapter: Callable[[], Adapter], state: State, delivery: UploadDelivery
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = MobileUploadsController(adapter, state, delivery)
    router = APIRouter()
    router.add_api_route(
        "/v1/kandinsky/threads/{chat}/sources/{blob}",
        controller.bind_recent,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route("/v1/kandinsky/uploads/recent", controller.recent_uploads, methods=["GET"])
    router.add_api_route("/v1/uploads/{upload}", controller.upload, methods=["PUT"])
    app.include_router(router)


def register_websocket(
    app: FastAPI,
    adapter: Callable[[], Adapter],
    state: State,
    removals: RemovalDelivery,
    uploads: UploadDelivery,
    *,
    poll_seconds: float,
    erasure_key: str,
    erasure_poll_seconds: float,
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = MobileSocketController(
        adapter, state, removals, uploads, poll_seconds, erasure_key, erasure_poll_seconds
    )
    app.add_api_websocket_route("/v1/ws", controller.socket)
