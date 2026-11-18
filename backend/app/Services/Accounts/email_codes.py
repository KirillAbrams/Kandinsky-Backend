# Готовит проверочные коды к хранению и отправляет письма регистрации и восстановления. Исходный
# код получает адресат, а в базе остаётся отпечаток.

"""Создание отпечатка кода и доставка писем через SMTP."""

import hmac
import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from config.studio import Settings

CodeSender = Callable[[str, str], None]

log = logging.getLogger(__name__)
VALID_SECONDS = 15 * 60
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5


# Считает отпечаток из кода, номера аккаунта или назначения и закрытого ключа. Одинаковые цифры у
# разных пользователей или в разных сценариях дают разные отпечатки.
def code_digest(secret: str, user_id: str, code: str) -> str:
    """Связывает отпечаток кода с аккаунтом и закрытым ключом сервера"""
    return hmac.digest(secret.encode(), f"{user_id}:{code}".encode(), "sha256").hex()


# Готовит письмо нужного назначения и передаёт его почтовой службе по защищённому соединению. Код
# и данные входа в почту не уходят открытым текстом.
def send_code(settings: Settings, email: str, code: str, *, registration: bool = False) -> None:
    """Передаёт письмо через защищённое соединение с почтовой службой"""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = (
        "Подтверждение почты Кандинского"
        if registration
        else "Код для восстановления пароля Кандинского"
    )
    message["Date"] = formatdate(usegmt=True)
    sender_domain = parseaddr(settings.smtp_from)[1].rpartition("@")[2]
    message["Message-ID"] = make_msgid(domain=sender_domain or None)
    label = "Код регистрации" if registration else "Код восстановления"
    message.set_content(
        f"{label}: {code}\n\n"
        "Код действует 15 минут\n"
        "Если вы не запрашивали код, оставьте письмо без ответа\n"
    )
    context = ssl.create_default_context()
    if settings.smtp_security == "tls":
        connection = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=12, context=context
        )
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=12)
    with connection as mail:
        if settings.smtp_security == "starttls":
            mail.starttls(context=context)
        if settings.smtp_user:
            mail.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        mail.send_message(message)


# Вызывает рабочую отправку или подставной отправитель в проверках. При сбое записывает только вид
# ошибки, чтобы код и адрес не попали в журнал.
def deliver_reset_code(
    settings: Settings, sender: CodeSender | None, email: str, code: str
) -> None:
    """Записывает причину сбоя доставки без адреса и кода"""
    try:
        if sender is None:
            send_code(settings, email, code)
        else:
            sender(email, code)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        log.warning("password reset delivery failed type=%s", type(exc).__name__)
