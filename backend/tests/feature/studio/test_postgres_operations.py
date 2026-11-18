# Проверяет права ролей, ожидание заданий и одновременные записи на отдельной настоящей базе. Её
# запускают на время проверки без доступа по сети.

import asyncio
import os
import runpy
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.engine import URL

from app.Console.Commands import database_roles
from app.Jobs import worker
from app.Models import Asset, Job, Thread, User
from app.Services.Database.connection import database
from app.Services.Database.notifications import notify
from app.Services.Generation import retry_generation
from tests.support.fixtures import account, create_studio, migration_config


# Создаёт короткий временный путь для локального соединения, запускает отдельную базу и отдаёт
# путь проверке. При любом исходе останавливает свой процесс, иначе он мог бы остаться после
# удаления папки.
@pytest.fixture
def postgres_socket():
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl or os.geteuid() == 0:
        pytest.skip("Нужны initdb и pg_ctl от обычного пользователя")
    # У сокета местной базы есть ограничение длины пути.
    with TemporaryDirectory(prefix="kdy-pg-", dir="/tmp") as socket:
        data = Path(socket) / "data"
        logfile = Path(socket) / "postgres.log"
        subprocess.run(
            [initdb, "-D", data, "-U", "layout_admin", "--auth-local=trust", "--auth-host=reject"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            subprocess.run(
                [
                    pg_ctl,
                    "-D",
                    data,
                    "-l",
                    logfile,
                    "-o",
                    f"-k {socket} -c listen_addresses=''",
                    "-w",
                    "start",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            yield socket
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run(
                    [pg_ctl, "-D", data, "-m", "fast", "-w", "stop"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )


# Создаёт прежнюю таблицу и счётчик, применяет роли и обновляет схему. Вход и запись данных
# работают с обычными правами, менять таблицы может только выделенная роль. Лишнее членство
# служебной роли вызывает отказ.
def test_role_commands_keep_schema_changes_away_from_the_server_and_worker(
    postgres_socket, monkeypatch
):
    # Собирает адрес пробной базы для указанной роли через локальное соединение. При смене роли
    # обращения остаются в той же изолированной базе.
    def url(user):
        return URL.create(
            "postgresql+psycopg",
            username=user,
            database="postgres",
            query={"host": postgres_socket},
        ).render_as_string(hide_password=False)

    # Открывает прямое соединение от указанной роли. Так можно проверить права самой базы
    # независимо от проверок сервера.
    def connection(user):
        return psycopg.connect(dbname="postgres", user=user, host=postgres_socket)

    for env in database_roles.ROLES.values():
        monkeypatch.setenv(env, "disposable-check-password")
    monkeypatch.setenv("STUDIO_DATABASE_URL", url("layout_admin"))
    with connection("layout_admin") as db:
        db.execute("CREATE TABLE existing_rows (id integer)")
        db.execute("CREATE SEQUENCE existing_numbers")

    monkeypatch.setattr(sys, "argv", ["database_roles", "provision"])
    runpy.run_path(database_roles.__file__, run_name="__main__")
    database_roles.main("provision")
    with pytest.raises(ValueError, match="Неизвестная операция"):
        database_roles.main("unknown")

    monkeypatch.setenv("STUDIO_DATABASE_URL", url("kandinsky_migrate"))
    command.upgrade(migration_config(), "head")
    database_roles.main("grant")
    api = create_studio(
        database_url=url("kandinsky_api"),
        media_dir=Path(postgres_socket) / "media",
        provider="demo",
    )
    with TestClient(api) as client:
        assert client.get("/health").json() == {"ok": True, "provider": "demo"}
        headers = account(client)
        login = client.post(
            "/v1/auth/login", json={"email": "one@example.com", "password": "long-password-123"}
        )
        assert login.status_code == 200
        assert (
            client.get("/v1/auth/me", headers=headers).json()["user_id"] == login.json()["user_id"]
        )

    with connection("kandinsky_migrate") as db:
        owner = db.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'existing_rows' AND schemaname = 'public'"
        ).fetchone()
        assert owner == ("kandinsky_migrate",)
        assert db.execute("SELECT nextval('existing_numbers')").fetchone() == (1,)
        db.execute("ALTER TABLE existing_rows ADD COLUMN detail text")

    with connection("kandinsky_api") as db:
        assert db.execute("INSERT INTO existing_rows (id) VALUES (1)").rowcount == 1
        assert db.execute("SELECT id FROM existing_rows").fetchone() == (1,)

    for role in ("kandinsky_api", "kandinsky_worker"):
        with connection(role) as db:
            privileges = db.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            assert privileges == (False,) * 5
            assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("CREATE TABLE unsafe_probe (id integer)")

    with connection("kandinsky_worker") as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM users")

    monkeypatch.setenv("STUDIO_DATABASE_URL", url("layout_admin"))
    with connection("layout_admin") as db:
        db.execute("CREATE ROLE unexpected_membership")
        db.execute("GRANT unexpected_membership TO kandinsky_api")
    with pytest.raises(RuntimeError, match="членство служебной роли kandinsky_api"):
        database_roles.main("provision")


# Создаёт задание со сроком незавершённой работы и отправляет уведомление о новой очереди.
# Проверяет, что после истечения срока другой обработчик заканчивает работу и сохраняет файл.
def test_postgres_signal_and_lease_timeout_resume_an_interrupted_job(
    postgres_socket, tmp_path, monkeypatch
):
    url = URL.create(
        "postgresql+psycopg",
        username="layout_admin",
        database="postgres",
        query={"host": postgres_socket},
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("STUDIO_DATABASE_URL", url)
    monkeypatch.setenv("STUDIO_MEDIA_DIR", str(tmp_path / "worker-media"))
    monkeypatch.setenv("STUDIO_PROVIDER", "demo")
    monkeypatch.setenv("STUDIO_MODERATION_MODE", "off")
    monkeypatch.setenv("STUDIO_GPU_TOKEN", "")
    command.upgrade(migration_config(), "head")

    # Ждёт, пока обработчик подпишется и увидит пустую очередь, затем добавляет прерванное
    # задание. Сверяет разные уведомления, ожидание срока и вторую успешную попытку. В конце
    # останавливает обработчика и закрывает соединение.
    async def exercise():
        listening = asyncio.Event()
        idle = asyncio.Event()
        notified = asyncio.Event()
        finished = asyncio.Event()
        waiting_lease = asyncio.Event()
        unrelated_notice = asyncio.Event()
        listen = worker.listen
        next_claim_time = worker.next_claim_time
        process_one = worker.process_one

        # Подключает обычное ожидание уведомлений и оборачивает обработчик. Проверка видит, когда
        # подписка заработала и какие сообщения дошли.
        async def observe_notifications(database_url, wake):
            # Передаёт уведомление обычному коду и отмечает его вид. Так сигналы о начале чтения,
            # очереди и результатах проверяются отдельно.
            def receive(change):
                wake(change)
                if change == "catch_up":
                    listening.set()
                if change == "queue":
                    notified.set()
                if change == "results":
                    unrelated_notice.set()

            await listen(database_url, receive)

        # Выполняет обычный проход обработчика и отмечает пустую очередь или выполненное задание.
        # Без отметок проверка не знала бы, когда добавлять работу и читать результат.
        async def observe_work(*args):
            did_work = await process_one(*args)
            if did_work:
                finished.set()
            else:
                idle.set()
            return did_work

        # Передаёт обычный срок ожидания и отмечает, что обработчик его получил. Так видно, что
        # занятую работу не попытались забрать сразу.
        def observe_lease_timer(factory):
            deadline = next_claim_time(factory)
            if deadline is not None:
                waiting_lease.set()
            return deadline

        monkeypatch.setattr(worker, "next_claim_time", observe_lease_timer)
        monkeypatch.setattr(worker, "listen", observe_notifications)
        monkeypatch.setattr(worker, "process_one", observe_work)
        engine, factory = database(url)
        background = asyncio.create_task(worker.main())
        try:
            await asyncio.wait_for(listening.wait(), timeout=5)
            await asyncio.wait_for(idle.wait(), timeout=5)
            with factory() as db:
                owner = User(email="worker@example.com", password_hash="isolated-worker-test")
                db.add(owner)
                db.flush()
                chat = Thread(user_id=owner.id)
                db.add(chat)
                db.flush()
                request = Job(
                    user_id=owner.id,
                    thread_id=chat.id,
                    operation_key="notify-worker",
                    fingerprint="a" * 64,
                    prompt="Дерево",
                    model="image",
                    aspect="1:1",
                    seed=15,
                    provider="demo",
                    status="running",
                    lease_owner="retired-worker",
                    lease_until=time.time() + 2,
                    attempts=1,
                )
                db.add(request)
                db.flush()
                job_id = request.id
                notify(db, "results")
                notify(db, "queue")
                db.commit()
            await asyncio.wait_for(unrelated_notice.wait(), timeout=5)
            await asyncio.wait_for(notified.wait(), timeout=5)
            await asyncio.wait_for(waiting_lease.wait(), timeout=5)
            await asyncio.wait_for(finished.wait(), timeout=5)
            with factory() as db:
                saved = db.get(Job, job_id)
                assert saved.status == "succeeded"
                assert saved.progress == 100
                assert saved.attempts == 2
                result = db.get(Asset, saved.output_id)
                assert (tmp_path / "worker-media" / result.filename).is_file()
        finally:
            background.cancel()
            await background
            engine.dispose()

    asyncio.run(exercise())


# Два владельца одновременно создают чат с одним номером, затем в чате победителя проверяется
# поздний повтор задания. В базе остаётся один владелец, а устаревший запрос не перезаписывает
# новый запуск.
def test_postgres_resolves_competing_chat_insert_and_rejects_retry_of_changed_job(
    postgres_socket, tmp_path, monkeypatch
):
    url = URL.create(
        "postgresql+psycopg",
        username="layout_admin",
        database="postgres",
        query={"host": postgres_socket},
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("STUDIO_DATABASE_URL", url)
    command.upgrade(migration_config(), "head")
    app = create_studio(database_url=url, media_dir=tmp_path / "media", provider="demo")
    factory = app.state.factory

    with TestClient(app) as client:
        first = account(client)
        second = account(client, "second@example.com")
        chat = str(uuid4())
        inserts = Barrier(2, timeout=5)

        # Удерживает оба запроса перед вставкой одного и того же чата. Они доходят до спорного
        # места вместе, вместо случайного порядка.
        def overlap_chat_inserts(db, context, instances):
            if any(isinstance(row, Thread) and row.id == chat for row in db.new):
                inserts.wait()

        event.listen(factory, "before_flush", overlap_chat_inserts)
        try:
            with ThreadPoolExecutor(max_workers=2) as threads:
                # Отправляет создание чата под указанным владельцем. Номер чата у двух вызовов
                # один, чтобы проверить конфликт владения.
                def create(headers):
                    return client.post("/v1/threads", json={"id": chat}, headers=headers)

                replies = list(threads.map(create, (first, second)))
        finally:
            event.remove(factory, "before_flush", overlap_chat_inserts)

        assert sorted(response.status_code for response in replies) == [201, 404]
        owner = first if replies[0].status_code == 201 else second
        assert client.get("/v1/threads", headers=owner).json()[0]["id"] == chat
        recorded = client.post(
            "/v1/jobs",
            json={"thread_id": chat, "prompt": "Магнолия"},
            headers={**owner, "Idempotency-Key": str(uuid4())},
        )
        assert recorded.status_code == 201
        job_id, replacement_run = recorded.json()["id"], str(uuid4())
        with factory.begin() as db:
            db.get(Job, job_id).status = "failed"
        read_job = retry_generation.visible_job

        # Читает старое состояние, обновляет запуск через другое соединение и возвращает прежнюю
        # запись. Текущая операция должна увидеть конфликт при записи, хотя чтение ещё показывало
        # старую ошибку.
        def retry_after_replacement(db, key, user):
            stale = read_job(db, key, user)
            with factory.begin() as peer:
                peer.execute(
                    update(Job)
                    .where(Job.id == key)
                    .values(status="running", run_id=replacement_run)
                )
            return stale

        with monkeypatch.context() as patch:
            patch.setattr(retry_generation, "visible_job", retry_after_replacement)
            retry = client.post(
                f"/v1/jobs/{job_id}/retry", headers=owner, params={"operation": str(uuid4())}
            )
        assert retry.status_code == 409
        current = client.get(f"/v1/jobs/{job_id}", headers=owner).json()
        assert (current["status"], current["run_id"]) == ("running", replacement_run)
