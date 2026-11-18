# Добавляет указатели для поиска файлов, просроченных разрешений и недавних чатов. Сами записи
# сохраняют прежний состав и значения.

"""Ускоряет поиск связей с файлами, очистку ссылок и сортировку недавних чатов."""

import sqlalchemy as sa
from alembic import op

revision = "b7d19c2f63a4"
down_revision = "d13e9a7f02b4"


# Добавляет указатели на связи файлов и сроки разрешений, а для недавних чатов учитывает
# закрепление и время активности. Без этих указателей базе приходится читать больше строк при
# выдаче и очистке.
def upgrade():
    op.create_index("ix_chat_sources_asset_id", "chat_sources", ["asset_id"])
    op.create_index("ix_transfers_asset_id", "transfers", ["asset_id"])
    op.create_index("ix_transfers_expires_at", "transfers", ["expires_at"])
    op.create_index("ix_upload_grants_asset_id", "upload_grants", ["asset_id"])
    recent = sa.text("deleted_at IS NULL AND archived_at IS NULL")
    op.create_index(
        "ix_threads_recent_order",
        "threads",
        [
            "user_id",
            sa.text("(pinned_at IS NOT NULL) DESC"),
            sa.text("activity_at DESC"),
            sa.text("id DESC"),
        ],
        postgresql_where=recent,
        sqlite_where=recent,
    )


# Убирает добавленные указатели в обратном порядке. Сами чаты, файлы и разрешения остаются, но
# прежний поиск снова может читать больше строк.
def downgrade():
    op.drop_index("ix_threads_recent_order", table_name="threads")
    op.drop_index("ix_upload_grants_asset_id", table_name="upload_grants")
    op.drop_index("ix_transfers_expires_at", table_name="transfers")
    op.drop_index("ix_transfers_asset_id", table_name="transfers")
    op.drop_index("ix_chat_sources_asset_id", table_name="chat_sources")
