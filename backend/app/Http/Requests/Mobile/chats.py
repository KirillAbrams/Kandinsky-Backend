"""Данные, которые телефон передаёт этому разделу приложения."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class GenerationAction(BaseModel):
    action: Literal["check", "cancel", "retry", "retry_slot"]
    operation: uuid.UUID | None = None
    slot: uuid.UUID | None = None
    expected_run: uuid.UUID | None = None


class ChatStateLookup(BaseModel):
    ids: list[int] = Field(max_length=128)


class CreateNativeChat(BaseModel):
    id: uuid.UUID | None = None
