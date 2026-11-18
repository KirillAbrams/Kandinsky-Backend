# Расширяет схему для восстановления пароля, удаления чатов и отслеживания заданий. Старые готовые
# файлы получают отметку, позволяющую отличить их от исходников.

"""Коды восстановления, отметки удалённых чатов и время первого запуска"""

import sqlalchemy as sa
from alembic import op

revision = "51d6a7e9c21f"
down_revision = "e52924a71d6a"
branch_labels = None
depends_on = None


# Добавляет записи восстановления пароля, отметки удаления чатов и время начала обработки задания
# Создаёт признак сгенерированного файла и заполняет его для уже имеющихся выходных результатов
# Добавляет очередь удаления содержимого и указатели видимых чатов, активных заданий и связанных исходных
# или выходных файлов
def upgrade():
    """Добавляет новые записи и указатели рядом с существующей историей"""
    op.create_table(
        "password_resets",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.add_column("threads", sa.Column("deleted_at", sa.Float(), nullable=True))
    op.add_column(
        "threads",
        sa.Column("custom_title", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("jobs", sa.Column("started_at", sa.Float(), nullable=True))
    op.add_column(
        "assets", sa.Column("generated", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.execute("UPDATE assets SET generated = true WHERE id IN (SELECT output_id FROM jobs)")
    op.create_table(
        "pending_media_removals", sa.Column("filename", sa.String(80), primary_key=True)
    )
    predicate = sa.text("deleted_at IS NULL")
    op.create_index(
        "ix_threads_visible_history",
        "threads",
        ["user_id", "created_at", "id"],
        postgresql_where=predicate,
        sqlite_where=predicate,
    )
    predicate = sa.text("status IN ('queued', 'running')")
    op.create_index(
        "ix_jobs_active_user",
        "jobs",
        ["user_id"],
        postgresql_where=predicate,
        sqlite_where=predicate,
    )
    op.create_index("ix_jobs_input_asset", "jobs", ["input_id"])
    op.create_index("ix_jobs_output_asset", "jobs", ["output_id"])


# Сначала удаляет указатели, зависящие от добавленных полей жизненного цикла
# Затем убирает очередь удаления файлов, новые признаки и записи восстановления пароля, сохраняя основные
# таблицы истории
def downgrade():
    """Убирает добавленные поля и восстанавливает прежний вид таблиц"""
    op.drop_index("ix_jobs_output_asset", table_name="jobs")
    op.drop_index("ix_jobs_input_asset", table_name="jobs")
    op.drop_index("ix_jobs_active_user", table_name="jobs")
    op.drop_index("ix_threads_visible_history", table_name="threads")
    op.drop_table("pending_media_removals")
    op.drop_column("assets", "generated")
    op.drop_column("jobs", "started_at")
    op.drop_column("threads", "custom_title")
    op.drop_column("threads", "deleted_at")
    op.drop_table("password_resets")
