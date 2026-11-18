# Запоминает удаление запроса или его результата для доставки в историю телефона. Без этих отметок
# старый снимок чата мог бы снова показать удалённую строку.

"""Сохранение отметок удаления содержимого и их доставка в нативную историю"""

import sqlalchemy as sa
from alembic import op

revision = "fe8c0d7b94a2"
down_revision = "0c0cc971ce12"
branch_labels = None
depends_on = None


# Добавляет отметку удаления запроса и номер удалённого выходного файла в сохранённое задание
# Создаёт очередь доставки удалений с владельцем, чатом, группой, отдельным заданием и выбранным файлом
# Указатель владельца позволяет получать его изменения для очистки истории на устройствах
def upgrade():
    op.add_column(
        "jobs", sa.Column("prompt_removed", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("jobs", sa.Column("removed_output_id", sa.String(36)))
    op.create_table(
        "job_content_removals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("root_id", sa.String(36), nullable=False),
        sa.Column("member_id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.String(36)),
        sa.Column("prompt", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_job_content_removals_owner_id", "job_content_removals", ["owner_id"])


# Удаляет таблицу доставки удалений содержимого вместе с её указателем
# Затем убирает добавленные поля задания, возвращая предыдущую схему без этих отметок
def downgrade():
    op.drop_table("job_content_removals")
    op.drop_column("jobs", "removed_output_id")
    op.drop_column("jobs", "prompt_removed")
