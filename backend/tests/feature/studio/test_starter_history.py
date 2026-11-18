# Проверяет точный порядок начальных чатов, отдельную историю каждого владельца и очистку при
# нехватке исходников.

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.Models import Asset, Job, Thread, User
from database.seeders import starter_history
from tests.support.fixtures import MOBILE_FIXTURES, register_account
from tests.support.fixtures import create_studio as create_app


# Сверяет контрольные суммы с исходным порядком, проверяет сто девяносто два разных материала и
# десять тысяч записей, из них четыре тысячи с результатом. Без сверки можно было бы потерять
# порядок или повторить одни и те же материалы.
def test_published_history_keeps_the_original_sequence_and_distinct_media():
    catalog = starter_history.history_catalog()
    dense, mixed = catalog["threads"]
    assert [dense["title"], mixed["title"]] == ["Плотная медиалента", "Длинная история"]

    # Записывает последовательность в одном и том же виде и считает её контрольную сумму. Она
    # меняется даже при перестановке двух записей.
    def checksum(sequence):
        return hashlib.sha256(
            json.dumps(sequence, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    # Контрольные суммы последовательностей взяты из исходного плана Алматы 11 ноября.
    assert (
        checksum(dense["rows"])
        == "02ed0e27bf3c5ef0d03f40c7071154354175a4bb0f89244a7efa88dc57b18d27"
    )
    assert (
        checksum(mixed["rows"])
        == "f0d35014f37614426d8e3f96152d0d3c2ddff6bcfab6b977fcb911538a04112f"
    )
    assert len(dense["rows"]) == len(set(sha for _, sha, _ in dense["rows"])) == 192
    assert all(ready for _, _, ready in dense["rows"])
    assert len(mixed["rows"]) == 10_000
    assert sum(ready for _, _, ready in mixed["rows"]) == 4_000
    assert {sha for _, sha, _ in dense["rows"]} == set(catalog["assets"])


# Регистрирует два аккаунта с пробной длинной историей и сверяет записи, отдельные номера
# владельцев и общие исходные файлы. Чтение чужой копии должно получить отказ.
def test_every_new_account_gets_a_private_copy_of_long_history(tmp_path, monkeypatch):
    media = tmp_path / "media"
    originals = media / ".starter-history-almaty-v2"
    originals.mkdir(parents=True)
    source = MOBILE_FIXTURES / "real-media/backend-a8e5fcf563f44dad86a779abc5c6b3b7.jpg"
    content = source.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    filename = sha + ".jpg"
    (originals / filename).write_bytes(content)
    preview = dict(
        width=640, height=480, duration_seconds=None, has_audio=False, poster="", tiny=""
    )
    item = dict(
        model="image",
        aspect="4:3",
        name=filename,
        mime="image/jpeg",
        size=len(content),
        preview=preview,
    )
    monkeypatch.setattr(
        starter_history,
        "history_catalog",
        lambda: {
            "assets": {sha: item},
            "threads": [
                {"title": "Плотная медиалента", "rows": [["Фото", sha, True]]},
                {
                    "title": "Длинная история",
                    "rows": [["Фото", sha, True], ["Текст", sha, False]] * 5000,
                },
            ],
        },
    )
    app = create_app(database_url=f"sqlite:///{tmp_path}/db", media_dir=media, starter_history=True)
    with TestClient(app) as client:
        # Регистрирует владельца через обычный проверочный путь с кодом почты. Возвращает данные
        # доступа для чтения своей истории.
        def register(name):
            response = register_account(client, name + "@example.com", "example-long-password-41")
            assert response.status_code == 201
            profile = response.json()
            return profile["user_id"], {"Authorization": "Bearer " + profile["token"]}

        first, first_auth = register("first")
        second, second_auth = register("second")
        with app.state.factory() as db:
            for owner in (first, second):
                threads = db.scalars(
                    select(Thread)
                    .where(Thread.user_id == owner)
                    .order_by(Thread.activity_at.desc())
                ).all()
                assert [t.title for t in threads] == ["Плотная медиалента", "Длинная история"]
                assert db.scalar(select(Job.output_id).where(Job.thread_id == threads[0].id))
                thread = threads[1]
                assert thread.title == "Длинная история"
                counts = dict(
                    db.execute(
                        select(Job.status, func.count())
                        .where(Job.thread_id == thread.id)
                        .group_by(Job.status)
                    ).all()
                )
                assert counts == {"succeeded": 5000, "cancelled": 5000}
            first_file = db.scalars(select(Asset).where(Asset.user_id == first)).one()
            second_file = db.scalars(select(Asset).where(Asset.user_id == second)).one()
            assert first_file.filename != second_file.filename
            assert (media / first_file.filename).samefile(media / second_file.filename)
            asset, other = first_file.id, second_file.id
        assert client.get("/v1/assets/" + asset, headers=second_auth).status_code == 404
        assert client.get("/v1/assets/" + other, headers=second_auth).content == content
        assert client.get("/v1/assets/" + asset, headers=first_auth).content == content


# Убирает один исходник и меняет порядок материала в наборе. После отказа не должно остаться
# записей или созданных ссылок, а остальные исходники остаются для новой попытки.
@pytest.mark.parametrize("missing_first", [True, False])
def test_starter_history_rolls_back_files_and_records_if_an_original_is_missing(
    tmp_path, monkeypatch, missing_first
):
    media = tmp_path / "media"
    originals = media / starter_history.ORIGINALS_DIR
    originals.mkdir(parents=True)
    source = originals / "one.jpg"
    source.write_bytes(b"sample bytes")

    # Готовит описание одного пробного файла под указанным именем. Из таких записей складывается
    # малый набор для точной проверки ошибки.
    def item(name):
        return {
            "name": name,
            "mime": "image/jpeg",
            "size": 12,
            "preview": None,
            "model": "image",
            "aspect": "1:1",
        }

    entries = [("1" * 64, item(source.name)), ("2" * 64, item("missing.jpg"))]
    catalogue = dict(reversed(entries) if missing_first else entries)
    monkeypatch.setattr(
        starter_history,
        "history_catalog",
        lambda: {
            "assets": catalogue,
            "threads": [],
        },
    )
    app = create_app(database_url=f"sqlite:///{tmp_path}/db", media_dir=media, starter_history=True)
    with TestClient(app) as client:
        with pytest.raises(FileNotFoundError, match="missing.jpg"):
            register_account(client, "no-partial-history@example.com", "local-history-password-123")
        with app.state.factory() as db:
            for table in (User, Asset, Thread, Job):
                assert db.scalar(select(func.count()).select_from(table)) == 0
    assert list(media.iterdir()) == [originals]
    assert source.read_bytes() == b"sample bytes"
