# Проверяет регистрацию и восстановление через маршруты телефона. Коды складываются в разные
# проверочные ящики, чтобы можно было сверить их назначение без почтового сервера.

from fastapi.testclient import TestClient

from tests.support.fixtures import create_bridge, create_studio


# Пытается использовать каждый тип кода в чужом сценарии и ждёт отказа. Затем меняет пароль своим
# кодом и проверяет, что доступ вернулся к тому же номеру владельца, а старый пароль больше не
# работает.
def test_mobile_registration_and_recovery_use_separate_codes(tmp_path):
    recovery = {}
    studio = create_studio(
        database_url=f"sqlite:///{tmp_path}/db",
        media_dir=tmp_path,
        password_reset_secret="fixture-mail-secret-" * 3,
        send_recovery=recovery.__setitem__,
    )
    registration = studio.state.registration_mailbox
    bridge = create_bridge(studio, tmp_path / "bridge.db")
    email = "mobile@example.com"
    credentials = {"email": email, "password": "first-mobile-password-76"}
    new = {"email": email, "password": "changed-mobile-password-87"}
    with TestClient(studio), TestClient(bridge) as client:
        assert client.post("/v1/demo/register/request", json={"email": email}).status_code == 202
        first_code = registration[email]
        assert (
            client.post(
                "/v1/demo/password-reset/confirm", json={**new, "code": first_code}
            ).status_code
            == 400
        )
        result = client.post("/v1/demo/register", json={**credentials, "code": first_code})
        assert result.status_code == 201
        assert (
            client.post("/v1/demo/password-reset/request", json={"email": email}).status_code == 202
        )
        assert (
            client.post("/v1/demo/register", json={**new, "code": recovery[email]}).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/demo/password-reset/confirm", json={**new, "code": recovery[email]}
            ).status_code
            == 204
        )
        assert client.post("/v1/demo/login", json=credentials).status_code == 401
        assert client.post("/v1/demo/login", json=new).json()["userId"] == result.json()["userId"]
