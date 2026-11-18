# Учитывает, где нужен исходный файл и какие уменьшенные копии от него зависят. Перед удалением
# проверяет эти связи, иначе один чат мог бы лишить другой общего файла.

"""Связывает исходники и уменьшенные копии с разговорами, которым они нужны."""

from collections.abc import Collection, Iterable

from fastapi import HTTPException
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app.Models import (
    Asset,
    AssetDerivative,
    ChatSource,
    Job,
    PendingMediaRemoval,
    Transfer,
    UploadGrant,
    User,
)
from app.Policies.ownership import active_thread, owned


def bind_source(db: Session, user: User, thread_id: str, asset_id: str) -> None:
    """Добавляет связь исходника с чатом после проверки владельца. Повтор сохраняет одну связь."""
    active_thread(db, thread_id, user)
    owned(db, Asset, asset_id, user)
    if db.get(ChatSource, (thread_id, asset_id)) is None:
        db.add(ChatSource(thread_id=thread_id, asset_id=asset_id))


def bind_derivative(db: Session, user: User, parent_id: str, child_id: str) -> None:
    """Привязывает JPEG к исходнику одного владельца и сохраняет прежнюю связь при повторе."""
    parent = owned(db, Asset, parent_id, user)
    child = owned(db, Asset, child_id, user)
    if (
        parent.id == child.id
        or child.generated
        or child.mime != "image/jpeg"
        or child.size > 512 * 1024
        or db.get(AssetDerivative, parent.id) is not None
    ):
        raise HTTPException(422, "Неверная уменьшенная копия")
    existing = db.get(AssetDerivative, child.id)
    if existing and existing.parent_id != parent.id:
        raise HTTPException(409, "Копия уже относится к другому исходнику")
    if existing is None:
        db.add(AssetDerivative(child_id=child.id, parent_id=parent.id))


def release_sources(db: Session, thread_id: str, assets: Iterable[str]) -> None:
    """Снимает связи чата с файлами, которые больше не нужны его заданиям."""
    for asset in assets:
        if not db.scalar(select(exists().where(Job.thread_id == thread_id, Job.input_id == asset))):
            db.execute(
                delete(ChatSource).where(
                    ChatSource.thread_id == thread_id, ChatSource.asset_id == asset
                )
            )


def remove_unused_assets(db: Session, owner: str, candidates: Collection[str]) -> list[str]:
    """Ставит в очередь очистку файлов владельца, у которых больше нет ссылок."""
    if not candidates:
        return []
    candidates = set(candidates)
    children = list(
        db.execute(
            select(AssetDerivative.child_id, AssetDerivative.parent_id).where(
                AssetDerivative.parent_id.in_(candidates)
            )
        )
    )
    candidates.update(child for child, _ in children)
    assets = list(
        db.scalars(
            select(Asset)
            .where(
                Asset.id.in_(candidates),
                Asset.user_id == owner,
                ~exists().where(or_(Job.input_id == Asset.id, Job.output_id == Asset.id)),
                ~exists().where(ChatSource.asset_id == Asset.id),
            )
            .with_for_update()
        )
    )
    eligible = {asset.id for asset in assets}
    parent_links = db.execute(
        select(AssetDerivative.child_id, AssetDerivative.parent_id).where(
            AssetDerivative.child_id.in_(candidates)
        )
    )
    keep_children = {child for child, parent in parent_links if parent not in eligible}
    assets = [asset for asset in assets if asset.id not in keep_children]
    ids = [asset.id for asset in assets]
    if ids:
        db.execute(delete(Transfer).where(Transfer.asset_id.in_(ids)))
        db.execute(delete(UploadGrant).where(UploadGrant.asset_id.in_(ids)))
        db.execute(
            delete(AssetDerivative).where(
                or_(AssetDerivative.child_id.in_(ids), AssetDerivative.parent_id.in_(ids))
            )
        )
        db.execute(delete(Asset).where(Asset.id.in_(ids)))
        db.add_all(PendingMediaRemoval(filename=asset.filename) for asset in assets)
    return ids
