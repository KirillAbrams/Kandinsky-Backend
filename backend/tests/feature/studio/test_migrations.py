# Проверяет переходы между версиями схемы на временных базах. Кроме появления таблиц сверяет
# сохранение сеансов, порядка чатов и связанных файлов.

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.support.fixtures import migration_config


# Применяет прежнюю цепочку к пустой базе и откатывает её до начала. После отката остаётся только
# служебная таблица версий, иначе часть обратных шагов потеряна.
def test_legacy_schema_can_upgrade_and_return_to_base(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/migrations.sqlite"
    monkeypatch.setenv("STUDIO_DATABASE_URL", database_url)
    config = migration_config()

    command.upgrade(config, "1fa4ab821ef9")
    engine = create_engine(database_url)
    try:
        assert "jobs" in inspect(engine).get_table_names()
        command.downgrade(config, "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


# Создаёт владельца и сеанс на старой схеме и сверяет их после перехода вперёд, обратно и до
# последней версии. Обновление не должно сбрасывать существующий вход.
def test_registration_migration_preserves_existing_login_and_sessions(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/previous.sqlite"
    monkeypatch.setenv("STUDIO_DATABASE_URL", database_url)
    config = migration_config()
    command.upgrade(config, "63e7817bd409")
    engine = create_engine(database_url)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO users (id,email,password_hash,generation_suspended_at) VALUES ('prior','old@example.com','unchanged',NULL)"
            )
        )
        db.execute(
            text(
                "INSERT INTO sessions (digest,user_id,expires_at,last_activity_at) VALUES ('existing','prior',999,123)"
            )
        )
    for target in ("1fa4ab821ef9", "63e7817bd409", "head"):
        if target != "63e7817bd409":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
        with engine.connect() as db:
            assert db.execute(
                text("SELECT id,email,password_hash,generation_suspended_at FROM users")
            ).one() == ("prior", "old@example.com", "unchanged", None)
            assert db.execute(
                text("SELECT digest,user_id,expires_at,last_activity_at FROM sessions")
            ).one() == ("existing", "prior", 999, 123)
    engine.dispose()


# Создаёт отметку ожидающего удаления и пытается перейти к схеме до защиты повторов. Проверяет
# отказ, сохранённую отметку и неизменившуюся версию базы.
def test_erasure_schema_preserves_replay_markers_when_downgrade_is_attempted(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/erasure-migration.sqlite"
    monkeypatch.setenv("STUDIO_DATABASE_URL", database_url)
    config = migration_config()
    command.upgrade(config, "d13e9a7f02b4")
    engine = create_engine(database_url)
    try:
        assert any(
            fk["referred_table"] == "threads"
            for fk in inspect(engine).get_foreign_keys("upload_grants")
        )
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO pending_gpu_removals VALUES ('erased-operation', NULL, 123)")
            )
        with pytest.raises(RuntimeError, match="Стирание содержимого нельзя отменить"):
            command.downgrade(config, "1fa4ab821ef9")
        with engine.connect() as db:
            assert db.execute(text("SELECT * FROM pending_gpu_removals")).one() == (
                "erased-operation",
                None,
                123,
            )
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "d13e9a7f02b4"
            )
    finally:
        engine.dispose()


# Создаёт закреплённый и свежий чаты с общими сведениями для файла, затем добавляет и убирает
# указатели. Выборка и связи должны остаться прежними, а состав указателей измениться в обе
# стороны.
def test_lookup_indexes_preserve_rows_and_order_across_upgrade_and_rollback(tmp_path, monkeypatch):
    from app.Models import Asset, ChatSource, Thread, Transfer, UploadGrant, User

    database_url = f"sqlite:///{tmp_path}/lookup-indexes.sqlite"
    monkeypatch.setenv("STUDIO_DATABASE_URL", database_url)
    config = migration_config()
    previous_revision = "d13e9a7f02b4"
    command.upgrade(config, previous_revision)
    engine = create_engine(database_url)

    # Считывает список указателей и сверяет порядок двух чатов и связь разрешений с файлом.
    # Одинаковый запрос сравнивает данные на каждой версии схемы.
    def snapshot():
        with engine.connect() as db:
            indexes = set(
                db.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars()
            )
            recent = (
                db.execute(
                    text(
                        "SELECT id FROM threads WHERE user_id='owner' AND deleted_at IS NULL "
                        "AND archived_at IS NULL ORDER BY (pinned_at IS NOT NULL) DESC, activity_at DESC, id DESC"
                    )
                )
                .scalars()
                .all()
            )
            links = db.execute(
                text(
                    "SELECT s.thread_id, t.digest, g.digest FROM chat_sources s "
                    "JOIN transfers t ON t.asset_id=s.asset_id JOIN upload_grants g ON g.asset_id=s.asset_id"
                )
            ).all()
        assert recent == ["pinned", "newer"]
        assert links == [("pinned", "download", "upload")]
        return indexes

    try:
        with engine.begin() as db:
            db.execute(
                User.__table__.insert().values(
                    id="owner", email="old@example.com", password_hash="fixture"
                )
            )
            db.execute(
                Thread.__table__.insert(),
                [
                    {"id": "pinned", "user_id": "owner", "activity_at": 1, "pinned_at": 3},
                    {"id": "newer", "user_id": "owner", "activity_at": 2, "pinned_at": None},
                ],
            )
            db.execute(
                Asset.__table__.insert().values(
                    id="media", user_id="owner", mime="image/png", size=4, filename="fixture.png"
                )
            )
            db.execute(ChatSource.__table__.insert().values(thread_id="pinned", asset_id="media"))
            db.execute(
                Transfer.__table__.insert().values(
                    digest="download", asset_id="media", expires_at=10
                )
            )
            db.execute(
                UploadGrant.__table__.insert().values(
                    digest="upload", user_id="owner", asset_id="media", expires_at=10
                )
            )

        existing_indexes = snapshot()
        new_indexes = {
            "ix_chat_sources_asset_id",
            "ix_transfers_asset_id",
            "ix_transfers_expires_at",
            "ix_upload_grants_asset_id",
            "ix_threads_recent_order",
        }
        assert existing_indexes.isdisjoint(new_indexes)

        command.upgrade(config, "head")
        assert snapshot() == existing_indexes | new_indexes

        command.downgrade(config, previous_revision)
        assert snapshot() == existing_indexes
    finally:
        engine.dispose()
