"""Проверяет размер и состав запроса к закрытой службе проверки содержимого."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.Services.Moderation.content_filter import FRAME_BATCH, MAX_FRAME_BYTES

MAX_BASE64 = ((MAX_FRAME_BYTES + 2) // 3) * 4


class TextBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Annotated[str, StringConstraints(min_length=1, max_length=2000)]] = Field(
        min_length=1, max_length=2
    )


class ImageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Annotated[str, StringConstraints(min_length=4, max_length=MAX_BASE64)]] = Field(
        min_length=1, max_length=FRAME_BATCH
    )
