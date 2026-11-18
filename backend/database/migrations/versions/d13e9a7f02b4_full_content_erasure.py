# Расширяет схему для окончательного удаления генераций, исходников и производных файлов. Следы
# завершённых удалений ограничены номерами и отпечатками, чтобы старый запрос не восстановил
# содержимое.

"""Очередь стирания хранит номера без текста и содержимого файлов."""

import sqlalchemy as sa
from alembic import op

revision = "d13e9a7f02b4"
down_revision = "1fa4ab821ef9"


# Связывает исходники с чатами и обложки с родительскими файлами, затем добавляет очередь стирания
# с видеокарты. Отдельные отметки завершённых удалений не дают повтору вернуть старый запрос.
def upgrade():
    op.add_column(
        "threads",
        sa.Column("erasure_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("upload_grants") as batch:
        batch.add_column(sa.Column("thread_id", sa.String(36)))
        batch.create_foreign_key("fk_upload_grants_thread", "threads", ["thread_id"], ["id"])
    op.create_index("ix_upload_grants_thread_id", "upload_grants", ["thread_id"])
    for name in ("asset_only", "retire_answer"):
        op.add_column(
            "job_content_removals",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.add_column(
        "job_content_removals",
        sa.Column("revoke_asset", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "chat_sources",
        sa.Column(
            "thread_id",
            sa.String(36),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "asset_id",
            sa.String(36),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "asset_derivatives",
        sa.Column(
            "child_id",
            sa.String(36),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "parent_id",
            sa.String(36),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_asset_derivatives_parent_id", "asset_derivatives", ["parent_id"])
    op.create_table(
        "erased_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("operation_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "operation_digest"),
    )
    op.create_index("ix_erased_generations_user_id", "erased_generations", ["user_id"])
    op.create_table(
        "pending_gpu_removals",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36)),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


# Отклоняет откат этой схемы. Удаление отметок сняло бы запрет на повтор старой генерации и
# нарушило бы обещание окончательной очистки.
def downgrade():
    # Откат не должен снимать запрет на повтор уже удалённых запросов.
    raise RuntimeError(
        "Стирание содержимого нельзя отменить откатом схемы; верните совместимый образ сервера"
    )
