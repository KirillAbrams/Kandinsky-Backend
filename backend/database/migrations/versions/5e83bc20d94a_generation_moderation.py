# Добавляет хранение жалоб и доставки сведений о скрытых результатах. Скрытие на основном сервере
# должно дойти и до телефона, который уже видел результат.

"""Жалобы на генерацию и надёжная доставка отзыва результатов"""

import sqlalchemy as sa
from alembic import op

revision = "5e83bc20d94a"
down_revision = "6f2b51e6742a"
branch_labels = None
depends_on = None


# Добавляет приостановку генерации у пользователя и признак запрета результата у задания
# Создаёт жалобы с ограниченными причинами и решениями, требуя согласованно заполнять сведения о рассмотрении
# Добавляет указатель очереди жалоб и отдельные отметки отзыва результатов для доставки очистки владельцу
def upgrade():
    op.add_column("users", sa.Column("generation_suspended_at", sa.Float()))
    op.add_column(
        "jobs", sa.Column("moderated", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "generation_reports",
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(24)),
        sa.Column("reviewed_at", sa.Float()),
        sa.Column("reviewer", sa.String(80)),
        sa.CheckConstraint("reason IN ('inappropriate', 'harmful', 'other')"),
        sa.CheckConstraint("decision IN ('keep', 'remove', 'remove-and-suspend')"),
        sa.CheckConstraint(
            "(decision IS NULL AND reviewed_at IS NULL AND reviewer IS NULL) OR "
            "(decision IS NOT NULL AND reviewed_at IS NOT NULL AND reviewer IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_generation_reports_queue", "generation_reports", ["decision", "created_at", "job_id"]
    )
    op.create_table(
        "result_removals",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("asset_id", sa.String(36)),
    )
    op.create_index("ix_result_removals_owner_id", "result_removals", ["owner_id"])


# Удаляет таблицы доставки отзыва и жалоб вместе с принадлежащими им указателями
# Затем убирает признаки запрета результата и приостановки генерации, возвращая прежний состав основных таблиц
def downgrade():
    op.drop_table("result_removals")
    op.drop_table("generation_reports")
    op.drop_column("jobs", "moderated")
    op.drop_column("users", "generation_suspended_at")
