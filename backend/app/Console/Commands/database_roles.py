# Готовит отдельные роли базы для обновления схемы, запросов и обработчика. Разделение даёт
# рабочим процессам доступ к данным без права менять всю схему.

"""Выделяет владельца схемы и выдаёт службам права на данные проекта."""

import os
import sys

import psycopg
from psycopg import sql

ROLES = {
    "kandinsky_migrate": "STUDIO_MIGRATION_PASSWORD",
    "kandinsky_api": "STUDIO_API_PASSWORD",
    "kandinsky_worker": "STUDIO_WORKER_PASSWORD",
}


# Под общей блокировкой создаёт или приводит роли к заданным ограничениям, выдаёт права в схеме и
# передаёт таблицы прежнего владельца роли обновления. Проверяет чужое членство, чтобы лишние
# права не сохранились через другую роль.
def provision(db):
    db.execute("SELECT pg_advisory_xact_lock(61748321)")
    admin, database = db.execute("SELECT current_user, current_database()").fetchone()
    for role, setting in ROLES.items():
        identifier = sql.Identifier(role)
        exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if not exists:
            db.execute(sql.SQL("CREATE ROLE {}").format(identifier))
        verifier = db.pgconn.encrypt_password(
            os.environ[setting].encode(), role.encode(), b"scram-sha-256"
        )
        db.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(identifier, sql.Literal(verifier.decode()))
        )
        if db.execute(
            "SELECT 1 FROM pg_auth_members WHERE member = %s::regrole", (role,)
        ).fetchone():
            raise RuntimeError(f"Проверьте членство служебной роли {role} в других ролях")

    db.execute(
        sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(database)
        )
    )
    db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    db.execute("GRANT USAGE, CREATE ON SCHEMA public TO kandinsky_migrate")
    db.execute("GRANT USAGE ON SCHEMA public TO kandinsky_api, kandinsky_worker")
    # В прежней установке таблицы создавал администратор. Меняем владельца только внутри схемы приложения.
    for name, kind in db.execute(
        "SELECT relname, relkind FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relowner = %s::regrole AND relkind IN ('r', 'p', 'S') ORDER BY relkind = 'S'",
        (admin,),
    ).fetchall():
        command = "ALTER SEQUENCE" if kind == "S" else "ALTER TABLE"
        db.execute(
            sql.SQL(command + " {} OWNER TO kandinsky_migrate").format(
                sql.Identifier("public", name)
            )
        )


# После изменения схемы даёт серверу права на рабочие таблицы и выдаёт обработчику только нужные
# чтение и запись. Таблица версий остаётся у роли обновления, иначе рабочая служба могла бы менять
# состояние схемы.
def grant_data_access(db):
    # Служебная таблица обновлений остаётся доступной только владельцу схемы.
    for (name,) in db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tableowner = current_user AND tablename <> 'alembic_version'"
    ).fetchall():
        db.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO kandinsky_api").format(
                sql.Identifier("public", name)
            )
        )
    db.execute("GRANT SELECT, UPDATE ON public.jobs TO kandinsky_worker")
    db.execute("GRANT SELECT, INSERT ON public.assets TO kandinsky_worker")
    db.execute(
        "GRANT SELECT, DELETE ON public.erased_sessions, public.pending_media_removals TO kandinsky_worker"
    )
    db.execute("GRANT SELECT, DELETE ON public.pending_gpu_removals TO kandinsky_worker")
    db.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kandinsky_api")


# Читает адрес базы из окружения, открывает соединение и выполняет указанное действие. Неизвестную
# команду отклоняет, чтобы ошибка ввода не запустила другой вид выдачи прав.
def main(action):
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["STUDIO_DATABASE_URL"]).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False)) as db:
        if action == "provision":
            provision(db)
        elif action == "grant":
            grant_data_access(db)
        else:
            raise ValueError("Неизвестная операция с ролями")


if __name__ == "__main__":
    main(sys.argv[1])
