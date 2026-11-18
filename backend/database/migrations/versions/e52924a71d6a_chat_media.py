# Добавляет быстрый поиск готовых файлов в чате. Галерея должна читать только задания с файлом,
# иначе ей пришлось бы просматривать и пустые результаты.

"""Индекс результатов с медиа для галереи чата"""

import sqlalchemy as sa
from alembic import op

revision = "e52924a71d6a"
down_revision = "c41e879b06aa"
branch_labels = None
depends_on = None


# Создаёт составной указатель по чату, времени и номеру только для заданий с выходным файлом
# Так галерея может читать готовые результаты по порядку без включения заданий, у которых файл ещё отсутствует
def upgrade():
    op.create_index(
        "ix_jobs_media_history",
        "jobs",
        ["thread_id", "created_at", "id"],
        postgresql_where=sa.text("output_id IS NOT NULL"),
        sqlite_where=sa.text("output_id IS NOT NULL"),
    )


# Удаляет отдельный указатель истории готовых файлов
# Содержимое таблицы заданий и общий указатель истории остаются доступными в предыдущей схеме
def downgrade():
    op.drop_index("ix_jobs_media_history", table_name="jobs")
