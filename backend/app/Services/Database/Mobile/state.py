# Хранит местные номера объектов, историю службы связи и очередь изменений для телефонов. Отметки
# удаления и порядок событий защищают от возврата старого сообщения после обрыва или позднего
# ответа.

"""Номера чатов и событий, которые понимают нативные клиенты."""

import json
import sqlite3
from contextlib import contextmanager

from app.Enums.moderation import BLOCKED_GENERATION
from app.Exceptions.accounts import ErasedAccount
from app.Services.Database.Mobile import state_erasure

OWNED_TABLES = (
    "identity",
    "messages",
    "events",
    "pending",
    "previews",
    "studio_uploads",
    "removed_results",
    "content_removals",
    "deleted_messages",
    "removed_media",
    "erased_chats",
    "removed_uploads",
    "deleted_slots",
)


# Сохраняет номер сообщения и заменяет вложения текстом об отказе после проверки. Признак
# блокировки нужен, чтобы запоздавшее обновление не показало прежний файл.
def moderated_answer(message):
    return {
        **message,
        "kind": "text",
        "text": BLOCKED_GENERATION,
        "attachments": [],
        "metadata": {
            **message["metadata"],
            "kandinskyStatus": "failed",
            "kandinskyError": BLOCKED_GENERATION,
            "kandinskyModerated": True,
        },
    }


# Сравнивает старые и новые ячейки по номеру. Считает переход из ошибки или отмены в работу
# законным только при новом номере запуска, иначе старое обновление могло бы оживить завершённое
# задание.
def has_retried_slot(previous, current):
    previous_slots = {slot["id"]: slot for slot in previous.get("kandinskySlots", [])}
    for slot in current.get("kandinskySlots", []):
        old = previous_slots.get(slot["id"])
        run = slot.get("runId")
        if (
            old
            and old["status"] in {"failed", "cancelled"}
            and slot["status"] in {"queued", "running"}
            and run
            and run != old.get("runId")
        ):
            return True
    return False


