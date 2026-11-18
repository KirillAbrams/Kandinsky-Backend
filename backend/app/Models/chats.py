# Описывает чаты, признаки видимости и ссылки на выбранные исходники. Связи с файлами позволяют
# понять, используется ли исходник в другом чате при удалении.

import time

from sqlalchemy import Float, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.Models.base import Base, new_id

RECENT_THREAD_PREDICATE = "deleted_at IS NULL AND archived_at IS NULL"


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        Index(
            "ix_threads_visible_history",
            "user_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_threads_library",
            "user_id",
            "archived_at",
            "activity_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_threads_recent_order",
            "user_id",
            text("(pinned_at IS NOT NULL) DESC"),
            text("activity_at DESC"),
            text("id DESC"),
            postgresql_where=text(RECENT_THREAD_PREDICATE),
            sqlite_where=text(RECENT_THREAD_PREDICATE),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="Новый чат")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    activity_at: Mapped[float] = mapped_column(Float, default=time.time, server_default=text("0"))
    pinned_at: Mapped[float | None] = mapped_column(Float)
    archived_at: Mapped[float | None] = mapped_column(Float)
    deleted_at: Mapped[float | None] = mapped_column(Float)
    custom_title: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    erasure_pending: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class ChatSource(Base):
    __tablename__ = "chat_sources"
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
