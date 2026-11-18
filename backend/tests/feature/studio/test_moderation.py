# Проверяет жалобы владельца, решения проверяющего и последствия отзыва файла. На пробной базе
# сверяет доступ к истории, запрет генерации и очистку уже начатой работы.

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.Models import Asset, GenerationReport, Job
from app.Services.Generation.process_generation import process_one
from app.Services.Gpu.provider import DemoProvider
from app.Services.Moderation.reports import resolve_report
from tests.support.fixtures import account, job


# Принимает запрос, запускает пробного обработчика и возвращает готовое задание. Проверки получают
# файл, созданный обычным порядком.
def completed(client, owner):
    response = job(client, owner)
    assert response.status_code == 201
    job_id = response.json()["id"]
    state = client.app.state
    assert asyncio.run(process_one(state.factory, state.settings))
    result = client.get(f"/v1/jobs/{job_id}", headers=owner).json()
    assert result["status"] == "succeeded"
    return result


# Проверяет отказ без входа, чужому аккаунту и при неизвестной причине. Две одновременные жалобы
# дают одинаковый ответ и одну запись, которая удаляется вместе с чатом.
def test_report_is_private_and_concurrent_replays_share_one_record(client):
    owner = account(client)
    foreign = account(client, "elsewhere@example.com")
    result = completed(client, owner)
    path = f"/v1/jobs/{result['id']}/report"
    assert client.post(path, json={"reason": "other"}).status_code == 401
    assert client.post(path, headers=foreign, json={"reason": "other"}).status_code == 404
    assert client.post(path, headers=owner, json={"reason": "arbitrary"}).status_code == 422

    # Отправляет ту же жалобу владельца и возвращает ответ сервера. Два потока выполняют именно
    # этот вызов, чтобы совпали все поля запроса.
    def report():
        return client.post(path, headers=owner, json={"reason": "inappropriate"})

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(lambda _: report(), range(2)))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["job_id"] == result["id"]
    assert first.json()["reason"] == "inappropriate"
    with client.app.state.factory() as db:
        saved = db.scalars(select(GenerationReport)).one()
        assert saved.job_id == result["id"]
        assert saved.decision is None
    assert client.delete(f"/v1/threads/{result['thread_id']}", headers=owner).status_code == 204
    with client.app.state.factory() as db:
        assert db.scalars(select(GenerationReport)).first() is None


# Пытается пожаловаться на задание в очереди и ждёт конфликта. Иначе появилась бы жалоба на файл,
# которого ещё нет.
def test_rejects_report_without_generated_result(client):
    owner = account(client)
    queued = job(client, owner).json()
    response = client.post(
        f"/v1/jobs/{queued['id']}/report",
        headers=owner,
        json={"reason": "other"},
    )
    assert response.status_code == 409


# Удаляет файл по жалобе, имея действующее разрешение на скачивание и задание с этим исходником.
# Сверяет отзыв разрешения и остановку зависимого задания. Самостоятельная работа остаётся в
# очереди.
def test_removing_reported_result_revokes_transfer_and_keeps_other_content(client):
    owner = account(client)
    result = completed(client, owner)
    route = f"/v1/assets/{result['output_id']}"
    ticket = client.post(route + "/ticket", headers=owner).json()["path"]
    assert client.get(ticket).status_code == 200
    factory = client.app.state.factory
    settings = client.app.state.settings
    with factory() as db:
        path = settings.media_dir / db.get(Asset, result["output_id"]).filename
    assert path.is_file()
    dependency = job(
        client,
        owner,
        key="pending-source-00001",
        model="video",
        input_id=result["output_id"],
    ).json()
    assert dependency["status"] == "queued"
    other = job(client, owner, key="unrelated-job-00001").json()
    report = f"/v1/jobs/{result['id']}/report"
    assert client.post(report, headers=owner, json={"reason": "other"}).status_code == 200

    with factory() as db:
        resolved = resolve_report(db, settings.media_dir, result["id"], "remove", "test-operator")
    assert resolved.decision == "remove"
    assert not path.exists()
    assert client.get(route, headers=owner).status_code == 404
    assert client.get(route + "/metadata", headers=owner).status_code == 404
    assert client.get(ticket).status_code == 401
    current = client.get(f"/v1/jobs/{result['id']}", headers=owner).json()
    assert current["status"] == "failed"
    assert current["moderated"] and current["output_id"] is None
    blocked = client.get(f"/v1/jobs/{dependency['id']}", headers=owner).json()
    assert blocked["status"] == "failed" and blocked["moderated"] and blocked["input_id"] is None
    assert client.get(f"/v1/jobs/{other['id']}", headers=owner).json()["status"] == "queued"
    assert (
        client.post(report, headers=owner, json={"reason": "other"}).json()["decision"] == "remove"
    )


