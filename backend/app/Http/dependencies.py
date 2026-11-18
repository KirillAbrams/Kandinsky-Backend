# Открывает сеансы базы для запросов и проверяет, под каким пользователем пришёл вызов. Раздельная
# проверка для ожидания событий позволяет освободить базу ещё до ожидания.

"""Сеанс базы и текущий пользователь для обработчиков HTTP."""

import time
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.Models import ErasedSession, User
from app.Models import Session as StoredSession
from app.Services.Accounts.sessions import digest

bearer = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


# Открывает сеанс из настроек текущего приложения и отдаёт его обработчику на время запроса.
# Закрытие после запроса возвращает соединение и освобождает незавершённую запись.
def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.factory() as db:
        yield db


DatabaseSession = Annotated[Session, Depends(get_db)]


# Ищет сеанс по отпечатку ключа, проверяет общий срок и время бездействия. Если аккаунт удалён,
# передаёт отдельный ответ, чтобы телефон убрал его сохранённые данные.
def _user_for_session(
    db: Session, auth: HTTPAuthorizationCredentials | None, idle_minutes: int
) -> User:
    token_digest = digest(auth.credentials) if auth else None
    now = time.time()
    session = db.get(StoredSession, token_digest) if token_digest else None
    if session and session.expires_at > now and session.last_activity_at > now - idle_minutes * 60:
        if user := db.get(User, session.user_id):
            return user
    # По этому признаку телефон удаляет местные данные уже удалённого аккаунта.
    erased = db.get(ErasedSession, token_digest) if token_digest else None
    code = "account_deleted" if erased and erased.expires_at > now else "Войдите в аккаунт"
    raise HTTPException(401, code, headers={"WWW-Authenticate": "Bearer"})


# Подставляет в проверку настроенный срок бездействия и возвращает найденного владельца.
# Обработчику не приходится верить номеру владельца, пришедшему от клиента.
def get_current_user(request: Request, db: DatabaseSession, auth: BearerCredentials) -> User:
    return _user_for_session(db, auth, request.app.state.settings.session_idle_minutes)


CurrentUser = Annotated[User, Depends(get_current_user)]


# Проверяет вход в отдельном коротком сеансе и забирает только номер владельца. После закрытия
# соединения запрос может долго ждать события, не занимая базу.
def get_removal_owner(request: Request, auth: BearerCredentials) -> str:
    # Освобождаем соединение до того, как обработчик начнёт ждать событий.
    with request.app.state.factory() as db:
        return _user_for_session(db, auth, request.app.state.settings.session_idle_minutes).id
