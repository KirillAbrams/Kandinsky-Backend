# Готовит доставку удаления аккаунта службе связи и проверку отозванных сеансов. Для этого
# достаточно номеров и отпечатков, содержимое аккаунта не требуется.

"""Доставка удаления аккаунта и отзыва сессий без сохранения его содержимого"""

import sqlalchemy as sa
from alembic import op

revision = "6f2b51e6742a"
down_revision = "c630dac0df8e"
branch_labels = None
depends_on = None


# Создаёт отдельную таблицу номеров удалённых владельцев для доставки очистки переходному серверу
# Сохраняет отпечатки отозванных сессий со сроком действия и указателем срока, не удерживая содержимое
# удалённого аккаунта
def upgrade():
    op.create_table(
        "account_erasures",
        sa.Column("owner_id", sa.String(36), primary_key=True),
    )
    op.create_table(
        "erased_sessions",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_erased_sessions_expires_at", "erased_sessions", ["expires_at"])


# Удаляет указатель срока отозванных сессий и обе таблицы доставки удаления
# После возврата к прежней схеме эти отдельные отметки больше не хранятся
def downgrade():
    op.drop_index("ix_erased_sessions_expires_at", table_name="erased_sessions")
    op.drop_table("erased_sessions")
    op.drop_table("account_erasures")