class State:
    # Готовит таблицы, журнал записи и запреты для удалённых владельцев при открытии базы. Запрет
    # на уровне таблиц блокирует позднюю запись, даже если запрос успел начаться до очистки.
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS identity (
                    id INTEGER PRIMARY KEY, owner TEXT, kind TEXT, external TEXT,
                    UNIQUE(owner, kind, external)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    owner TEXT, id INTEGER, chat INTEGER, job TEXT, sequence INTEGER,
                    body TEXT, revision INTEGER, PRIMARY KEY(owner, id),
                    UNIQUE(owner, chat, sequence)
                );
                CREATE TABLE IF NOT EXISTS events (
                    owner TEXT, seq INTEGER, body TEXT, PRIMARY KEY(owner, seq)
                );
                CREATE TABLE IF NOT EXISTS pending (
                    owner TEXT, id TEXT, deadline REAL, PRIMARY KEY(owner, id)
                );
                CREATE TABLE IF NOT EXISTS previews (
                    owner TEXT, blob TEXT, asset TEXT, PRIMARY KEY(owner, blob)
                );
                CREATE TABLE IF NOT EXISTS studio_uploads (
                    owner TEXT NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    route TEXT, expires REAL NOT NULL, blob TEXT, byte_size INTEGER,
                    PRIMARY KEY(owner, operation)
                );
                CREATE TABLE IF NOT EXISTS erased_accounts (owner TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS removed_results (
                    owner TEXT NOT NULL, job TEXT NOT NULL, asset TEXT,
                    PRIMARY KEY(owner, job)
                );
                CREATE INDEX IF NOT EXISTS ix_removed_result_asset ON removed_results(owner, asset);
                CREATE TABLE IF NOT EXISTS content_removals (
                    owner TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(owner,id)
                );
                CREATE TABLE IF NOT EXISTS deleted_messages (
                    owner TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(owner,id)
                );
                CREATE TABLE IF NOT EXISTS removed_media (
                    owner TEXT NOT NULL, blob TEXT NOT NULL, PRIMARY KEY(owner,blob)
                );
                CREATE TABLE IF NOT EXISTS erased_chats (
                    owner TEXT NOT NULL, chat INTEGER NOT NULL, PRIMARY KEY(owner,chat)
                );
                CREATE TABLE IF NOT EXISTS removed_uploads (
                    owner TEXT NOT NULL, operation TEXT NOT NULL, PRIMARY KEY(owner,operation)
                );
                CREATE TABLE IF NOT EXISTS deleted_slots (
                    owner TEXT NOT NULL, job TEXT NOT NULL, PRIMARY KEY(owner,job)
                );
            """)
            for table in OWNED_TABLES:
                for action in ("INSERT", "UPDATE"):
                    db.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS guard_erased_{table}_{action}
                        BEFORE {action} ON {table}
                        WHEN EXISTS (SELECT 1 FROM erased_accounts WHERE owner=NEW.owner)
                        BEGIN SELECT RAISE(ABORT, 'account_deleted'); END
                    """)

    # Открывает соединение с очисткой удалённых значений и закрывает его после операции. Отдельно
    # переводит отказ из-за удалённого аккаунта в ошибку, которую обработчик понимает как конец
    # доступа.
    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        except sqlite3.IntegrityError as error:
            if str(error) == "account_deleted":
                raise ErasedAccount from error
            raise
        finally:
            db.close()

    # Передаёт очистку чата в общий обработчик базы. И ручное удаление, и чтение очереди сервера
    # используют один порядок стирания и записи события.
    def erase_chat(self, owner, chat):
        state_erasure.erase_chat(self, owner, chat)

    # Передаёт удаление отдельных файлов обработчику базы. Общий порядок убирает запись об обложке
    # и помечает старую попытку загрузки, чтобы файл не вернулся с ней.
    def erase_assets(self, owner, media):
        state_erasure.erase_assets(self, owner, media)

    # Проверяет запрет на повтор конкретной отправки владельца. Без этой отметки телефон мог бы
    # вернуть удалённый исходник, автоматически повторив старую попытку.
    def upload_erased(self, owner, operation):
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM removed_uploads WHERE owner=? AND operation=?",
                    (owner, operation),
                ).fetchone()
                is not None
            )

    # Помечает аккаунты удалёнными и очищает их строки во всех служебных таблицах. Сохраняет
    # верхнюю границу номеров, чтобы новый аккаунт не получил номер прежней папки на телефоне.
    def erase_owners(self, owners):
        owners = [(owner,) for owner in owners]
        with self.connection() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            high_water = db.execute("SELECT MAX(id) FROM identity").fetchone()[0]
            db.executemany("INSERT OR IGNORE INTO erased_accounts VALUES (?)", owners)
            for table in OWNED_TABLES:
                db.executemany(f"DELETE FROM {table} WHERE owner=?", owners)
            # Телефон выбирает папку данных по номеру владельца. Повторная выдача номера открыла бы папку
            # удалённого аккаунта.
            if (
                high_water is not None
                and not db.execute("SELECT 1 FROM identity WHERE id=?", (high_water,)).fetchone()
            ):
                db.execute("DELETE FROM identity WHERE owner IS NULL AND kind='erasure_watermark'")
                db.execute(
                    "INSERT INTO identity (id,kind) VALUES (?,'erasure_watermark')",
                    (high_water,),
                )
            db.commit()
            db.execute("PRAGMA busy_timeout=0")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Проверяет, осталась ли отметка удаления этого владельца. Это позволяет остановить уже
    # начатую работу до следующего чтения файла или выдачи события.
    def is_erased(self, owner):
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM erased_accounts WHERE owner=?", (owner,)).fetchone()
                is not None
            )

    # Записывает сопоставление внешнего номера с местным и читает его обратно для того же
    # владельца и вида записи. Два чтения одной истории получают одинаковые номера.
    def identity(self, owner, kind, external):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO identity(owner,kind,external) VALUES (?,?,?)",
                (owner, kind, external),
            )
            return db.execute(
                "SELECT id FROM identity WHERE owner=? AND kind=? AND external=?",
                (owner, kind, external),
            ).fetchone()[0]

    # Находит внешний номер по владельцу, виду записи и местному номеру. Если хотя бы одна часть
    # не совпала, возвращать чужую запись нельзя.
    def external(self, owner, kind, number):
        with self.connection() as db:
            result = db.execute(
                "SELECT external FROM identity WHERE owner=? AND kind=? AND id=?",
                (owner, kind, number),
            ).fetchone()
            return result[0] if result else None

    # Сохраняет строку обычного текста из исходной истории и при необходимости убирает добавленный
    # старой сборкой ответ об отмене. Телефон, который видел прежнюю строку, получает событие
    # удаления.
    def remember_sample_text(self, owner, job, prompt, answer_id, publish):
        # Ранние сборки добавляли к обычному тексту ответ «Генерация отменена».
        # Уже открытым телефонам отправляем удаление этой строки один раз.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM messages WHERE owner=? AND id=? AND body<>'{}'",
                (owner, answer_id),
            ).fetchone():
                self.retire_message(db, owner, prompt["chatId"], answer_id, [])
            saved = self.remember_row(db, owner, job, prompt, publish)
            return [saved] if saved else []

    # Записывает запрос и ответ под одной блокировкой записи. Раздельная запись могла бы показать
    # ответ без запроса или оставить только запрос при сбое.
    def remember_pair(self, owner, job, prompt, answer, publish):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return [
                saved
                for message in (prompt, answer)
                if (saved := self.remember_row(db, owner, job, message, publish)) is not None
            ]

    # Проверяет, какие результаты задания или группы были удалены. Убирает их из вложений,
    # оставляет состояние ещё работающих ячеек и не возвращает больше недоступные файлы.
    @staticmethod
    def without_removed_results(db, owner, job, message):
        metadata = message.get("metadata")
        if not metadata:
            return message
        members = metadata.get("kandinskyMemberIds", [job])
        marks = ",".join("?" for _ in members)
        removed = {
            result
            for (result,) in db.execute(
                f"SELECT job FROM removed_results WHERE owner=? AND job IN ({marks}) "
                f"UNION SELECT job FROM deleted_slots WHERE owner=? AND job IN ({marks})",
                (owner, *members, owner, *members),
            )
        }
        if not removed:
            return message
        attachments = [
            item for item in message["attachments"] if item.get("resultJobId", job) not in removed
        ]
        message = State.revise_attachments(message, attachments)
        if attachments:
            return message
        if metadata.get("kandinskyPendingCount", 0) > 0:
            return {**message, "attachments": [], "kind": "text", "text": "Генерация продолжается"}
        return moderated_answer(message)

    # Сверяет удаление, прежнюю версию и порядок сообщения, затем сохраняет только нужное
    # изменение. Старая выдача не должна вернуть удалённый файл, изменить место строки или
    # заменить готовое состояние ожиданием. При совпадении содержимого сохраняет прежнюю версию
    # без нового события.
    def remember_row(self, db, owner, job, message, publish):
        number = message["id"]
        chat = message["chatId"]
        if db.execute(
            "SELECT 1 FROM erased_chats WHERE owner=? AND chat=?", (owner, chat)
        ).fetchone():
            return None
        if db.execute(
            "SELECT 1 FROM deleted_messages WHERE owner=? AND id=?", (owner, number)
        ).fetchone():
            return None
        available = [
            item
            for item in message["attachments"]
            if not self.media_removed(owner, item["blobId"], db=db)
        ]
        if available != message["attachments"]:
            message = self.revise_attachments(message, available)
        old = db.execute(
            "SELECT sequence,body,revision FROM messages WHERE owner=? AND id=?",
            (owner, number),
        ).fetchone()
        message = self.without_removed_results(db, owner, job, message)
        if old:
            prior = json.loads(old[1])
            if prior.get("metadata", {}).get("kandinskyModerated"):
                return {**prior, "revision": old[2]}
            terminal = {"succeeded", "failed", "cancelled"}
            previous_generation = prior.get("metadata", {})
            generation = message.get("metadata", {})
            if (
                previous_generation.get("kandinskyStatus") in terminal
                and generation.get("kandinskyStatus") not in terminal
                and not has_retried_slot(previous_generation, generation)
            ):
                return {**prior, "revision": old[2]}
            message["seq"] = old[0]
        else:
            while db.execute(
                "SELECT 1 FROM messages WHERE owner=? AND chat=? AND sequence=?",
                (owner, chat, message["seq"]),
            ).fetchone():
                message["seq"] += 2
        body = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if old and old[1] == body:
            return {**message, "revision": old[2]}
        revision = old[2] + 1 if old else 1
        db.execute(
            "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)",
            (owner, number, chat, job, message["seq"], body, revision),
        )
        result = {**message, "revision": revision}
        if result.get("metadata", {}).get("kandinskyModerated"):
            db.execute(
                "UPDATE events SET body=json_set(body,'$.message',json(?)) "
                "WHERE owner=? AND json_extract(body,'$.message.id')=?",
                (json.dumps(result, ensure_ascii=False), owner, number),
            )
        if publish or old:
            self.event(db, owner, {"chatId": chat, "kind": "message", "message": result})
        return result

    # Согласует список вложений с ячейками генерации. Если файлов больше нет, возвращает текстовое
    # состояние, чтобы лента не оставила пустой блок или ссылку на удалённое.
    @staticmethod
    def revise_attachments(message, available):
        metadata = message.get("metadata", {})
        slots = metadata.get("kandinskySlots")
        if slots is not None:
            blobs = {item["blobId"] for item in available}
            slots = [
                slot for slot in slots if slot.get("blobId") in blobs or not slot.get("blobId")
            ]
            message = {**message, "metadata": {**metadata, "kandinskySlots": slots}}
        if available:
            return {**message, "attachments": available}
        text = (
            "Генерация продолжается"
            if metadata.get("kandinskyPendingCount", 0)
            else "Результат удалён"
        )
        return {**message, "attachments": [], "kind": "text", "text": text}

    # Проверяет местную отметку удаления файла для выбранного владельца. При уже открытом
    # соединении читает отметку в той же операции, в которой обновляется сообщение.
    def media_removed(self, owner, blob, *, db=None):
        if db is None:
            with self.connection() as connection:
                return self.media_removed(owner, blob, db=connection)
        return (
            db.execute(
                "SELECT 1 FROM removed_media WHERE owner=? AND blob=?", (owner, blob)
            ).fetchone()
            is not None
        )

    # Собирает номера ранее удалённых вложений из событий выбранного сообщения. Повторное
    # уведомление тогда по-прежнему сообщает телефону, какие старые копии удалить.
    @staticmethod
    def prior_removed_media(db, owner, number):
        return {
            media["blobId"]: media
            for (value,) in db.execute(
                "SELECT value FROM events, json_each(body,'$.removedMedia') WHERE owner=? "
                "AND (json_extract(body,'$.message.id')=? OR json_extract(body,'$.messageId')=?)",
                (owner, number, number),
            )
            if (media := json.loads(value))
        }

    # Помечает сообщение удалённым, стирает его прежний текст и заменяет старые события отметкой
    # удаления. Телефон с отставшей историей получит очистку вместо старого содержимого.
    def retire_message(self, db, owner, chat, number, media):
        inserted = db.execute(
            "INSERT OR IGNORE INTO deleted_messages VALUES (?,?)", (owner, number)
        ).rowcount
        event = {"chatId": chat, "kind": "deleted", "messageId": number}
        complete = self.prior_removed_media(db, owner, number)
        complete.update((item["blobId"], item) for item in media)
        if complete:
            event["removedMedia"] = list(complete.values())
        db.execute("UPDATE messages SET body='{}' WHERE owner=? AND id=?", (owner, number))
        db.execute(
            "UPDATE events SET body=? WHERE owner=? AND ("
            "json_extract(body,'$.message.id')=? OR json_extract(body,'$.messageId')=?)",
            (json.dumps(event, ensure_ascii=False), owner, number, number),
        )
        if inserted or media:
            self.event(db, owner, event)

    # Применяет только ещё не отмеченные удаления, затем стирает файлы и корректирует сообщение и
    # старые события. При повторной доставке не добавляет ту же команду второй раз.
    def remove_content(self, owner, chat, root, prompt_id, answer_id, items, retire_answer):
        with self.connection() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            applied = [
                item
                for item in items
                if db.execute(
                    "INSERT OR IGNORE INTO content_removals VALUES (?,?)", (owner, item["id"])
                ).rowcount
            ]
            if not applied:
                state_erasure.checkpoint(db)
                return
            media = [
                {"blobId": item["blob"], "previewBlobId": item["preview"]}
                for item in applied
                if item["blob"]
            ]
            revoked = [
                {"blobId": item["blob"], "previewBlobId": item["preview"]}
                for item in applied
                if item["blob"] and item.get("revoke_asset", True)
            ]
            state_erasure.erase_asset_rows(db, owner, revoked)
            for item in applied:
                if not item["prompt"]:
                    db.execute(
                        "INSERT OR IGNORE INTO deleted_slots VALUES (?,?)",
                        (owner, item["member_id"]),
                    )
            if any(item["prompt"] for item in applied):
                self.retire_message(db, owner, chat, prompt_id, [])
                self.retire_message(db, owner, chat, answer_id, revoked)
                db.execute("DELETE FROM pending WHERE owner=? AND id=?", (owner, root))
            elif retire_answer:
                self.retire_message(db, owner, chat, answer_id, revoked)
                db.execute("DELETE FROM pending WHERE owner=? AND id=?", (owner, root))
            elif media:
                current = db.execute(
                    "SELECT body FROM messages WHERE owner=? AND id=?", (owner, answer_id)
                ).fetchone()
                if current and (message := json.loads(current[0])):
                    removed = {item["blobId"] for item in media}
                    keep = [
                        item for item in message["attachments"] if item["blobId"] not in removed
                    ]
                    saved = self.remember_row(
                        db, owner, root, self.revise_attachments(message, keep), False
                    )
                    if saved:
                        complete = self.prior_removed_media(db, owner, answer_id)
                        complete.update((item["blobId"], item) for item in revoked)
                        event = {
                            "chatId": chat,
                            "kind": "message",
                            "message": saved,
                            "removedMedia": list(complete.values()),
                        }
                        for blob in removed:
                            db.execute(
                                "UPDATE events SET body=? WHERE owner=? AND json_extract(body,'$.message.id')=? "
                                "AND EXISTS (SELECT 1 FROM json_each(body,'$.message.attachments') "
                                "WHERE json_extract(value,'$.blobId')=?)",
                                (json.dumps(event, ensure_ascii=False), owner, answer_id, blob),
                            )
                        self.event(db, owner, event)
            state_erasure.checkpoint(db)

    # Убирает скрытые основным сервером результаты и переоформляет ответы, к которым они
    # относились. Исправляет и прежние события, чтобы телефон не увидел файл при догрузке старой
    # истории.
    def remove_results(self, items):
        with self.connection() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            for owner, job, asset in items:
                if db.execute("SELECT 1 FROM erased_accounts WHERE owner=?", (owner,)).fetchone():
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO removed_results VALUES (?,?,?)",
                    (owner, job, asset),
                )
                db.execute("DELETE FROM pending WHERE owner=? AND id=?", (owner, job))
                if asset:
                    db.execute("DELETE FROM previews WHERE owner=? AND asset=?", (owner, asset))
                rows = db.execute(
                    "SELECT m.job,m.body FROM messages m JOIN identity i ON i.id=m.id "
                    "WHERE m.owner=? AND i.kind='answer' AND (m.job=? OR EXISTS ("
                    "SELECT 1 FROM json_each(m.body,'$.metadata.kandinskyMemberIds') "
                    "WHERE value=?))",
                    (owner, job, job),
                )
                for root, body in rows.fetchall():
                    if not (message := json.loads(body)):
                        continue
                    current = self.remember_row(db, owner, root, message, True)
                    if asset and current:
                        db.execute(
                            "UPDATE events SET body=json_set(body,'$.message',json(?)) "
                            "WHERE owner=? AND json_extract(body,'$.message.id')=? AND EXISTS ("
                            "SELECT 1 FROM json_each(body,'$.message.attachments') "
                            "WHERE json_extract(value,'$.blobId')=?)",
                            (
                                json.dumps(current, ensure_ascii=False),
                                owner,
                                current["id"],
                                asset.replace("-", ""),
                            ),
                        )
            db.commit()
            db.execute("PRAGMA busy_timeout=0")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Записывает следующее изменение под новым номером владельца в том же соединении. Для уже
    # удалённого чата оставляет только отметку об исчезновении, чтобы его содержимое не вышло
    # повторно.
    @staticmethod
    def event(db, owner, event):
        chat = event.get("chatId")
        if (
            chat is not None
            and db.execute(
                "SELECT 1 FROM erased_chats WHERE owner=? AND chat=?", (owner, chat)
            ).fetchone()
        ):
            event = {"chatId": chat, "kind": "kandinsky.retired"}
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE owner=?", (owner,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (owner, seq, json.dumps(event, ensure_ascii=False)),
        )

    # Читает наибольший номер события владельца или ноль при пустой истории. По этому номеру
    # телефон понимает, с какого места ждать следующие изменения.
    def cursor(self, owner):
        with self.connection() as db:
            return db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE owner=?", (owner,)
            ).fetchone()[0]

    # Возвращает до четырёх событий владельца строго после его отметки и по порядку. Ограничение
    # не даёт длинной истории отправиться через соединение одной большой порцией.
    def events_after(self, owner, cursor):
        with self.connection() as db:
            rows = db.execute(
                "SELECT seq,body FROM events WHERE owner=? AND seq>? ORDER BY seq LIMIT 4",
                (owner, cursor),
            ).fetchall()
            return [{"seq": seq, **json.loads(body)} for seq, body in rows]

    # Находит задание по сообщению или отметке порядка внутри выбранного чата. Проверка владельца
    # и чата защищает переход к чужой строке или чужому заданию.
    def boundary(self, owner, chat, *, sequence=None, message=None):
        with self.connection() as db:
            column, value = ("sequence", sequence) if sequence is not None else ("id", message)
            result = db.execute(
                f"SELECT job FROM messages WHERE owner=? AND chat=? AND {column}=?",
                (owner, chat, value),
            ).fetchone()
            return result[0] if result else None

    # Заносит работающие задания в очередь проверки и убирает завершённые. Уже удалённые
    # результаты туда не попадают, иначе служба продолжала бы их перечитывать.
    def update_pending(self, owner, job):
        with self.connection() as db:
            if job["status"] in {"queued", "running"}:
                db.execute(
                    "INSERT OR IGNORE INTO pending SELECT ?,?,? "
                    "WHERE NOT EXISTS(SELECT 1 FROM removed_results WHERE owner=? AND job=?)",
                    (owner, job["id"], job["created_at"] + 300, owner, job["id"]),
                )
            else:
                db.execute("DELETE FROM pending WHERE owner=? AND id=?", (owner, job["id"]))

    # Выбирает до десяти ожидающих заданий по близости записанного срока. Просроченное задание
    # тоже читается, чтобы поздно готовый результат всё равно появился.
    def pending(self, owner):
        with self.connection() as db:
            return [
                job
                for (job,) in db.execute(
                    "SELECT id FROM pending WHERE owner=? ORDER BY deadline LIMIT 10",
                    (owner,),
                )
            ]

    # Сохраняет или находит связь между обложкой и оригиналом файла владельца. Перед записью
    # сверяет отметки удаления, иначе позднее чтение истории могло бы восстановить обложку.
    def preview(self, owner, blob, asset=None):
        with self.connection() as db:
            if asset is not None:
                db.execute(
                    "INSERT OR IGNORE INTO previews SELECT ?,?,? "
                    "WHERE NOT EXISTS(SELECT 1 FROM removed_results WHERE owner=? AND asset=?) "
                    "AND NOT EXISTS(SELECT 1 FROM removed_media WHERE owner=? AND blob=?)",
                    (owner, blob, asset, owner, asset, owner, blob),
                )
                return asset
            row = db.execute(
                "SELECT asset FROM previews WHERE owner=? AND blob=?", (owner, blob)
            ).fetchone()
            return row[0] if row else None

    # Читает сохранённую попытку отправки с отпечатком файла и её результатом или разрешением. При
    # потере ответа можно продолжить прежнюю попытку с тем же содержимым.
    def studio_upload(self, owner, operation):
        with self.connection() as db:
            return db.execute(
                "SELECT fingerprint, route, expires, blob, byte_size "
                "FROM studio_uploads WHERE owner=? AND operation=?",
                (owner, operation),
            ).fetchone()

    # Запоминает отпечаток и временное разрешение до передачи файла. При продлении меняет только
    # разрешение и срок, иначе один номер можно было бы привязать к другому файлу.
    def begin_studio_upload(self, owner, operation, fingerprint, route, expires):
        with self.connection() as db:
            db.execute(
                "INSERT INTO studio_uploads (owner,operation,fingerprint,route,expires) "
                "VALUES (?,?,?,?,?) ON CONFLICT(owner,operation) DO UPDATE SET "
                "route=excluded.route,expires=excluded.expires",
                (owner, operation, fingerprint, route, expires),
            )

    # Записывает номер и размер уже принятого файла и убирает использованное разрешение. Следующий
    # повтор получает готовый результат без новой отправки.
    def complete_studio_upload(self, owner, operation, blob, size):
        with self.connection() as db:
            db.execute(
                "UPDATE studio_uploads SET route=NULL, blob=?, byte_size=? "
                "WHERE owner=? AND operation=?",
                (blob, size, owner, operation),
            )
