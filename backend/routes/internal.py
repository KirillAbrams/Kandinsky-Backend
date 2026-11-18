"""Адреса запросов и подключение обработчиков этой части приложения."""

from fastapi import APIRouter, Depends, FastAPI

from app.Http.Controllers.internal_erasures_controller import ErasuresController


def register_internal_routes(app: FastAPI, *, settings, feed) -> None:
    """Связывает адреса с обработчиками этой группы."""
    controller = ErasuresController(settings, feed)
    router = APIRouter()
    router.add_api_route(
        "/v1/internal/thread-erasures",
        controller.thread_erasures,
        methods=["GET"],
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/thread-erasures/ack",
        controller.ack_thread_erasures,
        methods=["POST"],
        status_code=204,
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/content-erasures",
        controller.content_erasures,
        methods=["GET"],
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/content-erasures/ack",
        controller.ack_content_erasures,
        methods=["POST"],
        status_code=204,
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/removal-changes",
        controller.wait_for_removals,
        methods=["GET"],
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/account-erasures",
        controller.account_erasures,
        methods=["GET"],
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/account-erasures/ack",
        controller.acknowledge_account_erasures,
        methods=["POST"],
        status_code=204,
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/result-removals",
        controller.result_removals,
        methods=["GET"],
        dependencies=[Depends(controller.require_erasure_key)],
    )
    router.add_api_route(
        "/v1/internal/result-removals/ack",
        controller.acknowledge_result_removals,
        methods=["POST"],
        status_code=204,
        dependencies=[Depends(controller.require_erasure_key)],
    )
    app.include_router(router)
