# Добавляет параметры генерации и сведения об обложке файла. Сохранённая обложка нужна для
# быстрого показа результата при возврате в историю.

"""Параметры генерации и постоянный постер для возврата в историю"""

import sqlalchemy as sa
from alembic import op

revision = "c41e879b06aa"
down_revision = "a76dc0b9f441"
branch_labels = None
depends_on = None


# Добавляет сохранённые настройки качества, размера, длительности, исключаемого содержимого и силы следования запросу
# Для обязательных полей задаёт исходные значения, чтобы прежние задания оставались пригодными для чтения
# В таблице файлов создаёт необязательное описание уменьшенной копии для быстрого возврата в историю
def upgrade():
    for column in [
        sa.Column("quality", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("resolution", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("negative_prompt", sa.String(2000), nullable=False, server_default=""),
        sa.Column("guidance_scale", sa.Float(), nullable=True),
    ]:
        op.add_column("jobs", column)
    op.add_column("assets", sa.Column("preview_info", sa.JSON(), nullable=True))


# Удаляет описание уменьшенной копии из файлов и добавленные настройки из заданий
# Сохраняет сами задания, файлы и прежние поля, возвращая состав столбцов предыдущей схемы
def downgrade():
    op.drop_column("assets", "preview_info")
    for name in ["guidance_scale", "negative_prompt", "duration_seconds", "resolution", "quality"]:
        op.drop_column("jobs", name)
