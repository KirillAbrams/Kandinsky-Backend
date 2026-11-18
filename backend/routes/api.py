"""Адреса основного сервера для аккаунтов, чатов, файлов и генерации."""

from logging import Logger

from fastapi import APIRouter, FastAPI

from app.Http.Controllers.assets_controller import AssetsController
from app.Http.Controllers.auth_controller import AuthenticationController
from app.Http.Controllers.generations_controller import GenerationsController
from app.Http.Controllers.password_reset_controller import PasswordResetController
from app.Http.Controllers.status_controller import health, wait_for_owner_removals
from app.Http.Controllers.threads_controller import ChatsController
from app.Http.Resources.chats import HistoryPage
from app.Http.Resources.generation import GenerationReportResponse, JobResponse
from app.Services.Accounts.email_codes import CodeSender
from app.Services.Database.notifications import RemovalFeed
from config.studio import Settings
from routes.internal import register_internal_routes


def register_studio_routes(
    app: FastAPI,
    *,
    settings: Settings,
    feed: RemovalFeed,
    log: Logger,
    send_recovery: CodeSender | None,
    send_registration: CodeSender | None,
) -> None:
    """Подключает открытые и служебные адреса в порядке проверки маршрутов."""
    app.add_api_route("/v1/jobs/native-removal-changes", wait_for_owner_removals, methods=["GET"])
    register_password_reset(app, settings, send_recovery)
    app.add_api_route("/health", health, methods=["GET"])
    register_auth_routes(app, settings=settings, log=log, send_registration=send_registration)
    register_internal_routes(app, settings=settings, feed=feed)
    register_threads_routes(app, settings=settings)
    register_generations_routes(app, settings=settings)
    register_assets_routes(app, settings=settings)


def register_auth_routes(
    app: FastAPI, *, settings: Settings, log: Logger, send_registration: CodeSender | None
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = AuthenticationController(settings, log, send_registration)
    router = APIRouter()
    router.add_api_route(
        "/v1/auth/register/request", controller.request_code, methods=["POST"], status_code=202
    )
    router.add_api_route(
        "/v1/auth/register", controller.register, methods=["POST"], status_code=201
    )
    router.add_api_route("/v1/auth/login", controller.login, methods=["POST"])
    router.add_api_route("/v1/auth/activity", controller.activity, methods=["POST"])
    router.add_api_route("/v1/auth/me", controller.me, methods=["GET"])
    router.add_api_route(
        "/v1/auth/me", controller.delete_account, methods=["DELETE"], status_code=204
    )
    router.add_api_route("/v1/auth/logout", controller.logout, methods=["POST"], status_code=204)
    app.include_router(router)


def register_assets_routes(app: FastAPI, *, settings: Settings) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = AssetsController(settings)
    router = APIRouter()
    router.add_api_route("/v1/uploads/ticket", controller.upload_ticket, methods=["POST"])
    router.add_api_route("/v1/uploads", controller.recent_uploads, methods=["GET"])
    router.add_api_route(
        "/v1/uploads/{ticket}", controller.background_upload, methods=["POST"], status_code=201
    )
    router.add_api_route("/v1/assets", controller.upload, methods=["POST"], status_code=201)
    router.add_api_route(
        "/v1/threads/{thread_id}/sources/{asset_id}",
        controller.bind_chat_source,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/assets/{parent_id}/previews/{child_id}",
        controller.link_existing_preview,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/assets/{asset_id}/metadata", controller.asset_metadata, methods=["GET"]
    )
    router.add_api_route(
        "/v1/assets/{asset_id}/ticket", controller.transfer_ticket, methods=["POST"]
    )
    router.add_api_route("/v1/transfers/{ticket}", controller.transfer_content, methods=["GET"])
    router.add_api_route("/v1/assets/{asset_id}", controller.asset_content, methods=["GET"])
    app.include_router(router)


def register_generations_routes(app: FastAPI, *, settings: Settings) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = GenerationsController(settings)
    router = APIRouter()
    router.add_api_route("/v1/models", controller.models, methods=["GET"])
    router.add_api_route(
        "/v1/jobs", controller.submit, methods=["POST"], response_model=JobResponse, status_code=201
    )
    router.add_api_route(
        "/v1/jobs/batch",
        controller.submit_batch,
        methods=["POST"],
        response_model=list[JobResponse],
        status_code=201,
    )
    router.add_api_route("/v1/jobs/native-removals", controller.native_removals, methods=["GET"])
    router.add_api_route(
        "/v1/jobs/native-removals/ack",
        controller.ack_native_removals,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/contents",
        controller.delete_job_content,
        methods=["DELETE"],
        status_code=204,
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/batch",
        controller.get_batch,
        methods=["GET"],
        response_model=list[JobResponse],
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/batch/cancel",
        controller.cancel_batch,
        methods=["POST"],
        response_model=list[JobResponse],
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/retry",
        controller.retry_failed_job,
        methods=["POST"],
        response_model=JobResponse,
    )
    router.add_api_route(
        "/v1/jobs/{job_id}", controller.get_job, methods=["GET"], response_model=JobResponse
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/report",
        controller.report_generation,
        methods=["POST"],
        response_model=GenerationReportResponse,
    )
    router.add_api_route(
        "/v1/jobs/{job_id}/cancel", controller.cancel, methods=["POST"], response_model=JobResponse
    )
    app.include_router(router)


def register_threads_routes(app: FastAPI, *, settings: Settings) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = ChatsController(settings)
    router = APIRouter()
    router.add_api_route("/v1/threads", controller.create_thread, methods=["POST"], status_code=201)
    router.add_api_route("/v1/threads", controller.threads, methods=["GET"])
    router.add_api_route("/v1/threads/states", controller.thread_states, methods=["POST"])
    router.add_api_route(
        "/v1/threads/deletions",
        controller.thread_deletions,
        methods=["POST"],
        response_model=list[str],
    )
    router.add_api_route("/v1/threads/{thread_id}", controller.change_thread, methods=["PATCH"])
    router.add_api_route(
        "/v1/threads/{thread_id}", controller.delete_thread, methods=["DELETE"], status_code=204
    )
    router.add_api_route(
        "/v1/threads/{thread_id}/jobs",
        controller.thread_jobs,
        methods=["GET"],
        response_model=list[JobResponse],
    )
    router.add_api_route(
        "/v1/threads/{thread_id}/history",
        controller.paged_history,
        methods=["GET"],
        response_model=HistoryPage,
    )
    app.include_router(router)


def register_password_reset(
    app: FastAPI, settings: Settings, sender: CodeSender | None = None
) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = PasswordResetController(settings, sender)
    app.add_api_route(
        "/v1/auth/password-reset/request",
        controller.request_code,
        methods=["POST"],
        status_code=202,
    )
    app.add_api_route(
        "/v1/auth/password-reset/confirm",
        controller.confirm_code,
        methods=["POST"],
        status_code=204,
    )
