# Проверяет очистку очередей, когда записей больше одной выдаваемой порции. После чтения сверяет и
# пустые серверные очереди, и полученные службой связи отметки.

import asyncio
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.Models import AccountErasure, JobContentRemoval, ResultRemoval, Thread, User
from app.Services.Database.Mobile.state import State
from app.Services.Mobile import erasure_sync
from app.Services.Mobile.adapter import Adapter
from tests.support.fixtures import create_studio, register_account

ERASURE_KEY = "local-erasure-pages-test-secret" * 2


# Создаёт последовательные номера из заданного диапазона. Разные диапазоны позволяют заполнить
# несколько очередей без совпадений и точно сверить итог.
def ids(offset, count):
    return [str(UUID(int=offset + index)) for index in range(count)]


# Заполняет очереди удалённых владельцев, чатов, запросов и результатов сверх границы порции.
# Проверяет отказ пустому подтверждению и полную доставку всех отметок, включая последнюю короткую
# порцию.
def test_bridge_drains_each_removal_feed_past_one_page(tmp_path):
    studio = create_studio(
        database_url=f"sqlite:///{tmp_path}/studio.sqlite",
        media_dir=tmp_path / "media",
        bridge_erasure_key=ERASURE_KEY,
        provider="demo",
    )
    state = State(tmp_path / "bridge.sqlite")
    erased_owners, results = ids(100, 101), ids(1000, 101)
    deleted_threads, contents = ids(2000, 129), ids(3000, 129)
    native_contents = ids(4000, 33)
    key = {"X-Kandinsky-Erasure-Key": ERASURE_KEY}

    # Вызывает поочерёдно все виды очистки через местную передачу запросов. Итоговые числа
    # показывают результат каждого маршрута.
    async def drain(owner, token):
        async with httpx.AsyncClient(
            base_url="http://studio.test", transport=httpx.ASGITransport(app=studio)
        ) as client:
            adapter = Adapter(client, state)
            await adapter.sync_account_erasures(ERASURE_KEY)
            await adapter.sync_result_removals(ERASURE_KEY)
            await erasure_sync.sync_thread_erasures(adapter, ERASURE_KEY)
            await adapter.sync_content_removals(owner, token)
            await erasure_sync.sync_content_erasures(adapter, ERASURE_KEY)

    with TestClient(studio) as client:
        user = register_account(client, "pages@example.com", "local-test-password-42").json()
        owner, token = user["user_id"], user["token"]
        other = ids(6000, 1)[0]
        with studio.state.factory() as db:
            db.add(User(id=other, email="other@example.com", password_hash="not-used-for-login"))
            db.flush()
            db.add_all(AccountErasure(owner_id=item) for item in erased_owners)
            db.add_all(ResultRemoval(job_id=item, owner_id=owner) for item in results)
            db.add_all(
                Thread(id=item, user_id=owner, deleted_at=1.0, erasure_pending=True)
                for item in deleted_threads
            )
            db.add_all(
                JobContentRemoval(
                    id=item,
                    owner_id=account,
                    thread_id=str(UUID(int=5000)),
                    root_id=item,
                    member_id=item,
                    prompt=True,
                )
                for account, batch in ((owner, native_contents), (other, contents))
                for item in batch
            )
            db.commit()

        for feed in ("thread-erasures", "content-erasures"):
            assert client.post(f"/v1/internal/{feed}/ack", headers=key, json=[]).status_code == 422
        assert client.get("/v1/internal/account-erasures", headers=key).json()["owners"]
        assert (
            client.post(
                "/v1/jobs/native-removals/ack",
                headers={"Authorization": f"Bearer {token}"},
                json=[],
            ).status_code
            == 422
        )
        asyncio.run(drain(owner, token))

        for feed in ("result-removals", "thread-erasures", "content-erasures"):
            assert client.get(f"/v1/internal/{feed}", headers=key).json() == {"items": []}
        assert client.get("/v1/internal/account-erasures", headers=key).json() == {"owners": []}
        with studio.state.factory() as db:
            assert db.scalar(select(func.count()).select_from(Thread)) == len(deleted_threads)

    with state.connection() as db:
        assert db.execute("SELECT count(*) FROM erased_accounts").fetchone() == (101,)
        assert db.execute("SELECT count(*) FROM removed_results").fetchone() == (101,)
        assert db.execute("SELECT count(*) FROM erased_chats").fetchone() == (129,)
        assert db.execute(
            "SELECT count(*) FROM deleted_messages WHERE owner=?", (owner,)
        ).fetchone() == (66,)
        assert db.execute(
            "SELECT count(*) FROM deleted_messages WHERE owner=?", (other,)
        ).fetchone() == (258,)
    assert state.is_erased(erased_owners[-1])
    assert not state.is_erased(owner)
