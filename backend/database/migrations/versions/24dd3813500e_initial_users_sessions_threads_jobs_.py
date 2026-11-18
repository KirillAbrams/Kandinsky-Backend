# Создаёт первоначальную схему сервера. Пользователь, сеанс, чат, файл и задание связаны, чтобы
# история и очередь обработки могли опираться на одну базу.

"""Создание таблиц пользователей, сессий, чатов, заданий и файлов

Номер изменения 24dd3813500e
Предыдущее изменение
Дата создания 2026-11-10 19:36:36.857614

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Номера задают порядок применения изменений базы
revision: str = "24dd3813500e"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Создаёт исходные таблицы пользователей, сессий входа, чатов, файлов и заданий генерации со связями между ними
# Ограничивает допустимые состояния задания и запрещает повторный номер операции в пределах одного владельца
# Добавляет указатели для поиска по владельцу, чату и доступности задания для исполнителя
def upgrade() -> None:
    """Применить изменение базы"""
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("mime", sa.String(length=40), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_assets_user_id"), "assets", ["user_id"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("digest"),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_threads_user_id"), "threads", ["user_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("operation_key", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.String(length=2000), nullable=False),
        sa.Column("model", sa.String(length=20), nullable=False),
        sa.Column("aspect", sa.String(length=10), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("input_id", sa.String(length=36), nullable=True),
        sa.Column("output_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("lease_owner", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status in ('queued','running','succeeded','failed','cancelled')"),
        sa.ForeignKeyConstraint(
            ["input_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["output_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "operation_key"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "lease_until"], unique=False)
    op.create_index(op.f("ix_jobs_thread_id"), "jobs", ["thread_id"], unique=False)
    op.create_index(op.f("ix_jobs_user_id"), "jobs", ["user_id"], unique=False)


# Удаляет указатели и исходные таблицы в порядке, при котором сначала исчезают зависимые задания и сессии
# После удаления чатов и файлов убирает пользователей, возвращая базу к состоянию до первой версии схемы
def downgrade() -> None:
    """Вернуть прежнюю схему базы"""
    op.drop_index(op.f("ix_jobs_user_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_thread_id"), table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_threads_user_id"), table_name="threads")
    op.drop_table("threads")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_assets_user_id"), table_name="assets")
    op.drop_table("assets")
    op.drop_table("users")
