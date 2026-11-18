# Описывает сохранённые файлы, производные копии, временные разрешения и очередь очистки. Связи
# помогают отозвать разрешения и убрать только файлы, которые больше не нужны.

import time

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.Models.base import Base, new_id


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mime: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    generated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class PendingMediaRemoval(Base):
    __tablename__ = "pending_media_removals"
    filename: Mapped[str] = mapped_column(String(80), primary_key=True)


class Transfer(Base):
    __tablename__ = "transfers"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class UploadGrant(Base):
    __tablename__ = "upload_grants"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id"), index=True)


class AssetDerivative(Base):
    __tablename__ = "asset_derivatives"
    child_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    parent_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