# Проверяет повтор согласного решения и отказ при противоположном. Имя первого проверяющего и
# время сохраняются, исходный файл остаётся доступен.
def test_operator_can_keep_result_and_cannot_overwrite_decision(client):
    owner = account(client)
    result = completed(client, owner)
    report = f"/v1/jobs/{result['id']}/report"
    assert client.post(report, headers=owner, json={"reason": "other"}).status_code == 200
    state = client.app.state
    with state.factory() as db:
        kept = resolve_report(db, state.settings.media_dir, result["id"], "keep", "reviewer-a")
        repeated = resolve_report(db, state.settings.media_dir, result["id"], "keep", "reviewer-b")
        assert kept.reviewed_at == repeated.reviewed_at
        assert repeated.reviewer == "reviewer-a"
        with pytest.raises(ValueError, match="другое решение"):
            resolve_report(db, state.settings.media_dir, result["id"], "remove", "reviewer-b")
    assert client.get(f"/v1/assets/{result['output_id']}", headers=owner).status_code == 200


# Сначала пытается удалить результат без жалобы, затем убирает ссылку на файл у задания с жалобой.
# Оба действия отклоняются, а жалоба не помечается решённой.
def test_operator_cannot_remove_an_unreported_job_or_publish_a_decision_without_its_file(client):
    owner = account(client)
    result = completed(client, owner)
    job_id = result["id"]
    state = client.app.state
    with state.factory() as db:
        with pytest.raises(ValueError, match="Жалоба не найдена"):
            resolve_report(db, state.settings.media_dir, job_id, "remove", "operator")
    assert client.get(f"/v1/assets/{result['output_id']}", headers=owner).status_code == 200
    assert (
        client.post(
            f"/v1/jobs/{job_id}/report", headers=owner, json={"reason": "other"}
        ).status_code
        == 200
    )
    with state.factory() as db:
        db.get(Job, job_id).output_id = None
        db.commit()
        with pytest.raises(ValueError, match="Готовый файл жалобы не найден"):
            resolve_report(db, state.settings.media_dir, job_id, "remove", "operator")
    with state.factory() as db:
        assert db.get(GenerationReport, job_id).decision is None
        assert db.get(Asset, result["output_id"]) is not None


# Создаёт жалобы на два задания с общим файлом. После удаления по первой жалобе второй проверяющий
# всё ещё может записать своё решение. Иначе вторая жалоба осталась бы в очереди без файла.
def test_two_reports_can_be_resolved_when_the_first_revokes_the_shared_result(client):
    owner = account(client)
    first = completed(client, owner)
    second_id = job(client, owner, key="shared-result-test").json()["id"]
    state = client.app.state
    with state.factory() as db:
        second = db.get(Job, second_id)
        second.status = "succeeded"
        second.output_id = first["output_id"]
        db.commit()
    for job_id in (first["id"], second_id):
        assert (
            client.post(
                f"/v1/jobs/{job_id}/report", headers=owner, json={"reason": "other"}
            ).status_code
            == 200
        )
    with state.factory() as db:
        resolve_report(db, state.settings.media_dir, first["id"], "remove", "reviewer-a")
    with state.factory() as db:
        second = db.get(Job, second_id)
        assert second.moderated and second.output_id is None
        reviewed = resolve_report(db, state.settings.media_dir, second_id, "remove", "reviewer-b")
        assert reviewed.decision == "remove"
        assert reviewed.reviewer == "reviewer-b"


# Удаляет результат и приостанавливает работу владельца. Проверяет доступ к старой истории,
# остановку его очереди и отказ для нового запроса. Другой аккаунт по-прежнему может создавать
# задания.
def test_suspend_generation_preserves_access_to_existing_history(client):
    owner = account(client)
    foreign = account(client, "active@example.com")
    result = completed(client, owner)
    queued = job(client, owner, key="before-suspension-01").json()
    route = f"/v1/jobs/{result['id']}/report"
    assert client.post(route, headers=owner, json={"reason": "harmful"}).status_code == 200
    state = client.app.state
    with state.factory() as db:
        resolve_report(db, state.settings.media_dir, result["id"], "remove-and-suspend", "operator")
    assert client.get(f"/v1/jobs/{result['id']}", headers=owner).status_code == 200
    assert client.get(f"/v1/jobs/{queued['id']}", headers=owner).json()["moderated"]
    assert job(client, owner, key="after-suspension-01").status_code == 403
    assert job(client, foreign, key="after-suspension-01").status_code == 201


