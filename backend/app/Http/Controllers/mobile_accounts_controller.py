# Здесь подключаются команды аккаунта для телефона и форма удаления для браузера. Почту, пароль и
# коды проверяет основной сервер. Служба связи переводит ответ в вид, понятный телефону.

"""Регистрация, вход, восстановление доступа и удаление аккаунта."""

import time
import uuid
from collections.abc import Callable

from fastapi import Request
from fastapi.responses import Response

from app.Http.Controllers.account_web_controller import delete_from_browser, deletion_page
from app.Http.Requests.accounts import (
    AccountDeletion,
    PasswordResetConfirm,
    PasswordResetRequest,
    VerifiedRegistration,
)
from app.Http.Requests.Mobile.accounts import Credentials, DeviceEnrollment
from app.Services.Database.Mobile.state import State
from app.Services.Mobile.adapter import Adapter
from app.Services.Mobile.session_tokens import pack_token, token_from


class MobileAccountsController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Подключает все команды аккаунта к службе связи. Удаление включается при наличии служебного
    # ключа, чтобы запрос можно было довести до очистки копий.
    def __init__(self, adapter: Callable[[], Adapter], state: State) -> None:
        self._adapter = adapter
        self._state = state

    # Пересылает вход или подтверждённую регистрацию основному серверу, затем выдаёт телефону его
    # номер пользователя и новый номер устройства. Преобразует ключ в вид, который телефон сможет
    # сохранить и отправить обратно.
    async def login(self, value, endpoint):
        item = await self._adapter().studio.request(
            "POST", f"/v1/auth/{endpoint}", json=value.model_dump()
        )
        owner = item["user_id"]
        return {
            "userId": self._state.identity(owner, "user", owner),
            "deviceId": str(uuid.uuid4()),
            "name": item["email"],
            "token": pack_token(item["token"]),
            "expiresAtMs": round((time.time() + 7 * 86400) * 1000),
        }

    # Передаёт адрес основному серверу для отправки кода регистрации. Аккаунт создаётся позже,
    # когда пользователь подтвердит, что письмо пришло ему.
    async def request_registration(self, body: PasswordResetRequest):
        return await self._adapter().studio.request(
            "POST", "/v1/auth/register/request", json=body.model_dump()
        )

    # Просит основной сервер отправить код для смены пароля. Сам пароль на этом шаге остаётся
    # прежним, поэтому одного адреса недостаточно для доступа к аккаунту.
    async def request_password_reset(self, body: PasswordResetRequest):
        return await self._adapter().studio.request(
            "POST", "/v1/auth/password-reset/request", json=body.model_dump()
        )

    # Передаёт введённый код и новый пароль основному серверу. Пустой успешный ответ позволяет
    # телефону перейти к обычному входу после проверки.
    async def confirm_password_reset(self, body: PasswordResetConfirm):
        await self._adapter().studio.request(
            "POST", "/v1/auth/password-reset/confirm", json=body.model_dump()
        )
        return Response(status_code=204)

    # Завершает регистрацию через общий обработчик входа. После проверки кода телефон получает
    # ключ сразу, без отдельного запроса входа.
    async def register(self, value: VerifiedRegistration):
        return await self.login(value, "register")

    # Проверяет введённые данные через общий обработчик входа. Телефон получает тот же набор
    # полей, что после регистрации, и одинаково открывает историю в обоих случаях.
    async def sign_in(self, value: Credentials):
        return await self.login(value, "login")

    # Проверяет приглашение как ключ доступа, сверяет владельца и выдаёт отдельный номер
    # устройству. Проверка нужна, чтобы старое либо отозванное приглашение не открыло аккаунт.
    async def enroll(self, value: DeviceEnrollment):
        owner, token = await self._adapter().owner({"authorization": "Bearer " + value.invitation})
        item = await self._adapter().studio.request("GET", "/v1/auth/me", token)
        return {
            "userId": self._state.identity(owner, "user", owner),
            "deviceId": str(uuid.uuid4()),
            "name": item["email"],
            "token": value.invitation,
            "expiresAtMs": round((time.time() + 7 * 86400) * 1000),
        }

    # Передаёт основному серверу сведения о действии пользователя под его ключом входа. Так срок
    # бездействия учитывается на стороне, которая решает, когда закончить сеанс.
    async def user_activity(self, request: Request):
        return await self._adapter().studio.request(
            "POST", "/v1/auth/activity", token_from(request.headers)
        )

    # Передаёт запрос на отзыв ключа основному серверу и ждёт ответа. Очистка только на телефоне
    # оставила бы этот ключ действующим для сетевых запросов.
    async def studio_logout(self, request: Request):
        await self._adapter().studio.request("POST", "/v1/auth/logout", token_from(request.headers))
        return Response(status_code=204)

    # Определяет владельца по ключу входа и передаёт его пароль на удаление. Владелец берётся
    # из проверенного входа, чтобы команду нельзя было направить на чужой аккаунт.
    async def erase_account(self, request: Request, body: AccountDeletion):
        service = self._adapter()
        owner, token = await service.owner(request.headers)
        await service.delete_account(owner, token, body.password.get_secret_value())
        return Response(status_code=204)

    # Открывает в браузере форму со свежей отметкой. Любой пользователь может получить
    # страницу, а удаление начинается только после проверки отправленной формы.
    @staticmethod
    async def deletion_form():
        return deletion_page()

    # Передаёт заполненную форму проверке браузерного запроса и возвращает страницу с
    # результатом. Обработчик формы использует ту же службу удаления, что и телефон.
    async def submit_deletion_form(self, request: Request):
        return await delete_from_browser(request, self._adapter())
