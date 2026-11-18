# Описывает задания, жалобы и записи доставляемых удалений. Отдельные состояния и номера попыток
# позволяют вернуть результат в очередь без потери истории и не восстановить удалённую работу.

import time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.Models.base import Base, new_id

ACTIVE_JOB_PREDICATE = "status IN ('queued', 'running')"


class ErasedGeneration(Base):
    __tablename__ = "erased_generations"
    __table_args__ = (UniqueConstraint("user_id", "operation_digest"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str] = mapped_column(String(36))
    operation_digest: Mapped[str] = mapped_column(String(64))


class PendingGpuRemoval(Base):
    __tablename__ = "pending_gpu_removals"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "operation_key"),
        Index("ix_jobs_batch_position", "batch_id", "batch_index", unique=True),
        CheckConstraint("status in ('queued','running','succeeded','failed','cancelled')"),
        Index("ix_jobs_claim", "status", "lease_until"),
        Index("ix_jobs_history", "thread_id", "created_at", "id"),
        Index(
            "ix_jobs_active_user",
            "user_id",
            postgresql_where=text(ACTIVE_JOB_PREDICATE),
            sqlite_where=text(ACTIVE_JOB_PREDICATE),
        ),
        Index("ix_jobs_input_asset", "input_id"),
        Index("ix_jobs_output_asset", "output_id"),
        Index(
            "ix_jobs_media_history",
            "thread_id",
            "created_at",
            "id",
            postgresql_where=text("output_id IS NOT NULL"),
            sqlite_where=text("output_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), index=True)
    operation_key: Mapped[str] = mapped_column(String(100))
    batch_id: Mapped[str | None] = mapped_column(String(36))
    batch_index: Mapped[int | None] = mapped_column(Integer)
    prompt_removed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    removed_output_id: Mapped[str | None] = mapped_column(String(36))
    fingerprint: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(String(2000))
    model: Mapped[str] = mapped_column(String(20))
    aspect: Mapped[str] = mapped_column(String(10))
    seed: Mapped[int] = mapped_column(Integer)
    quality: Mapped[str] = mapped_column(String(16), default="standard", server_default="standard")
    resolution: Mapped[str] = mapped_column(
        String(16), default="standard", server_default="standard"
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    negative_prompt: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    guidance_scale: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    output_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    moderated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    provider: Mapped[str] = mapped_column(String(20))
    # При каждой передаче задания выдаём новый номер. По нему отличаем текущего обработчика от предыдущего.
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float)
    progress: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(36))


class GenerationReport(Base):
    __tablename__ = "generation_reports"
    __table_args__ = (
        CheckConstraint("reason IN ('inappropriate', 'harmful', 'other')"),
        CheckConstraint("decision IN ('keep', 'remove', 'remove-and-suspend')"),
        CheckConstraint(
            "(decision IS NULL AND reviewed_at IS NULL AND reviewer IS NULL) OR "
            "(decision IS NOT NULL AND reviewed_at IS NOT NULL AND reviewer IS NOT NULL)"
        ),
        Index("ix_generation_reports_queue", "decision", "created_at", "job_id"),
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    decision: Mapped[str | None] = mapped_column(String(24))
    reviewed_at: Mapped[float | None] = mapped_column(Float)
    reviewer: Mapped[str | None] = mapped_column(String(80))


class ResultRemoval(Base):
    __tablename__ = "result_removals"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str | None] = mapped_column(String(36))


class JobContentRemoval(Base):
    __tablename__ = "job_content_removals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str] = mapped_column(String(36))
    root_id: Mapped[str] = mapped_column(String(36))
    member_id: Mapped[str] = mapped_column(String(36))
    asset_id: Mapped[str | None] = mapped_column(String(36))
    prompt: Mapped[bool]
    asset_only: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    retire_answer: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    revoke_asset: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
