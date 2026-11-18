# Описывает страницу истории и признак наличия более старых сообщений. Телефон может понять, нужна
# ли следующая порция, даже когда текущая короткая.

from pydantic import BaseModel

from app.Http.Resources.generation import JobResponse


class HistoryPage(BaseModel):
    messages: list[JobResponse]
    has_older: bool
    has_newer: bool
