# Проверяет постраничное чтение полной истории и отдельный переход по материалам. Границы страниц
# и права владельца важны при восстановлении места чтения.

"""Готовим историю из десяти тысяч записей для проверки чтения страниц"""

from sqlalchemy import select, update

from app.Models import Asset, Job, User
from tests.support.fixtures import account, seed_history


# Читает десять тысяч записей в обе стороны и запрашивает окно вокруг середины. У записей
# одинаковое время, поэтому без устойчивого порядка пропуск или повтор проявился бы на границе
# страниц.
def test_full_10000_history_both_directions_and_equal_timestamps(client):
    h = account(client)
    thread, expected = seed_history(client, h)
    path = f"/v1/threads/{thread}/history"
    response = client.get(path, headers=h)
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page["messages"]) == 64
    assert [x["id"] for x in page["messages"]] == expected[-64:]
    seen = []
    while True:
        seen = [x["id"] for x in page["messages"]] + seen
        if not page["has_older"]:
            break
        page = client.get(path, params={"before": page["messages"][0]["id"]}, headers=h).json()
        assert 0 < len(page["messages"]) <= 64
    assert seen == expected
    page = client.get(path, params={"around": expected[0]}, headers=h).json()
    newer = [x["id"] for x in page["messages"]]
    while page["has_newer"]:
        page = client.get(path, params={"after": page["messages"][-1]["id"]}, headers=h).json()
        newer += [x["id"] for x in page["messages"]]
    assert newer == expected
    middle = client.get(path, params={"around": expected[5000]}, headers=h).json()
    assert expected[5000] in [x["id"] for x in middle["messages"]]
    assert len(middle["messages"]) <= 64
    assert middle["has_older"] and middle["has_newer"]


# Запрашивает чужой чат, чужую границу и недопустимые размеры окна. Проверяет, что числа из
# запроса ограничены и не дают читать историю другого владельца.
def test_cursor_and_thread_stay_private_and_inputs_are_bounded(client):
    h = account(client)
    thread, ids = seed_history(client, h, 80)
    other = account(client, "other@example.com")
    path = f"/v1/threads/{thread}/history"
    assert client.get(path, headers=other).status_code == 404
    other_thread = client.post("/v1/threads", headers=other).json()["id"]
    assert (
        client.get(
            f"/v1/threads/{other_thread}/history", params={"before": ids[20]}, headers=other
        ).status_code
        == 404
    )
    another_owned_thread = client.post("/v1/threads", headers=h).json()["id"]
    assert (
        client.get(
            f"/v1/threads/{another_owned_thread}/history", params={"before": ids[20]}, headers=h
        ).status_code
        == 404
    )
    assert (
        client.get(path, headers=h, params={"around": ids[21], "media_only": True}).status_code
        == 404
    )
    for params in [
        {"limit": 0},
        {"limit": 129},
        {"before": "bad"},
        {"before": ids[0], "after": ids[1]},
    ]:
        assert client.get(path, params=params, headers=h).status_code == 422


# Запрашивает только записи с файлами в истории, где между ними есть пустые записи. Сверяет шаг в
# обе стороны и признаки продолжения списка, затем проверяет отказ чужому пользователю.
def test_media_cursor_crosses_sparse_history_in_both_directions(client):
    headers = account(client)
    thread, ids = seed_history(client, headers, 400)
    media = [ids[4], ids[220], ids[395]]
    with client.app.state.factory() as db:
        owner = db.scalar(select(User.id).where(User.email == "one@example.com"))
        asset = Asset(user_id=owner, mime="image/png", size=1, filename="test.png")
        db.add(asset)
        db.flush()
        db.execute(
            update(Job).where(Job.id.in_(media)).values(status="succeeded", output_id=asset.id)
        )
        db.commit()
    path = f"/v1/threads/{thread}/history"
    for boundary, expected, older, newer in [
        ({"before": media[2]}, media[1], True, True),
        ({"after": media[0]}, media[1], True, True),
        ({"after": media[1]}, media[2], True, False),
        ({"before": media[1]}, media[0], False, True),
    ]:
        response = client.get(
            path, headers=headers, params={**boundary, "media_only": True, "limit": 1}
        )
        assert response.status_code == 200
        page = response.json()
        assert [row["id"] for row in page["messages"]] == [expected]
        assert (page["has_older"], page["has_newer"]) == (older, newer)
    other = account(client, "other@example.com")
    assert client.get(path, headers=other, params={"media_only": True}).status_code == 404
