# Готовит поиск заданий в длинном чате и добавляет отметку для сверки файлов. Порядок по времени и
# номеру нужен, чтобы не пропускать задания с одинаковым временем.

"""Индекс для стабильного курсора длинной истории"""

import sqlalchemy as sa
from alembic import op

revision = "a76dc0b9f441"
down_revision = "03c41cc1579b"
branch_labels = None
depends_on = None


# Создаёт составной указатель по чату, времени и номеру задания для последовательного чтения длинной истории
# Добавляет файлам необязательный отпечаток содержимого, оставляя прежние записи совместимыми без заполнения этого поля
def upgrade():
    op.create_index("ix_jobs_history", "jobs", ["thread_id", "created_at", "id"])
    op.add_column("assets", sa.Column("sha256", sa.String(64), nullable=True))


# Удаляет добавленное поле отпечатка содержимого из таблицы файлов
# Затем убирает составной указатель истории, возвращая схему к предыдущей версии
def downgrade():
    op.drop_column("assets", "sha256")
    op.drop_index("ix_jobs_history", table_name="jobs")
