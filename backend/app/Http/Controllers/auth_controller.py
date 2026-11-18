# Подключает команды регистрации, входа, активности, выхода и удаления аккаунта. Передаёт операции
# службам, чтобы проверка входа и запись в базу не расходились между маршрутами.

"""Запросы входа и удаления учётной записи"""

from logging import Logger
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.Http.dependencies import CurrentUser, DatabaseSession, bearer
from app.Http.Requests.accounts import (
    AccountDeletion,
    Credentials,
    PasswordResetRequest,
    VerifiedRegistration,
)
from app.Services.Accounts.email_codes import CodeSender
from app.Services.Accounts.erasure import delete_owned_account
from app.Services.Accounts.login import login_with_password
from app.Services.Accounts.registration import create_account, request_registration_code
from app.Services.Accounts.sessions import record_activity, revoke_session
from config.studio import Settings


class AuthenticationController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Собирает обработчики входа вокруг общих настроек и способа отправки кода. Проверки могут
    # подставить отправителя, не меняя маршрут приложения.
    def __init__(
        self, settings: Settings, log: Logger, send_registration: CodeSender | None
    ) -> None:
        self._settings = settings
        self._log = log
        self._send_registration = send_registration

    # Приводит адрес к нижнему регистру и запрашивает код регистрации. Это позволяет считать
    # попытки и хранить код одинаково для разных написаний адреса.
    def request_code(self, body: PasswordResetRequest, db: DatabaseSession):
        return request_registration_code(
            db, self._settings, str(body.email).lower(), self._send_registration
        )

    # Передаёт адрес, пароль и код на проверку и создание аккаунта. Успешный ответ сразу содержит
    # данные для входа, поэтому приложение не просит вводить пароль заново.
    def register(self, body: VerifiedRegistration, db: DatabaseSession):
        return create_account(db, self._settings, str(body.email), body.password, body.code)

    # Учитывает попытку до проверки пароля и выдаёт ключ только после успешной проверки. Ошибочные
    # пароли тоже расходуют предел, поэтому повторять их бесконечно нельзя.
    def login(self, body: Credentials, db: DatabaseSession):
        """Проверяет пароль под той же блокировкой, что и его смена"""
        return login_with_password(db, str(body.email), body.password, self._settings.session_hours)

    # Передаёт ключ для обновления времени активности. Без ключа или при истёкшем сеансе обращение
    # не может продлить вход.
    def activity(
        self,
        db: DatabaseSession,
        auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if auth is None:
            raise HTTPException(401, "Войдите в аккаунт")
        record_activity(db, auth.credentials, self._settings.session_idle_minutes)
        return {"ok": True}

    # Возвращает номер и почту владельца, уже найденного по ключу входа. Клиент может убедиться,
    # что вход ещё действует, без повторной отправки пароля.
    @staticmethod
    def me(user: CurrentUser):
        return {"user_id": user.id, "email": user.email}

    # Передаёт номер вошедшего владельца и пароль на удаление. Номер берётся из проверенного
    # входа, поэтому команда не может указать другого владельца.
    def delete_account(self, body: AccountDeletion, user: CurrentUser, db: DatabaseSession):
        delete_owned_account(
            db, user.id, body.password.get_secret_value(), self._settings.media_dir, self._log
        )

    # Отзывает именно ключ текущего входа. Если бы приложение убирало его только у себя, сервер
    # продолжил бы принимать тот же ключ.
    @staticmethod
    def logout(
        user: CurrentUser,
        db: DatabaseSession,
        auth: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ):
        revoke_session(db, auth.credentials)
