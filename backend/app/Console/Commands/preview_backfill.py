# Добавляет маленькие обложки к сохранённым ранее результатам. Отдельный запуск позволяет
# обработать старые файлы порциями вне запроса истории.

"""Добавляет размытые кадры к результатам, сохранённым до обновления."""

import logging
import subprocess

from sqlalchemy import select, update

from app.Models import Asset, Job
from app.Services.Database.connection import database
from app.Services.Media.files import preview_metadata, tiny_preview
from config.studio import Settings

log = logging.getLogger("studio.preview_backfill")


# Читает порции файлов без обложки и строит маленькую копию из прежнего кадра или оригинала.
# Записывает только ещё пустое поле, поэтому фоновая работа не затрёт обложку, сохранённую другим
# запросом.
def backfill_previews(factory, media_dir):
    """Обходит результаты порциями, не задерживая открытие истории"""
    cursor = ""
    updated = skipped = 0
    missing = Asset.preview_info["tiny"].as_string().is_(None)
    while True:
        with factory() as db:
            rows = db.execute(
                select(Asset.id, Asset.filename, Asset.mime, Asset.preview_info)
                .where(
                    Asset.id > cursor,
                    missing,
                    select(Job.id).where(Job.output_id == Asset.id).exists(),
                )
                .order_by(Asset.id)
                .limit(64)
            ).all()
        if not rows:
            return updated, skipped
        cursor = rows[-1].id
        for asset in rows:
            try:
                info = asset.preview_info
                if info and "poster" in info:
                    info = {**info, "tiny": tiny_preview(info["poster"])}
                else:
                    info = preview_metadata(media_dir / asset.filename, asset.mime)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                log.warning("asset=%s preview=%s", asset.id, type(exc).__name__)
                skipped += 1
                continue
            with factory() as db:
                changed = db.execute(
                    update(Asset).where(Asset.id == asset.id, missing).values(preview_info=info)
                )
                db.commit()
                updated += changed.rowcount


if __name__ == "__main__":
    settings = Settings()
    engine, factory = database(settings.database_url)
    try:
        updated, skipped = backfill_previews(factory, settings.media_dir)
        print(f"updated={updated} skipped={skipped}")
    finally:
        engine.dispose()
