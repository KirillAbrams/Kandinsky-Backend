# Готовит соединения с базой и выдаёт отдельный сеанс каждой операции. Для местной базы включает
# ограничения, журнал и функцию сравнения текста, нужную запросам.

"""Соединение с базой и фабрика сеансов SQLAlchemy."""

from sqlite3 import Connection

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry


def database(url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Создаёт подключение и фабрику отдельных сеансов для запросов."""
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 20}
        if url.startswith("sqlite")
        else {},
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):
        # Для каждого нового местного соединения включает внешние ключи и журнал записи. Добавляет
        # сравнение текста без учёта регистра, иначе проверочная база не смогла бы выполнить тот
        # же поиск по чатам.
        @event.listens_for(engine, "connect")
        def sqlite_setup(connection: Connection, record: ConnectionPoolEntry) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.create_function("casefold", 1, str.casefold, deterministic=True)

    return engine, sessionmaker(engine, expire_on_commit=False)
