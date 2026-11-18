# Подключает выбор модели, отправку, чтение, отмену, повтор и удаление результата. В запросах на
# изменение проверяет владельца и связывает ответ с подтверждённой записью в базе.

"""Запросы создания, повтора и отмены генераций"""

import uuid
from typing import Annotated, Literal

from fastapi import Header

from app.Http.dependencies import CurrentUser, DatabaseSession
from app.Http.Requests.generation import BatchJobRequest, GenerationReportRequest, JobRequest
from app.Http.Resources.generation import job_response
from app.Services.Generation.acknowledge_removals import acknowledge_native_removals
from app.Services.Generation.cancel_generation import cancel_generation, cancel_generation_batch
from app.Services.Generation.model_catalog import model_catalog
from app.Services.Generation.remove_content import remove_job_content
from app.Services.Generation.results import (
    generation_batch,
    generation_result,
    pending_native_removals,
)
from app.Services.Generation.retry_generation import retry_generation
from app.Services.Generation.submit_jobs import GenerationParameters, submit_jobs
from app.Services.Moderation.report_generation import report_result
from config.studio import Settings


class GenerationsController:
    """Принимает проверенные данные и передаёт действия приложению."""

    # Собирает команды списка моделей и действий с заданиями. Все они используют общую проверку
    # владельца, чтобы один способ вызова не давал больше прав, чем другой.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # Возвращает характеристики моделей для настроенного исполнителя после проверки входа. Телефон
    # может скрыть неподдерживаемые режимы до отправки.
    def models(self, user: CurrentUser):
        return model_catalog(self._settings.provider, video_sr=self._settings.video_sr_enabled)

    # Передаёт параметры и ключ отправки службе постановки в очередь и отдаёт один результат. Один
    # и тот же ключ пригодится при потере ответа, чтобы повтор не создал второе задание.
    def submit(
        self,
        body: JobRequest,
        user: CurrentUser,
        db: DatabaseSession,
        operation: Annotated[str, Header(alias="Idempotency-Key")],
    ):
        parameters = GenerationParameters(**body.model_dump())
        (snapshot,) = submit_jobs(db, self._settings, user, parameters, operation)
        return job_response(snapshot)

    # Выделяет общие параметры и число ячеек из запроса. Передаёт их с одним ключом отправки,
    # чтобы группа сохранилась вместе и повторно не распалась на разные задания.
    def submit_batch(
        self,
        body: BatchJobRequest,
        user: CurrentUser,
        db: DatabaseSession,
        operation: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    ):
        request = JobRequest.model_validate(body.model_dump(exclude={"count"}))
        parameters = GenerationParameters(**request.model_dump())
        snapshots = submit_jobs(db, self._settings, user, parameters, str(operation), body.count)
        return [job_response(snapshot) for snapshot in snapshots]

    # Выбирает очередную порцию записей удаления вошедшего владельца по времени и номеру. Отметки
    # остаются до подтверждения, поэтому потеря ответа не мешает доставке.
    @staticmethod
    def native_removals(user: CurrentUser, db: DatabaseSession):
        return pending_native_removals(db, user)

    # Проверяет размер списка и удаляет только переданные отметки владельца. Чужие номера не могут
    # закрыть ожидание очистки в другом аккаунте.
    @staticmethod
    def ack_native_removals(user: CurrentUser, db: DatabaseSession, ids: list[uuid.UUID]):
        acknowledge_native_removals(db, user, ids)

    # Проверяет вид удаления и наличие номера результата, затем под блокировкой находит чат и все
    # нужные ячейки. Уже стёртая отправка считается обработанной, иначе сетевой повтор ложно
    # показал бы ошибку.
    def delete_job_content(
        self,
        job_id: str,
        user: CurrentUser,
        db: DatabaseSession,
        target: Literal["prompt", "result"],
        blob: uuid.UUID | None = None,
    ):
        remove_job_content(db, user, job_id, target, blob, self._settings.media_dir)

    # Ищет видимое владельцу задание, загружает его группу и формирует ответы по порядку. Это даёт
    # телефону состояние всех результатов по одному номеру.
    @staticmethod
    def get_batch(job_id: str, user: CurrentUser, db: DatabaseSession):
        return [job_response(snapshot) for snapshot in generation_batch(db, user, job_id)]

    # Снимает право обработки с ещё ожидающих или работающих участников группы. Готовые ячейки
    # остаются готовыми, поэтому отмена не уничтожает полученный результат.
    @staticmethod
    def cancel_batch(job_id: str, user: CurrentUser, db: DatabaseSession):
        return [job_response(snapshot) for snapshot in cancel_generation_batch(db, user, job_id)]

    # Передаёт номер команды и ожидаемой попытки службе повтора. Если экран устарел, проверка там
    # не позволит вернуть в очередь более свежую работу.
    @staticmethod
    def retry_failed_job(
        job_id: str,
        user: CurrentUser,
        db: DatabaseSession,
        operation: uuid.UUID,
        expected_run: uuid.UUID | None = None,
    ):
        return job_response(retry_generation(db, user, job_id, operation, expected_run))

    # Читает одно видимое задание и добавляет сведения о его обложке. Удалённый запрос или чужой
    # номер не могут вернуть файл через этот вызов.
    @staticmethod
    def get_job(job_id: str, user: CurrentUser, db: DatabaseSession):
        return job_response(generation_result(db, user, job_id))

    # Сверяет, что результат готов и принадлежит заявителю, затем сохраняет одну жалобу. При
    # повторной отправке возвращает существующую запись, чтобы не дублировать работу модератора.
    @staticmethod
    def report_generation(
        job_id: str, body: GenerationReportRequest, user: CurrentUser, db: DatabaseSession
    ):
        return report_result(db, user, job_id, body.reason)

    # Помечает отменённым только ещё активное задание и перечитывает его из базы. Свежий ответ
    # нужен, чтобы телефон не показал прежнее состояние из памяти сеанса.
    @staticmethod
    def cancel(job_id: str, user: CurrentUser, db: DatabaseSession):
        return job_response(cancel_generation(db, user, job_id))
