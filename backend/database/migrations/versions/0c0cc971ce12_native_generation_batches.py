# Добавляет порядок результатов внутри одной отправки. Прежний номер задания остаётся действующим,
# поэтому старые чаты можно открывать после обновления.

"""Принадлежность результатов к общей отправке с сохранением прежних номеров заданий"""

import sqlalchemy as sa
from alembic import op

revision = "0c0cc971ce12"
down_revision = "c630dac0df8e"
branch_labels = None
depends_on = None


# Добавляет заданию номер общей отправки и положение результата внутри неё
# Уникальный индекс не допускает два результата на одном месте одной группы
# Прежние задания сохраняют свои номера и остаются одиночными с пустыми новыми полями
def upgrade():
    op.add_column("jobs", sa.Column("batch_id", sa.String(36)))
    op.add_column("jobs", sa.Column("batch_index", sa.Integer()))
    op.create_index("ix_jobs_batch_position", "jobs", ["batch_id", "batch_index"], unique=True)


# Убирает индекс и поля принадлежности задания к общей отправке
# Сами задания остаются в таблице, но сведения об их группировке больше не сохраняются
def downgrade():
    op.drop_index("ix_jobs_batch_position", table_name="jobs")
    op.drop_column("jobs", "batch_index")
    op.drop_column("jobs", "batch_id")
