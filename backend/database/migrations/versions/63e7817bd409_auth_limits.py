# Хранит время последнего действия в сеансе и число попыток входа. Эти данные позволяют
# ограничивать вход сразу для нескольких экземпляров сервера.

"""Пределы входа и срок бездействия сеанса."""

import time

import sqlalchemy as sa
from alembic import op

revision = "63e7817bd409"
down_revision = "1b529c7626cf"


# Добавляет отметку активности к сеансам, заполняет её для прежних входов и создаёт общую таблицу
# попыток. Иначе прежние сеансы не имели бы точки отсчёта, а несколько серверов считали бы попытки
# по отдельности.
def upgrade():
    # Для старых сеансов время последнего действия неизвестно. Отсчёт начинаем при обновлении.
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("last_activity_at", sa.Float(), nullable=True))
    op.execute(
        sa.table("sessions", sa.column("last_activity_at"))
        .update()
        .values(last_activity_at=time.time())
    )
    with op.batch_alter_table("sessions") as batch:
        # Во время обновления прежние экземпляры приложения ещё могут выдавать сеансы.
        clock = (
            "EXTRACT(EPOCH FROM clock_timestamp())"
            if op.get_bind().dialect.name == "postgresql"
            else "(strftime('%s','now'))"
        )
        batch.alter_column(
            "last_activity_at",
            existing_type=sa.Float(),
            nullable=False,
            server_default=sa.text(clock),
        )
    op.create_table(
        "auth_attempts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_auth_attempts_expires_at", "auth_attempts", ["expires_at"])


# Убирает записи попыток и поле активности при возврате к прежней схеме. Вместе с ними пропадают
# сохранённый счётчик и возможность считать время бездействия.
def downgrade():
    op.drop_table("auth_attempts")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("last_activity_at")
