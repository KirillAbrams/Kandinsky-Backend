# Описывает команды создания и изменения чатов. Проверка полей помогает отклонять пустые изменения
# до обращения к базе.

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ThreadRequest(BaseModel):
    id: UUID


class ThreadPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=100)
    pinned: bool | None = None
    archived: bool | None = None

    # Убирает пробелы по краям и отклоняет название, состоящее только из пробелов. Без проверки в
    # списке могла бы появиться строка без видимого заголовка.
    @field_validator("title")
    @classmethod
    def trim_title(cls, value):
        """Сохраняет название без пробелов по краям и пустых строк"""
        if value is None or not value.strip():
            raise ValueError("Введите название чата")
        return value.strip()

    # Требует хотя бы одного изменения названия, закрепления или архива. Пустой запрос не должен
    # выглядеть как успешное изменение чата.
    @model_validator(mode="after")
    def require_change(self):
        """Отклоняет запрос, в котором нечего менять"""
        if self.title is None and self.pinned is None and self.archived is None:
            raise ValueError("Выберите действие с чатом")
        return self


class ThreadDeletionLookup(BaseModel):
    ids: list[UUID] = Field(max_length=200)
