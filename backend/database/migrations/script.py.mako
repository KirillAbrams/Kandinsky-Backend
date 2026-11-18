## Шаблон создаёт заготовку изменения схемы из описания, номеров и команд.
"""${message}

Номер изменения ${up_revision}
Предыдущее изменение ${down_revision | comma,n}
Дата создания ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# Номера задают порядок применения изменений базы
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


# Выполняет шаги для перехода на эту версию схемы.
# Указывает, что база должна получить до запуска нового кода.
def upgrade() -> None:
    """Применить изменение базы"""
    ${upgrades if upgrades else "pass"}


# Выполняет обратные шаги для возврата прежней схемы.
# Для необратимого изменения здесь указывают причину отказа.
def downgrade() -> None:
    """Вернуть прежнюю схему базы"""
    ${downgrades if downgrades else "pass"}
