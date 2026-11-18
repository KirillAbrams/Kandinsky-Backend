"""Данные, которые телефон передаёт этому разделу приложения."""

from pydantic import BaseModel, Field


class DeviceEnrollment(BaseModel):
    invitation: str = Field(pattern=r"^[0-9a-f]{64}$")


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)
