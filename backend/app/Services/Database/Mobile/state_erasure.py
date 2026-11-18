# Здесь стираются копии удалённых чатов и файлов из служебной базы. Остаточные записи нужны только
# для того, чтобы запоздавший ответ не вернул удалённое в историю.

"""Стирает выбранные данные SQLite и сохраняет только запрет на их повторную запись."""

import json
import sqlite3
import time


# Подтверждает изменения и несколько раз пробует сократить журнал записи базы. Если активный
# читатель мешает очистке, сообщает об ошибке, чтобы вышестоящий обработчик мог повторить
# операцию.
def checkpoint(db):
    db.commit()
    db.execute("PRAGMA busy_timeout=0")
    for pause in (0, 0.005, 0.015, 0.045):
        if pause:
            time.sleep(pause)
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not busy:
            return
    raise sqlite3.OperationalError("Erasure waits for active readers")


# Собирает номера файлов и их обложек. Убирает связанные записи и запоминает удалённые файлы и
# загрузки, чтобы повтор старой операции не сделал их снова доступными.
def erase_asset_rows(db, owner, media):
    blobs = {value for item in media for value in item.values() if value}
    for blob in blobs:
        db.execute("INSERT OR IGNORE INTO removed_media VALUES (?,?)", (owner, blob))
        db.execute(
            "DELETE FROM previews WHERE owner=? AND (blob=? OR replace(asset,'-','')=?)",
            (owner, blob, blob),
        )
        db.execute(
            "INSERT OR IGNORE INTO removed_uploads SELECT owner,operation FROM studio_uploads WHERE owner=? AND blob=?",
            (owner, blob),
        )
        db.execute("DELETE FROM studio_uploads WHERE owner=? AND blob=?", (owner, blob))


# В одной записи в базу помечает файлы удалёнными и убирает их связи. Завершает очисткой журнала,
# чтобы в нём не оставались прежние значения после успешного удаления.
def erase_assets(state, owner, media):
    with state.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        erase_asset_rows(db, owner, media)
        checkpoint(db)


# Отмечает чат удалённым, убирает ожидающие задания и вычищает содержимое сообщений и журнала. При
# первом удалении добавляет событие для телефонов, чтобы они тоже убрали чат.
def erase_chat(state, owner, chat):
    with state.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        was_erased = db.execute(
            "SELECT 1 FROM erased_chats WHERE owner=? AND chat=?", (owner, chat)
        ).fetchone()
        db.execute("INSERT OR IGNORE INTO erased_chats VALUES (?,?)", (owner, chat))
        db.execute(
            "DELETE FROM pending WHERE owner=? AND id IN (SELECT job FROM messages WHERE owner=? AND chat=?)",
            (owner, owner, chat),
        )
        db.execute(
            "INSERT OR IGNORE INTO deleted_messages SELECT owner,id FROM messages WHERE owner=? AND chat=?",
            (owner, chat),
        )
        db.execute("UPDATE messages SET body='{}' WHERE owner=? AND chat=?", (owner, chat))
        event = {"chatId": chat, "kind": "kandinsky.retired"}
        db.execute(
            "UPDATE events SET body=? WHERE owner=? AND json_extract(body,'$.chatId')=?",
            (json.dumps(event), owner, chat),
        )
        if not was_erased:
            state.event(db, owner, event)
        checkpoint(db)
