# Проверяет подтверждение почты при регистрации. Коды перехватываются внутри пробного приложения,
# чтобы проверять правила отправки без выхода на почтовый сервер.

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.Models import User
from bootstrap.studio import create_app
from tests.support.fixtures import create_studio

REQUEST = "/v1/auth/register/request"
REGISTER = "/v1/auth/register"
OWNER = {"email": "reader@example.com", "password": "independent-passphrase-77"}


# Запрашивает код и проверяет, что до подтверждения аккаунта и входа нет. Повтор письма ограничен,
# неверный код отклоняется, а два одновременных подтверждения дают только один аккаунт и один
# успешный вход.
def test_registration_requires_email_before_creating_account(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setenv("STUDIO_PASSWORD_RESET_SECRET", "fixture-only-secret-" * 3)
    monkeypatch.setenv("STUDIO_SMTP_HOST", "test.invalid")
    monkeypatch.setenv("STUDIO_SMTP_FROM", "mail@example.com")
    from app.Services.Accounts import email_codes

    monkeypatch.setattr(
        email_codes, "send_code", lambda _, addr, code, **kw: sent.update({addr: code})
    )
    with TestClient(
        create_app(database_url=f"sqlite:///{tmp_path}/db", media_dir=tmp_path)
    ) as client:
        assert client.post(REGISTER, json=OWNER).status_code == 422
        assert client.post(REQUEST, json={"email": OWNER["email"]}).status_code == 202
        code = sent.pop(OWNER["email"])
        client.post(REQUEST, json={"email": OWNER["email"]})
        assert sent == {}
        with client.app.state.factory() as db:
            assert db.query(User).count() == 0
        assert client.post("/v1/auth/login", json=OWNER).status_code == 401
        wrong = "00000000" if code != "00000000" else "11111111"
        assert client.post(REGISTER, json={**OWNER, "code": wrong}).status_code == 400
        with ThreadPoolExecutor(2) as pool:
            replies = list(
                pool.map(lambda _: client.post(REGISTER, json={**OWNER, "code": code}), range(2))
            )
        assert sorted(r.status_code for r in replies) == [201, 400]
        session = next(r.json()["token"] for r in replies if r.status_code == 201)
        assert client.post(REQUEST, json={"email": OWNER["email"]}).status_code == 202
        assert not sent
        assert (
            client.get("/v1/auth/me", headers={"Authorization": "Bearer " + session}).status_code
            == 200
        )


# Перебирает неверные коды до отказа и отдельно старит новый код. После обычной регистрации повтор
# по занятому адресу не отправляет письмо, не меняет пароль и не отзывает уже выданный сеанс.
def test_expiration_attempt_budget_and_existing_sessions(tmp_path):
    import time

    from app.Models import RegistrationCode
    from tests.support.fixtures import register_account

    app = create_studio(database_url=f"sqlite:///{tmp_path}/db", media_dir=tmp_path)
    email = OWNER["email"]
    with TestClient(app) as client:
        client.post(REQUEST, json={"email": email})
        code = app.state.registration_mailbox.pop(email)
        wrong = "22222222" if code != "22222222" else "33333333"
        for _ in range(5):
            assert client.post(REGISTER, json={**OWNER, "code": wrong}).status_code == 400
        assert client.post(REGISTER, json={**OWNER, "code": code}).status_code == 400
        with app.state.factory.begin() as db:
            db.get(RegistrationCode, email).requested_at = time.time() - 1000
        client.post(REQUEST, json={"email": email})
        fresh = app.state.registration_mailbox.pop(email)
        with app.state.factory.begin() as db:
            db.get(RegistrationCode, email).requested_at = time.time() - 1000
        assert client.post(REGISTER, json={**OWNER, "code": fresh}).status_code == 400

        token = register_account(client, email, OWNER["password"]).json()["token"]
        assert client.post(REQUEST, json={"email": email}).status_code == 202
        assert not app.state.registration_mailbox
        assert (
            client.post(
                REGISTER, json={**OWNER, "password": "different-password-33", "code": fresh}
            ).status_code
            == 400
        )
        assert (
            client.get("/v1/auth/me", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )
        assert client.post("/v1/auth/login", json=OWNER).status_code == 200


# Дважды запрашивает код при отказе почты. Оба запроса сообщают о недоступности, а в базе нет
# пользователя. Иначе после неотправленного письма человек мог бы ждать срока повтора.
def test_registration_delivery_failure_allows_immediate_retry(tmp_path):
    # Имитирует отказ отправки сразу после передачи адреса и кода. Проверка может повторить сбой
    # без почты и без задержек.
    def unavailable(email, code):
        raise OSError("smtp unavailable")

    with TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path}/db",
            media_dir=tmp_path,
            password_reset_secret="isolated-mail-secret-" * 3,
            send_registration=unavailable,
        )
    ) as client:
        for _ in range(2):
            assert client.post(REQUEST, json={"email": OWNER["email"]}).status_code == 503
        with client.app.state.factory() as db:
            assert db.query(User).count() == 0


# Убирает настройки почты и проверяет отказ запроса регистрации, её подтверждения и смены пароля.
# В базе не появляется аккаунт, иначе отсутствие настройки обходило бы проверку почты.
def test_mail_flows_report_unavailable_without_configured_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_SMTP_HOST", "")
    monkeypatch.setenv("STUDIO_SMTP_FROM", "")
    with TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path}/db",
            media_dir=tmp_path,
            password_reset_secret="",
        )
    ) as client:
        assert client.post(REQUEST, json={"email": OWNER["email"]}).status_code == 503
        payload = {**OWNER, "code": "12345678"}
        assert client.post(REGISTER, json=payload).status_code == 503
        assert client.post("/v1/auth/password-reset/confirm", json=payload).status_code == 503
        with client.app.state.factory() as db:
            assert db.query(User).count() == 0


# Выдаёт код и до его подтверждения занимает тот же адрес другой записью. Позднее подтверждение
# должно вернуть конфликт, сохранив пароль первого пользователя.
def test_parallel_account_creation_preserves_the_first_account(tmp_path):
    app = create_studio(database_url=f"sqlite:///{tmp_path}/db", media_dir=tmp_path)
    with TestClient(app) as client:
        email = OWNER["email"]
        assert client.post(REQUEST, json={"email": email}).status_code == 202
        code = app.state.registration_mailbox.pop(email)
        with app.state.factory.begin() as db:
            db.add(User(email=email, password_hash="isolated-existing-account"))
        result = client.post(REGISTER, json={**OWNER, "code": code})
        assert result.status_code == 409
        with app.state.factory() as db:
            assert db.query(User).one().password_hash == "isolated-existing-account"
