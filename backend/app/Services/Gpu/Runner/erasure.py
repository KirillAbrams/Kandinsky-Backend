# Хранит отметки о стёртых заданиях и управляет запущенными попытками. Отметки нужны, чтобы
# запоздавший запрос с прежним номером не создал файлы заново.

"""Останавливает выбранную работу и стирает её попытки без возможности повторного запуска."""

import os
import shutil
import signal
import subprocess
import threading


class JobErasure:
    # Получает путь и способ открыть базу от службы заданий. Общая блокировка и список процессов
    # позволяют стирать работу согласованно с её запуском и завершением.
    def __init__(self, root, connect):
        self.root = root
        self.connect = connect
        self.lock = threading.RLock()
        self.processes = {}

    # Готовит таблицу отметок удаления. Если старая база хранила только номер попытки, добавляет
    # исходный номер, чтобы после обновления заметить и убрать связанные попытки.
    @staticmethod
    def prepare(db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS erased_jobs(id TEXT PRIMARY KEY, canonical TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_erased_canonical ON erased_jobs(canonical)")
        if "canonical" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
            db.execute("ALTER TABLE jobs ADD COLUMN canonical TEXT")
            db.execute("UPDATE jobs SET canonical=id WHERE canonical IS NULL")

    # Ищет отметку удаления по исходному номеру или номеру попытки. Оба варианта проверяются,
    # чтобы старый и новый формат команды одинаково блокировали повтор.
    @staticmethod
    def contains(db, identity):
        return (
            db.execute(
                "SELECT 1 FROM erased_jobs WHERE id=? OR canonical=?", (identity, identity)
            ).fetchone()
            is not None
        )

    # Просит работающий процесс и его потомков выйти, ждёт короткое время и при необходимости
    # останавливает принудительно. Иначе удаляемая работа могла бы продолжить писать файлы.
    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except ProcessLookupError:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)

    # Убирает папку попытки после остановки. Если на её месте ссылка, удаляет только саму ссылку,
    # чтобы очистка не затронула файлы по чужому пути.
    def remove_folder(self, identity):
        path = self.root() / identity
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    # Собирает все известные попытки, закрывает их для новых запусков, останавливает процессы и
    # стирает строки и файлы. Если чтение базы пока мешает очистить журнал, сообщает об ошибке,
    # чтобы операция могла быть повторена.
    def erase(self, canonical, known_run=None):
        aliases = [canonical] + ([known_run] if known_run else [])
        with self.lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in aliases)
                rows = db.execute(
                    f"SELECT id,canonical FROM jobs WHERE canonical=? OR id IN ({placeholders})",
                    (canonical, *aliases),
                ).fetchall()
                # Старый исполнитель знал только номер попытки. Новый не позволяет перепривязать чужую работу.
                if any(row["canonical"] not in {row["id"], canonical} for row in rows):
                    raise ValueError("Attempt belongs to another generation")
                prior = db.execute(
                    f"SELECT id FROM erased_jobs WHERE canonical=? OR id IN ({placeholders})",
                    (canonical, *aliases),
                ).fetchall()
                identities = (
                    set(aliases) | {row["id"] for row in rows} | {row["id"] for row in prior}
                )
                db.executemany(
                    "INSERT OR IGNORE INTO erased_jobs VALUES (?,?)",
                    [(identity, canonical) for identity in identities],
                )
                db.executemany(
                    "UPDATE jobs SET status='cancelled' WHERE id=?",
                    [(value,) for value in identities],
                )
            for identity in identities:
                process = self.processes.get(identity)
                if process is not None:
                    self.stop(process)
                    self.processes.pop(identity, None)
                self.remove_folder(identity)
            with self.connect() as db:
                db.executemany("DELETE FROM jobs WHERE id=?", [(value,) for value in identities])
                db.commit()
                busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise OSError("Erasure waits for active readers")
