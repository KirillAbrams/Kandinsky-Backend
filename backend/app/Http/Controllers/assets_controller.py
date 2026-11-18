# Подключает отправку, выдачу и чтение сведений о файлах. Временные разрешения отделены от
# обычного входа и дают доступ только к нужной операции.

"""Запросы загрузки и выдачи файлов"""

import uuid

from fastapi import UploadFile
from fastapi.responses import FileResponse

from app.Http.dependencies import CurrentUser, DatabaseSession
from app.Http.Resources.media import asset_summary
from app.Models import Asset
from app.Policies.ownership import owned
from app.Services.Media.assets import asset_for_transfer, recent_uploads
from app.Services.Media.background_upload import accept_background_upload
from app.Services.Media.issue_ticket import (
    TRANSFER_TICKET_SECONDS,
    UPLOAD_TICKET_SECONDS,
    issue_transfer_ticket,
    issue_upload_ticket,
)
from app.Services.Media.link_assets import link_asset_preview, link_chat_source
from app.Services.Media.prepare_metadata import prepare_asset_metadata
from app.Services.Media.upload_asset import upload_asset
from config.studio import Settings


class AssetsController:
    """Оформляет ответы на загрузку, сведения о файле и скачивание."""

    # Собирает команды для файлов вокруг общей проверки владельца и папки хранения. Обработчики
    # получают одинаковые пределы и путь, куда складываются файлы.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # Проверяет владельца и чат, убирает истёкшие пустые разрешения и выдаёт новый ключ в пределах
    # лимита. Предел не даёт накапливать слишком много незавершённых отправок.
    @staticmethod
    def upload_ticket(user: CurrentUser, db: DatabaseSession, thread_id: uuid.UUID | None = None):
        ticket = issue_upload_ticket(db, user, thread_id)
        return {"path": "/v1/uploads/" + ticket, "expires_in": UPLOAD_TICKET_SECONDS}

    # Находит недавно завершённые отправки владельца и отдаёт номера, размеры и типы. Клиент может
    # повторно выбрать файл без новой передачи его содержимого.
    @staticmethod
    def recent_uploads(user: CurrentUser, db: DatabaseSession):
        return [asset_summary(asset) for asset in recent_uploads(db, user)]

    # Передаёт открытый файл, временный ключ и предел размера службе фоновой отправки. Возвращает
    # уже сохранённую запись и при первом вызове, и при одинаковом повторе.
    def background_upload(self, ticket: str, db: DatabaseSession, file: UploadFile):
        asset = accept_background_upload(
            db,
            ticket,
            file.file,
            file.content_type,
            max_upload_bytes=self._settings.max_upload_bytes,
            media_dir=self._settings.media_dir,
        )
        return asset_summary(asset)

    # Проверяет владельца оригинала при добавлении обложки, ограничивает чтение и пишет запись о
    # файле. При сбое записи убирает физический файл, чтобы он не остался без владельца.
    def upload(
        self,
        user: CurrentUser,
        db: DatabaseSession,
        file: UploadFile,
        parent: uuid.UUID | None = None,
    ):
        asset = upload_asset(
            db,
            user,
            file.file,
            file.content_type,
            parent,
            max_upload_bytes=self._settings.max_upload_bytes,
            media_dir=self._settings.media_dir,
        )
        return asset_summary(asset)

    # Под блокировкой владельца связывает ранее загруженный файл с открытым чатом. Без связи
    # очистка другого чата могла бы считать исходник свободным.
    @staticmethod
    def bind_chat_source(thread_id: str, asset_id: str, user: CurrentUser, db: DatabaseSession):
        link_chat_source(db, user, thread_id, asset_id)

    # Привязывает уже сохранённую уменьшенную копию к оригиналу после проверок. Это позволяет
    # переиспользовать файл и учитывать его при очистке, не отправляя байты снова.
    @staticmethod
    def link_existing_preview(
        parent_id: str, child_id: str, user: CurrentUser, db: DatabaseSession
    ):
        link_asset_preview(db, user, parent_id, child_id)

    # Проверяет доступ и при первом чтении вычисляет недостающие отпечаток и кадр. Повтор
    # использует сохранённые сведения. По отдельному запросу может вернуть более удобное для ленты
    # фото.
    def asset_metadata(
        self, asset_id: str, user: CurrentUser, db: DatabaseSession, display_preview: bool = False
    ):
        return prepare_asset_metadata(
            db, user, asset_id, self._settings.media_dir, display_preview=display_preview
        )

    # Проверяет право на файл и сохраняет короткоживущий отпечаток нового разрешения. Так
    # скачивающему процессу можно выдать доступ к одному файлу без полного ключа входа.
    def transfer_ticket(self, asset_id: str, user: CurrentUser, db: DatabaseSession):
        ticket = issue_transfer_ticket(db, user, asset_id)
        return {"path": "/v1/transfers/" + ticket, "expires_in": TRANSFER_TICKET_SECONDS}

    # Находит действующую выдачу по отпечатку и отдаёт только связанный файл. Истечение срока и
    # удаление файла одинаково прекращают доступ.
    def transfer_content(self, ticket: str, db: DatabaseSession):
        asset = asset_for_transfer(db, ticket)
        return FileResponse(self._settings.media_dir / asset.filename, media_type=asset.mime)

    # Проверяет владельца и выдаёт файл с нужным типом для показа внутри приложения. Ответ
    # поддерживает чтение участка, поэтому перемотка ролика не требует полного скачивания.
    def asset_content(self, asset_id: str, user: CurrentUser, db: DatabaseSession):
        asset = owned(db, Asset, asset_id, user)
        return FileResponse(
            self._settings.media_dir / asset.filename,
            media_type=asset.mime,
            filename=asset.filename,
            content_disposition_type="inline",
        )