# Останавливает обработчик после чтения исходника, отзывает этот файл и разрешает вычислению
# завершиться. Результат не должен появиться в базе или папке.
def test_revoked_input_fences_a_running_worker(client):
    owner = account(client)
    result = completed(client, owner)
    queued = job(
        client,
        owner,
        key="working-source-0001",
        model="video",
        input_id=result["output_id"],
    ).json()
    route = f"/v1/jobs/{result['id']}/report"
    assert client.post(route, headers=owner, json={"reason": "other"}).status_code == 200
    state = client.app.state

    # Ждёт, пока обработчик возьмёт файл, записывает решение проверяющего и отпускает работу. В
    # случае сбоя завершает пробную фоновую задачу, чтобы она не пережила проверку.
    async def race():
        started, release = asyncio.Event(), asyncio.Event()

        class Paused(DemoProvider):
            # Убеждается, что обработчик уже получил нужный исходник, и ждёт разрешения. После
            # возобновления пишет пробные выходные байты для проверки запрета поздней публикации.
            async def generate(self, job, output, reference):
                assert job.id == queued["id"] and reference.is_file()
                started.set()
                await release.wait()
                # Подготавливаем содержимое, которое исполнитель мог получить ещё до решения проверяющего.
                output.write_bytes(b"unpublished result")
                return "video/mp4"

        running = asyncio.create_task(process_one(state.factory, state.settings, Paused()))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            with state.factory() as db:
                resolve_report(db, state.settings.media_dir, result["id"], "remove", "operator")
            release.set()
            assert await asyncio.wait_for(running, timeout=10)
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(race())
    current = client.get(f"/v1/jobs/{queued['id']}", headers=owner).json()
    assert current["moderated"] and current["status"] == "failed"
    assert current["output_id"] is None
    assert not list(state.settings.media_dir.iterdir())


# На отдельной базе проверяет команды списка, просмотра и решения жалобы. Сверяет удаление файла,
# приостановку и возврат права генерации, а также понятные ошибки неверных команд.
def test_operator_cli_lists_reviews_and_restores_generation_on_the_isolated_database(
    client, monkeypatch, capsys
):
    import json
    import runpy
    import sys
    from pathlib import Path
    from uuid import UUID

    from app.Console.Commands import moderation as reports

    owner = account(client)
    result = completed(client, owner)
    job_id = result["id"]
    assert (
        client.post(
            f"/v1/jobs/{job_id}/report", headers=owner, json={"reason": "harmful"}
        ).status_code
        == 200
    )
    settings = client.app.state.settings
    assert settings.database_url.startswith("sqlite:///")
    monkeypatch.setenv("STUDIO_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STUDIO_MEDIA_DIR", str(settings.media_dir))

    # Настраивает аргументы, запускает команду проверяющего и разбирает её ответ. Использует ту же
    # пробную базу, что и запросы приложения.
    def execute(*args):
        monkeypatch.setattr(sys, "argv", ["reports", *args])
        runpy.run_path(reports.__file__, run_name="__main__")
        return json.loads(capsys.readouterr().out)

    # Ждёт остановки команды с кодом ошибки и проверяет причину отказа. Это отличает ожидаемый
    # запрет от поломки команды.
    def denied(*args, reason):
        with pytest.raises(SystemExit) as stop:
            execute(*args)
        assert stop.value.code == 2
        assert reason in capsys.readouterr().err

    unknown_id = str(UUID(int=0))
    denied("list", "--limit", "101", reason="от 1 до 100")
    denied("show", unknown_id, reason="Жалоба не найдена")
    denied("resume", unknown_id, reason="Аккаунт не найден")
    denied("resolve", job_id, "keep", "--reviewer", "  ", reason="имя оператора")
    listed = execute("list", "--limit", "1")
    assert [(item["job_id"], item["decision"]) for item in listed] == [(job_id, None)]

    initial = execute("show", job_id)
    original = Path(initial["media_path"])
    assert original.is_relative_to(settings.media_dir) and original.is_file()
    assert initial["model"] == "image"
    assert initial["reviewer"] is None
    reviewed = execute("resolve", job_id, "remove-and-suspend", "--reviewer", " test operator ")
    assert reviewed["decision"] == "remove-and-suspend"
    assert not original.exists()
    assert execute("list") == []
    resolved = execute("show", job_id)
    assert resolved["reviewer"] == "test operator"
    assert resolved["media_path"] is None
    assert job(client, owner, key="blocked-generation-cli").status_code == 403

    assert execute("resume", initial["owner_id"]) == {
        "owner_id": initial["owner_id"],
        "generation_suspended": False,
    }
    assert job(client, owner, key="restored-generation-cli").status_code == 201
