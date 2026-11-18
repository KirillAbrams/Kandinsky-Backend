# Собирает параметры запуска и безопасные значения по умолчанию для базы, почты и вычислений.
# Настройки из окружения позволяют одним исходникам работать дома и на сервере.

"""Настройки читаются из окружения
Секреты остаются на сервере"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://kandinsky_api@db:5432/studio"
    media_dir: Path = Path("./media")
    starter_history: bool = False
    provider: Literal["kandinsky", "demo"] = "kandinsky"
    gpu_url: str = "http://127.0.0.1:8099"
    gpu_token: str = ""
    video_sr_enabled: bool = False
    moderation_mode: Literal["off", "required"] = "off"
    moderation_origin: str = ""
    moderation_token: SecretStr = SecretStr("")
    moderation_timeout_seconds: float = Field(180, ge=10, le=900)
    session_hours: int = 168
    session_idle_minutes: int = Field(10080, ge=1, le=10080)
    max_upload_bytes: int = 20 * 1024 * 1024
    lease_seconds: int = 90
    cors_origins: list[str] = []
    password_reset_secret: SecretStr = SecretStr("")
    bridge_erasure_key: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "tls"] = "starttls"
    smtp_from: str = ""
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")

    # Если обязательная проверка содержимого включена, требует адрес службы и ключ. Адрес должен
    # содержать только схему и узел с возможным портом. Это помогает заметить ошибку настройки до
    # обработки первого запроса.
    @model_validator(mode="after")
    def require_private_classifier(self):
        if self.moderation_mode == "required":
            origin = urlsplit(self.moderation_origin)
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path not in {"", "/"}
                or origin.query
                or origin.fragment
                or not self.moderation_token.get_secret_value()
            ):
                raise ValueError("Required moderation needs an internal HTTP(S) origin and token")
        return self
