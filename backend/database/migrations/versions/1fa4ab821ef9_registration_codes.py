# Отводит отдельное место для проверочных кодов при регистрации. Хранить код в строке аккаунта
# нельзя, потому что до подтверждения аккаунт ещё не создан.

"""Коды для новых регистраций хранятся отдельно от существующих аккаунтов."""

import sqlalchemy as sa
from alembic import op

revision = "1fa4ab821ef9"
down_revision = "63e7817bd409"


# Создаёт таблицу проверочных кодов и указатель времени запроса. Сервер может искать просроченные
# записи независимо от уже созданных аккаунтов.
def upgrade():
    op.create_table(
        "registration_codes",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_registration_codes_requested_at", "registration_codes", ["requested_at"])


# Удаляет таблицу ожидающих кодов вместе с её указателем. При возврате к прежней схеме такие коды
# уже нельзя подтвердить.
def downgrade():
    op.drop_table("registration_codes")
