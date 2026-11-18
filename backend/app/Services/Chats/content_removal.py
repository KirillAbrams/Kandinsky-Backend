# Удаляет чат, целый запрос либо отдельный результат и сохраняет отметки для доставки очистки.
# Общие файлы остаются, пока на них есть живые ссылки.

"""Хранит отметку удаления и освобождает файлы, если они больше никому не нужны"""

import logging
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.Models import ChatSource, Job, JobContentRemoval, PendingMediaRemoval, Thread, UploadGrant
from app.Services.Chats.erasure_records import queue_gpu_removals, remember_generations
from app.Services.Database.notifications import notify
from app.Services.Media.source_links import release_sources, remove_unused_assets

log = logging.getLogger(__name__)


def thread_summary(thread: Thread) -> dict[str, str | float | None]:
    """Возвращает поля чата для списка без сообщений."""
    return {
        "id": thread.id,
        "title": thread.title,
        "created_at": thread.created_at,
        "activity_at": thread.activity_at,
        "pinned_at": thread.pinned_at,
        "archived_at": thread.archived_at,
    }


def erase_thread(db: Session, thread: Thread, media_dir: Path) -> None:
    """Удаляет содержимое чата и сохраняет его номер для поздних повторов."""
    if thread.deleted_at is not None:
        clear_pending_media(db, media_dir)
        return
    jobs = list(db.scalars(select(Job).where(Job.thread_id == thread.id).with_for_update()))
    candidates = {value for job in jobs for value in (job.input_id, job.output_id) if value}
    candidates.update(
        db.scalars(select(ChatSource.asset_id).where(ChatSource.thread_id == thread.id))
    )
    remember_generations(db, jobs)
    queue_gpu_removals(db, jobs)
    db.execute(delete(Job).where(Job.thread_id == thread.id))
    db.execute(delete(ChatSource).where(ChatSource.thread_id == thread.id))
    db.execute(delete(UploadGrant).where(UploadGrant.thread_id == thread.id))
    removed = remove_unused_assets(db, thread.user_id, candidates)
    record_asset_removals(db, thread, thread.id, removed)
    thread.title = ""
    thread.created_at = thread.activity_at = 0
    thread.pinned_at = thread.archived_at = None
    thread.custom_title = False
    thread.deleted_at = time.time()
    thread.erasure_pending = True
    notify(db, "owner:" + thread.user_id)
    notify(db, "threads")
    notify(db, "contents")
    notify(db, "queue")
    db.commit()
    clear_pending_media(db, media_dir)


def record_asset_removals(db: Session, thread: Thread, root_id: str, assets: Iterable[str]) -> None:
    """Добавляет отметки файлов, которые телефон должен убрать после обновления истории."""
    db.add_all(
        JobContentRemoval(
            owner_id=thread.user_id,
            thread_id=thread.id,
            root_id=root_id,
            member_id=asset,
            asset_id=asset,
            prompt=False,
            asset_only=True,
            retire_answer=False,
            revoke_asset=True,
        )
        for asset in assets
    )


def remove_generation_content(
    db: Session,
    thread: Thread,
    members: Sequence[Job],
    target: Literal["result", "prompt"],
    blob: str | None,
    media_dir: Path,
) -> bool:
    """Стирает запрос или выбранный результат. Остальные работы группы при удалении результата сохраняются."""
    root = members[0]
    if target == "result":
        member = next(
            (item for item in members if blob in (item.output_id, item.removed_output_id)), None
        )
        if member is None:
            return False
        if member.removed_output_id:
            queue_gpu_removals(db, [member])
            db.commit()
            clear_pending_media(db, media_dir)
            return True
        removed = [member]
    else:
        removed = members
    old_prompt = root.prompt
    assets = set()
    output_ids = {member.output_id for member in removed if member.output_id}
    source_ids = {member.input_id for member in removed if member.input_id}
    queue_gpu_removals(db, removed)
    if target == "prompt":
        remember_generations(db, removed)
        db.execute(delete(Job).where(Job.id.in_([member.id for member in removed])))
        release_sources(db, thread.id, source_ids)
    for member in removed:
        asset = member.output_id
        if asset:
            assets.add(asset)
            if target == "result":
                member.removed_output_id = asset
                member.output_id = None
        if target == "prompt":
            if member.input_id:
                assets.add(member.input_id)
    if target == "prompt" and not thread.custom_title and thread.title == old_prompt[:80]:
        replacement = db.scalar(
            select(Job.prompt)
            .where(
                Job.thread_id == thread.id,
                Job.prompt_removed.is_(False),
                Job.id.not_in([member.id for member in members]),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        thread.title = replacement[:80] if replacement else "Новый чат"
    db.flush()
    removed_assets = set(remove_unused_assets(db, thread.user_id, assets))
    retire = target == "prompt" or not any(
        member.output_id or member.status in {"queued", "running"} for member in members
    )
    db.add_all(
        JobContentRemoval(
            owner_id=thread.user_id,
            thread_id=thread.id,
            root_id=root.id,
            member_id=member.id,
            asset_id=member.output_id if target == "prompt" else member.removed_output_id,
            prompt=target == "prompt",
            retire_answer=retire,
            revoke_asset=(member.output_id if target == "prompt" else member.removed_output_id)
            in removed_assets,
        )
        for member in removed
    )
    record_asset_removals(db, thread, root.id, removed_assets - output_ids)
    notify(db, "owner:" + thread.user_id)
    notify(db, "contents")
    notify(db, "queue")
    db.commit()
    clear_pending_media(db, media_dir)
    return True


def clear_pending_media(db: Session, media_dir: Path) -> None:
    """Удаляет файлы из очереди. Запись в базе исчезает только после успешной очистки."""
    after = ""
    while True:
        names = list(
            db.scalars(
                select(PendingMediaRemoval.filename)
                .where(PendingMediaRemoval.filename > after)
                .order_by(PendingMediaRemoval.filename)
                .limit(128)
            )
        )
        if not names:
            break
        removed = []
        for name in names:
            if Path(name).name != name:
                continue
            try:
                (media_dir / name).unlink(missing_ok=True)
                removed.append(name)
            except OSError as exc:
                log.warning("thread file cleanup failed type=%s", type(exc).__name__)
        if removed:
            db.execute(delete(PendingMediaRemoval).where(PendingMediaRemoval.filename.in_(removed)))
        db.commit()
        after = names[-1]
