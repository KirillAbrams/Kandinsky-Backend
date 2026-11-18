# Подключает получение письма и применение кода восстановления. Без настроенного закрытого ключа и
# способа отправки восстановление не включается.

"""Маршруты получения кода и восстановления пароля."""

from fastapi import BackgroundTasks

from app.Http.dependencies import DatabaseSession
from app.Http.Requests.accounts import PasswordResetConfirm, PasswordResetRequest
from app.Services.Accounts.email_codes import CodeSender, deliver_reset_code
from app.Services.Accounts.password_reset import issue_reset_code, reset_password
from config.studio import Settings


class PasswordResetController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Передаёт двум обработчикам общие параметры проверки и отправки письма. Оба используют одинаковое
    # условие готовности, иначе сервер мог бы выдавать коды, которые сам не может подтвердить.
    def __init__(self, settings: Settings, sender: CodeSender | None) -> None:
        self._settings = settings
        self._sender = sender
        self._secret = settings.password_reset_secret.get_secret_value()
        self._enabled = len(self._secret) >= 32 and bool(
            sender is not None or (settings.smtp_host and settings.smtp_from)
        )

    # Запрашивает код с учётом паузы между письмами и отдаёт отправку фоновой задаче. Общий ответ
    # для любого адреса не раскрывает, зарегистрирован ли он.
    def request_code(self, body: PasswordResetRequest, tasks: BackgroundTasks, db: DatabaseSession):
        """Выдаёт код с задержкой между письмами и общим ответом для всех адресов"""
        email = str(body.email)
        code = issue_reset_code(db, email, secret=self._secret, enabled=self._enabled)
        if code is not None:
            tasks.add_task(deliver_reset_code, self._settings, self._sender, email.lower(), code)
        return {"message": "Если аккаунт есть, код придёт на почту"}

    # Передаёт код и новый пароль службе восстановления. Пустой успешный ответ приходит после
    # записи нового пароля и отзыва старых разрешений, поэтому клиент может предложить обычный
    # вход.
    def confirm_code(self, body: PasswordResetConfirm, db: DatabaseSession):
        """Меняет пароль и отзывает прежние разрешения после одноразовой проверки"""
        reset_password(
            db,
            str(body.email),
            body.code,
            body.password,
            secret=self._secret,
            enabled=self._enabled,
        )
