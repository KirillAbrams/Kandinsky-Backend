# Задаёт основу моделей базы и общий способ выдавать номера. Единая основа нужна, чтобы таблицы
# вошли в общее описание схемы.

import uuid

from sqlalchemy.orm import DeclarativeBase


# Создаёт случайный строковый номер для новой строки. Такой номер можно использовать сразу, не
# читая последний номер из общей таблицы.
def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass
