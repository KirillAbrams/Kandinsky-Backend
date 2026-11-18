# Описывает допустимые параметры задания и жалобы на результат. Проверки отсекают сочетания,
# которые исполнитель не сможет обработать.

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.Enums.moderation import ReportReason


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str
    prompt: str = Field(min_length=1, max_length=2000)
    model: Literal["image", "video", "video_sr"] = "image"
    aspect: Literal["1:1", "5:3", "3:5"] = "1:1"
    seed: int = Field(default=42, ge=0, le=2147483647)
    input_id: str | None = None
    quality: Literal["draft", "standard"] = "standard"
    resolution: Literal["compact", "standard"] = "standard"
    duration_seconds: Literal[1, 3, 5] = 5
    negative_prompt: str = Field(default="", max_length=2000)
    guidance_scale: float | None = Field(default=None, ge=1, le=8)

    # Сверяет исходный файл, длительность и настройки с выбранной моделью. Так неподдерживаемое
    # задание не попадает в очередь и пользователь получает ответ сразу.
    @model_validator(mode="after")
    def supported_combination(self) -> Self:
        if self.model == "video_sr" and (
            not self.input_id
            or self.quality != "standard"
            or self.resolution != "standard"
            or self.duration_seconds != 5
            or self.negative_prompt
            or self.guidance_scale is not None
        ):
            raise ValueError("Для повышения разрешения нужен MP4. Масштаб ×2.")
        if self.model == "image" and self.duration_seconds != 5:
            raise ValueError("У изображения нет длительности")
        if self.model == "video" and self.input_id is None:
            if (
                self.quality != "standard"
                or self.negative_prompt
                or self.guidance_scale not in (None, 1.0)
            ):
                raise ValueError("Video Lite использует 16 шагов и guidance=1")
        return self

    # Снимает пробелы по краям и проверяет, что осталось описание. Запрос из одних пробелов иначе
    # прошёл бы проверку длины.
    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Опишите, что создать")
        return value


class BatchJobRequest(JobRequest):
    count: int = Field(ge=2, le=4, strict=True)

    # Запрещает пакетное увеличение разрешения ролика. Этот режим принимает один исходник и отдаёт
    # один файл, поэтому группа обещала бы лишние результаты.
    @model_validator(mode="after")
    def supported_batch(self) -> Self:
        if self.model == "video_sr":
            raise ValueError("Повышение разрешения создаёт один результат")
        return self


class ResultRemovalAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[UUID] = Field(min_length=1, max_length=100)


class GenerationReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: ReportReason
