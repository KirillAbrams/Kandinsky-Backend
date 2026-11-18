# Готовит хранилище закрепления, архива и недавней активности. Без этих полей телефоны не смогли
# бы согласованно показать порядок списка.

"""Порядок недавних чатов, закрепление и архив"""

import sqlalchemy as sa
from alembic import op

revision = "c630dac0df8e"
down_revision = "51d6a7e9c21f"
branch_labels = None
depends_on = None


# Добавляет время активности, закрепления и переноса в архив для списка чатов
# Начальную активность вычисляет по последнему заданию, а у пустого чата использует время его создания
# Создаёт указатель владельца, архива и активности только для чатов без отметки удаления
def upgrade():
    """Сохраняет время последней отправки и добавляет признаки списка"""
    op.add_column(
        "threads", sa.Column("activity_at", sa.Float(), nullable=False, server_default="0")
    )
    op.add_column("threads", sa.Column("pinned_at", sa.Float(), nullable=True))
    op.add_column("threads", sa.Column("archived_at", sa.Float(), nullable=True))
    op.execute(
        "UPDATE threads SET activity_at = COALESCE("
        "(SELECT MAX(jobs.created_at) FROM jobs WHERE jobs.thread_id = threads.id), created_at)"
    )
    active = sa.text("deleted_at IS NULL")
    op.create_index(
        "ix_threads_library",
        "threads",
        ["user_id", "archived_at", "activity_at", "id"],
        postgresql_where=active,
        sqlite_where=active,
    )


# Удаляет указатель списка чатов и поля архива, закрепления и последней активности
# Основные записи чатов, задания и файлы сохраняются в составе предыдущей схемы
def downgrade():
    """Возвращает прежнюю схему списка, сохраняя историю и файлы"""
    op.drop_index("ix_threads_library", table_name="threads")
    for field in ("archived_at", "pinned_at", "activity_at"):
        op.drop_column("threads", field)
