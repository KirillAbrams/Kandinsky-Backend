# Загружает две начальные истории из проверочного набора при включённой настройке. Записи
# изображают уже завершённую историю, поэтому не занимают очередь исполнителя.

"""Создаёт два проверочных чата из сохранённого набора Кандинского в Алматы."""

import gzip
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import insert

from app.Models import Asset, Job, Thread

CATALOG = Path(__file__).resolve().parent / "sample_media" / "history.json.gz"
ORIGINALS_DIR = ".starter-history-almaty-v2"


# Читает сжатое описание набора и хранит его в памяти процесса. Новая регистрация использует
# прежнее описание без повторной распаковки.
@lru_cache(maxsize=1)
def history_catalog():
    with gzip.open(CATALOG, "rt", encoding="utf-8") as source:
        return json.load(source)


# Выдаёт владельцу отдельные номера файлов и чатов, связывает их с сохранёнными оригиналами и
# пишет задания порциями. При сбое отменяет запись и удаляет созданные ссылки, чтобы не оставить
# неполный набор.
def add_starter_history(db, user, media_dir):
    catalog = history_catalog()
    created = time.time() - 20_000
    linked = []
    try:
        asset_ids = {}
        assets = []
        for sha, entry in catalog["assets"].items():
            original = media_dir / ORIGINALS_DIR / entry["name"]
            asset_id = str(uuid.uuid4())
            filename = asset_id + original.suffix
            target = media_dir / filename
            os.link(original, target)
            linked.append(target)
            asset_ids[sha] = asset_id
            assets.append(
                dict(
                    id=asset_id,
                    user_id=user.id,
                    filename=filename,
                    mime=entry["mime"],
                    size=entry["size"],
                    sha256=sha,
                    preview_info=entry["preview"],
                    generated=True,
                )
            )
        db.execute(insert(Asset), assets)

        activity = time.time()
        for index, feed in enumerate(catalog["threads"]):
            thread = Thread(
                user_id=user.id,
                title=feed["title"],
                custom_title=True,
                created_at=activity - index,
                activity_at=activity - index,
            )
            db.add(thread)
            db.flush()
            jobs = []
            for position, (prompt, sha, has_media) in enumerate(feed["rows"]):
                entry = catalog["assets"][sha]
                duration = entry["preview"].get("duration_seconds")
                # В старой истории запись без файла содержит только текст. Она не поступает в очередь.
                jobs.append(
                    dict(
                        id=str(uuid.uuid4()),
                        thread_id=thread.id,
                        user_id=user.id,
                        operation_key=str(uuid.uuid4()),
                        fingerprint="0" * 64,
                        prompt=prompt,
                        model=entry["model"],
                        aspect=entry["aspect"],
                        duration_seconds=round(duration or 1),
                        seed=42,
                        status="succeeded" if has_media else "cancelled",
                        output_id=asset_ids[sha] if has_media else None,
                        created_at=created + position,
                        provider="sample",
                    )
                )
                if len(jobs) == 500:
                    db.execute(insert(Job), jobs)
                    jobs.clear()
            if jobs:
                db.execute(insert(Job), jobs)
        db.commit()
    except BaseException:
        db.rollback()
        for path in linked:
            path.unlink(missing_ok=True)
        raise
