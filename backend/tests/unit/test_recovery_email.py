# Проверяет построение письма и порядок защищённой отправки без настоящей почты. Также сверяет,
# что ошибка доставки не копирует адрес и код в журнал.

from contextlib import nullcontext
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from app.Services.Accounts.email_codes import deliver_reset_code, send_code
from config.studio import Settings


# Собирает два письма и проверяет дату с часовым поясом и разные номера сообщений. Повторная
# выдача кода не должна выглядеть для почтовой службы тем же письмом.
def test_recovery_email_has_date_and_unique_message_id(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "app.Services.Accounts.email_codes.smtplib.SMTP_SSL",
        lambda *args, **kwargs: nullcontext(SimpleNamespace(send_message=messages.append)),
    )
    settings = Settings(
        smtp_host="mail.example.com",
        smtp_security="tls",
        smtp_from="Студия <reset@example.com>",
        smtp_user="",
    )
    send_code(settings, "owner@example.com", "12345678")
    send_code(settings, "owner@example.com", "87654321")

    assert all(parsedate_to_datetime(str(message["Date"])).tzinfo for message in messages)
    identifiers = {str(message["Message-ID"]) for message in messages}
    assert len(identifiers) == 2 and all(value.endswith("@example.com>") for value in identifiers)


# Запоминает этапы соединения и сверяет, что защита с проверкой узла включена раньше входа, а
# письмо отправлено последним. Проверяет адресата и ожидаемый текст.
def test_default_delivery_negotiates_starttls_before_login_and_sending(monkeypatch):
    operations = []
    settings = Settings(
        smtp_host="mail.example.com",
        smtp_port=1587,
        smtp_security="starttls",
        smtp_from="reset@example.com",
        smtp_user="fixture-user",
        smtp_password="fixture-secret",
    )

    # Сверяет настройки соединения и подставляет почтового собеседника с журналом действий. Это
    # позволяет подтвердить порядок без отправки настоящего письма.
    def connect(host, port, *, timeout):
        assert (host, port, timeout) == ("mail.example.com", 1587, 12)
        return nullcontext(
            SimpleNamespace(
                starttls=lambda *, context: operations.append(("tls", context.check_hostname)),
                login=lambda user, password: operations.append(("login", user, password)),
                send_message=lambda message: operations.append(("send", message)),
            )
        )

    monkeypatch.setattr("app.Services.Accounts.email_codes.smtplib.SMTP", connect)
    deliver_reset_code(settings, None, "owner@example.com", "12345678")
    assert operations[:2] == [("tls", True), ("login", "fixture-user", "fixture-secret")]
    operation, message = operations[2]
    assert operation == "send" and len(operations) == 3
    assert message["To"] == "owner@example.com"
    assert "Код восстановления: 12345678" in message.get_content()


# Передаёт ошибку с чувствительными полями и сверяет журнал. Должен остаться только вид отказа,
# иначе журнал раскроет данные восстановления.
def test_delivery_error_logs_only_type_and_keeps_address_and_code_private(caplog):
    # Создаёт отказ, в текст которого намеренно попали адрес и код. Если обработчик запишет его
    # целиком, проверка увидит утечку.
    def unavailable(address, code):
        raise OSError(f"Failed at {address} with code {code}")

    deliver_reset_code(Settings(), unavailable, "owner@example.com", "12345678")
    assert "password reset delivery failed type=OSError" in caplog.text
    assert "owner@example.com" not in caplog.text
    assert "12345678" not in caplog.text
