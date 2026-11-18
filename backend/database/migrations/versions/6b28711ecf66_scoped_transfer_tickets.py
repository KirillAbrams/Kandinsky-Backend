# Добавляет временное разрешение на скачивание выбранного файла. Только отпечаток ключа и срок
# остаются в базе, а сам файл хранится отдельно.

"""Временные разрешения на скачивание отдельных файлов

Номер изменения 6b28711ecf66
Предыдущее изменение 24dd3813500e
Дата создания 2026-11-10 19:41:24.672011

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Номера задают порядок применения изменений базы
revision: str = "6b28711ecf66"
down_revision: Union[str, Sequence[str], None] = "24dd3813500e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Создаёт таблицу временных разрешений на скачивание с отпечатком ключа, номером файла и сроком действия
# Связь с таблицей файлов не позволяет сохранить разрешение для несуществующего оригинала
def upgrade() -> None:
    """Применить изменение базы"""
    op.create_table(
        "transfers",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
        ),
        sa.PrimaryKeyConstraint("digest"),
    )


# Удаляет таблицу временных разрешений на скачивание при возврате к предыдущей схеме
# Сами записи файлов и их содержимое это изменение не удаляет
def downgrade() -> None:
    """Вернуть прежнюю схему базы"""
    op.drop_table("transfers")
