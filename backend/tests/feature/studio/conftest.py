# Готовит отдельную копию сервера для проверок. База и файлы живут во временной папке, поэтому
# результаты соседних проверок не влияют друг на друга.

import pytest
from fastapi.testclient import TestClient

from tests.support.fixtures import create_studio as create_app


# Открывает пробное приложение с временной базой, своим хранилищем и пробным генератором. После
# проверки закрывает приложение, чтобы его фоновые службы не остались работать.
@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path}/db", media_dir=tmp_path / "media", provider="demo"
        )
    ) as client:
        yield client
