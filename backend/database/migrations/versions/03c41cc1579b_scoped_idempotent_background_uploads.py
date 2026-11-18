# Вводит временное разрешение на отправку файла в фоне. Сохранённая контрольная сумма помогает
# безопасно продолжить загрузку после потери ответа.

"""Временные разрешения на повторяемые фоновые загрузки

Номер изменения 03c41cc1579b
Предыдущее изменение 6b28711ecf66
Дата создания 2026-11-10 19:57:59.284388

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Номера задают порядок применения изменений базы
revision: str = "03c41cc1579b"
down_revision: Union[str, Sequence[str], None] = "6b28711ecf66"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Создаёт разрешения на загрузку с владельцем, сроком действия и необязательной ссылкой на уже принятый файл
# Отпечаток содержимого позволяет различить безопасный повтор и замену файла под тем же разрешением
# Добавляет указатель владельца для поиска и очистки его разрешений
def upgrade() -> None:
    """Применить изменение базы"""
    op.create_table(
        "upload_grants",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("digest"),
    )
    op.create_index(op.f("ix_upload_grants_user_id"), "upload_grants", ["user_id"], unique=False)


# Удаляет указатель владельца и таблицу разрешений на повторяемые загрузки
# Возвращает прежнюю схему без удаления таблицы пользователей и уже сохранённых файлов
def downgrade() -> None:
    """Вернуть прежнюю схему базы"""
    op.drop_index(op.f("ix_upload_grants_user_id"), table_name="upload_grants")
    op.drop_table("upload_grants")
