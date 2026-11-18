# Проверяет запуск службы связи и совпадение состояния аккаунта в момент удаления. Ошибки здесь
# могли бы оставить старый телефон с непонятным отказом вместо очистки.

from uuid import UUID

from fastapi.testclient import TestClient

from app.Services.Database.Mobile.state import State
from bootstrap.bridge import from_environment
from tests.support.fixtures import (
    BRIDGE_TEST_PASSWORD,
    create_bridge,
    create_studio,
    register_mobile,
)


# Подставляет новый путь к базе и адрес сервера, затем запускает сборку приложения. Проверяет
# маршруты, закрытые права папки и пустую отметку нового владельца.
def test_bridge_factory_creates_state_directory_from_environment(tmp_path, monkeypatch):
    state_path = tmp_path / "private" / "bridge.sqlite"
    monkeypatch.setenv("KANDINSKY_UPSTREAM", "https://studio.test")
    monkeypatch.setenv("KANDINSKY_BRIDGE_STATE", str(state_path))
    monkeypatch.setenv("KANDINSKY_BRIDGE_ERASURE_KEY", "")
    app = from_environment()
    assert {"/health", "/v1/demo/login"}.issubset(app.openapi()["paths"])
    assert any(getattr(route, "path", None) == "/v1/ws" for route in app.routes)
    assert state_path.is_file()
    assert state_path.parent.stat().st_mode & 0o777 == 0o700
    assert State(state_path).cursor(str(UUID(int=1))) == 0


# Отмечает аккаунт удалённым только в местной базе, пока основной сервер ещё его узнаёт. Сверяет
# отдельный ответ об удалении и пустой список чатов, чтобы переход не вернул старую историю.
def test_bridge_reports_erased_local_account_during_upstream_overlap(tmp_path):
    studio = create_studio(
        database_url=f"sqlite:///{tmp_path}/studio.sqlite",
        media_dir=tmp_path / "media",
        provider="demo",
    )
    path = tmp_path / "bridge.sqlite"
    bridge = create_bridge(studio, path)
    with TestClient(studio) as primary, TestClient(bridge) as phone:
        email = "overlap@example.com"
        profile, auth = register_mobile(phone, email)
        login = primary.post(
            "/v1/auth/login", json={"email": email, "password": BRIDGE_TEST_PASSWORD}
        )
        assert login.status_code == 200
        State(path).erase_owners([login.json()["user_id"]])
        response = phone.post("/v1/devices/enroll", json={"invitation": profile["token"]})
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "account_deleted"}}
        assert phone.get("/v1/chats", headers=auth).json()["chats"] == []
