# Проверяет цену ошибок при запуске вычислений на расстоянии. Подставная машина позволяет
# проверить срок, запись номера и обязательную остановку без платного вызова.

"""Проверяет ограничение аренды и остановку на пробной службе без платы"""

import json
from types import SimpleNamespace

import pytest

from app.Services.Gpu.Runner.lifecycle import sandbox_options, start_bounded


# Сверяет параметры обычного поколения и увеличения видео и отклоняет неправильные сроки. Это
# обнаруживает изменения предела или требований к машине до реальной аренды.
def test_lifetime_and_gpu_budget():
    options = sandbox_options(3600)
    assert options["timeout"] == 3600
    assert options["gpu"] == "L4"
    assert sandbox_options(600, video_sr=True)["gpu"] == "A100-80GB"
    assert options["encrypted_ports"] == [8099]
    assert options["cpu"] == 4
    assert options["memory"] == 65536
    for invalid in (0, -1, 3601, 3600.5, True):
        with pytest.raises(ValueError):
            sandbox_options(invalid)


# Создаёт подставную удалённую службу и список вызовов с настраиваемым отказом. Тест может увидеть
# полный порядок запуска и остановки без соединения с поставщиком.
def boundary(*, tunnel_fails=False, termination_confirms=True):
    calls = []

    class Sandbox:
        object_id = "sb-test"
        ended = False

        # Возвращает отсутствие кода завершения до остановки и ноль после неё. Так запуск отличает
        # ещё занятую машину от свободной.
        def poll(self):
            return None if not self.ended else 0

        # Запоминает попытку остановки и меняет состояние только при настроенном подтверждении.
        # Проверка может отличить посланную команду от реально подтверждённого завершения.
        def terminate(self):
            self.ended = termination_confirms
            calls.append("terminate")

        # Запоминает ожидание завершения. Остановка без ожидания была бы недостаточной при ошибке
        # создания соединения.
        def wait(self, **kwargs):
            calls.append("wait")

        # Возвращает адрес проверочной машины либо воспроизводит отказ после запуска. Это отделяет
        # создание машины от возможности до неё обратиться.
        def tunnels(self):
            if tunnel_fails:
                raise RuntimeError("Tunnel failed")
            return {8099: SimpleNamespace(url="https://test.modal.run")}

    sandbox = Sandbox()

    # Сохраняет переданные параметры и выдаёт подставную машину. По списку вызовов видно, какой
    # образ и срок передали и был ли запуск вообще.
    def create(*args, **kwargs):
        calls.append(kwargs)
        return sandbox

    sdk = SimpleNamespace(
        Sandbox=SimpleNamespace(create=create, from_id=lambda _: sandbox),
        Secret=SimpleNamespace(from_dict=lambda d: d),
    )
    return sdk, sandbox, calls


# Запускает подставную аренду и сверяет закрытую запись номера и адреса без секретного ключа. Этот
# файл нужен для остановки и не должен стать копией настроек доступа.
def test_create_records_identity_without_token(tmp_path):
    sdk, sandbox, calls = boundary()
    path = tmp_path / "state.json"
    record = start_bounded(sdk, "app", "prebuilt-image", "secret" * 8, path)
    assert calls[0]["timeout"] == 3600
    assert calls[0]["image"] == "prebuilt-image"
    assert record["sandbox_id"] == sandbox.object_id
    assert "secret" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["url"] == "https://test.modal.run"


# Сохраняет номер ещё работающей машины и просит запустить другую. Правильный отказ оставляет
# список запусков пустым.
def test_live_previous_sandbox_prevents_allocation(tmp_path):
    sdk, sandbox, calls = boundary()
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sandbox_id": sandbox.object_id}))
    with pytest.raises(RuntimeError, match="already running"):
        start_bounded(sdk, "app", "image", "x" * 48, path)
    assert calls == []


# Ломает получение адреса после создания машины. Проверяет остановку и ожидание, иначе вычисления
# могли бы остаться включёнными после отказа.
def test_tunnel_failure_terminates_allocated_gpu(tmp_path):
    sdk, sandbox, calls = boundary(tunnel_fails=True)
    with pytest.raises(RuntimeError, match="Tunnel failed"):
        start_bounded(sdk, "app", "image", "x" * 48, tmp_path / "state.json")
    assert sandbox.ended
    assert calls[-2:] == ["terminate", "wait"]


# Передаёт короткий ключ и сверяет, что запуска и записи номера не было. Непригодные настройки
# должны остановить работу до выделения машины.
def test_short_service_token_is_rejected_before_creating_a_sandbox(tmp_path):
    sdk, _, calls = boundary()
    path = tmp_path / "state.json"
    with pytest.raises(ValueError, match="at least 32"):
        start_bounded(sdk, "app", "image", "short", path)
    assert calls == []
    assert not path.exists()


# Указывает номер уже завершённого запуска и запрашивает новый на минуту. Сверяет один вызов
# создания и новый срок в записи.
def test_completed_sandbox_can_be_replaced(tmp_path):
    sdk, sandbox, calls = boundary()
    sandbox.ended = True
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sandbox_id": sandbox.object_id}))
    start_bounded(sdk, "app", "image", "x" * 48, path, seconds=60)
    assert len(calls) == 1
    assert calls[0]["timeout"] == 60
    assert json.loads(path.read_text())["timeout"] == 60


# Имитирует сбой адреса и отсутствие подтверждения остановки. Номер должен сохраниться, чтобы
# машину можно было найти позже, а настоящая причина отказа осталась видимой.
def test_unconfirmed_termination_preserves_the_identity_for_recovery(tmp_path):
    sdk, sandbox, calls = boundary(tunnel_fails=True, termination_confirms=False)
    path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="termination is not confirmed") as error:
        start_bounded(sdk, "app", "image", "x" * 48, path)
    assert str(error.value.__cause__) == "Tunnel failed"
    assert calls[-2:] == ["terminate", "wait"]
    assert json.loads(path.read_text())["sandbox_id"] == sandbox.object_id
