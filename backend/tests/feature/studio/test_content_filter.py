# Проверяет, что материалы проходят обязательную проверку до генерации и перед выдачей. Пробные
# ответы службы позволяют проверить запреты, сбои и остановку работы по времени.

import asyncio
import base64
import io
import json
import subprocess
import sys
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from app.Exceptions.moderation import FilterUnavailable
from app.Models import Asset
from app.Services.Generation.process_generation import process_one
from app.Services.Moderation import content_filter
from tests.support.fixtures import account, job
from tests.support.fixtures import create_studio as create_app


# Открывает пробный сервер с обязательной проверкой и отдельной папкой файлов. У обработчика те же
# правила допуска, что при включённой защите.
@pytest.fixture
def guarded(tmp_path):
    with TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path}/studio.db",
            provider="demo",
            media_dir=tmp_path / "media",
            moderation_mode="required",
            moderation_origin="https://filter.test",
            moderation_token="local-fixture-token",
        )
    ) as app:
        yield app


# Подключает управляемый ответ только для проверочного адреса службы. Остальные сетевые клиенты
# создаются обычным порядком.
def use_filter(monkeypatch, handler):
    original = httpx.AsyncClient

    # При создании клиента проверяет его адрес и при совпадении направляет вызовы к пробному
    # ответу. Это даёт возможность подменить только нужную службу.
    def client(*args, **kwargs):
        if kwargs.get("base_url") == "https://filter.test":
            kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


# Выполняет один проход обработчика и читает состояние указанного задания от имени владельца.
# Проверка видит результат через обычный сетевой ответ.
def run(app, headers, job_id, provider):
    state = app.app.state
    assert asyncio.run(process_one(state.factory, state.settings, provider))
    return app.get(f"/v1/jobs/{job_id}", headers=headers).json()


class TestGenerator:
    __test__ = False

    # Запоминает пробный ролик и начинает считать вызовы с нуля. По числу вызовов видно,
    # остановила ли проверка запрос до вычисления.
    def __init__(self, video=None):
        self.calls = 0
        self.video = video

    # Увеличивает счётчик и копирует заданный ролик или пишет маленькое изображение. Возвращает
    # тип файла, чтобы следующий шаг действительно проверил его содержимое.
    async def generate(self, job, path, reference):
        self.calls += 1
        if self.video:
            path.write_bytes(self.video.read_bytes())
            return "video/mp4"
        Image.new("RGB", (80, 80), "white").save(path)
        return "image/png"


# Передаёт службе основной и исключающий тексты. Когда основной запрещён, обработчик даже не
# вызывает генератор и не выдаёт файл.
def test_denied_request_never_reaches_generator(guarded, monkeypatch):
    auth = account(guarded)
    item = job(guarded, auth, prompt="Synthetic policy case", negative_prompt="Watermark").json()
    requests = []

    # Сверяет ключ и путь запроса, запоминает оба текста и возвращает запрет для первого. Так
    # проверяется передача содержимого и остановка до генерации.
    def deny(request):
        assert request.headers["authorization"] == "Bearer local-fixture-token"
        assert request.url.path == "/v1/text"
        requests.append(json.loads(request.content)["items"])
        return httpx.Response(200, json={"allowed": [False, True]})

    use_filter(monkeypatch, deny)
    model = TestGenerator()
    result = run(guarded, auth, item["id"], model)
    assert model.calls == 0
    assert requests == [["Synthetic policy case", "Watermark"]]
    assert result["status"] == "failed" and result["moderated"]
    assert result["output_id"] is None


# Подставляет недоступность, пустой список, неверный вид ответа и слишком большой ответ. Ни один
# из них не разрешает вычисление и не записывается как решение о запрете содержимого.
@pytest.mark.parametrize("response", ["unavailable", "missing", "strings", "oversized"])
def test_bad_verdict_stops_without_calling_generator(guarded, monkeypatch, response):
    auth = account(guarded)
    item = job(guarded, auth).json()
    replies = {
        "unavailable": httpx.Response(503),
        "missing": httpx.Response(200, json={"allowed": []}),
        "strings": httpx.Response(200, json={"allowed": ["true"]}),
        "oversized": httpx.Response(200, content=b'{"allowed":[true]}' + b" " * 4096),
    }
    use_filter(monkeypatch, lambda _: replies[response])
    model = TestGenerator()
    result = run(guarded, auth, item["id"], model)
    assert model.calls == 0
    assert result["status"] == "failed" and not result["moderated"]
    assert "проверка безопасности" in result["error"].lower()


# Вызывает превышение времени на тексте или на готовом изображении. Сверяет число обращений к
# генератору, сообщение об ошибке и отсутствие файла и строки вложения.
@pytest.mark.parametrize("timeout_at", ["/v1/text", "/v1/images"])
def test_classifier_timeout_leaves_no_result_on_disk_or_in_database(
    guarded, monkeypatch, timeout_at
):
    auth = account(guarded)
    item = job(guarded, auth).json()
    requests = []

    # Записывает путь, прерывает выбранный запрос по времени и разрешает остальные. Так можно
    # остановить обработку до генерации или после неё.
    def moderate(request):
        requests.append(request.url.path)
        if request.url.path == timeout_at:
            raise TimeoutError("internal fixture timeout")
        return httpx.Response(200, json={"allowed": [True]})

    use_filter(monkeypatch, moderate)
    model = TestGenerator()
    result = run(guarded, auth, item["id"], model)
    expected = ["/v1/text"] if timeout_at == "/v1/text" else ["/v1/text", "/v1/images"]
    assert requests == expected
    assert model.calls == len(expected) - 1
    assert result["status"] == "failed" and not result["moderated"]
    assert "проверка безопасности" in result["error"].lower()
    assert result["output_id"] is None
    with guarded.app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == 0
    assert not list(guarded.app.state.settings.media_dir.iterdir())


# Собирает ролик со спокойным началом и запрещённым последним кадром. Проверяет, что оба конца
# дошли до анализа, а после отказа ролик не появился в базе или на диске.
def test_late_frame_cannot_be_published(guarded, monkeypatch, tmp_path):
    auth = account(guarded)
    clip = tmp_path / "frames.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=white:s=96x96:r=24:d=0.25",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=96x96:r=24:d=0.05",
            "-filter_complex",
            "concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            str(clip),
        ],
        check=True,
        timeout=20,
    )
    item = job(guarded, auth, model="video").json()
    seen = []

    # Разрешает тексты и отмечает красный кадр запрещённым после чтения цвета. Запоминает порядок
    # ответов, чтобы проверка увидела анализ конца ролика.
    def check(request):
        items = json.loads(request.content)["items"]
        if request.url.path == "/v1/text":
            return httpx.Response(200, json={"allowed": [True] * len(items)})
        assert request.url.path == "/v1/images"
        allowed = []
        for payload in items:
            with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
                red, green, blue = image.getpixel((10, 10))
            safe = not (red > 200 and green < 70 and blue < 70)
            seen.append(safe)
            allowed.append(safe)
        return httpx.Response(200, json={"allowed": allowed})

    use_filter(monkeypatch, check)
    result = run(guarded, auth, item["id"], TestGenerator(clip))
    assert seen[0] is True and seen[-1] is False
    assert result["status"] == "failed" and result["moderated"]
    with guarded.app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == 0
    assert not list(guarded.app.state.settings.media_dir.iterdir())


# Загружает исходное фото для ролика и получает отказ при его анализе. Генератор не вызывается,
# хотя текст уже разрешён.
def test_denied_reference_image_never_reaches_generator(guarded, monkeypatch):
    auth = account(guarded)
    photo = io.BytesIO()
    Image.new("RGB", (90, 90), "navy").save(photo, "JPEG")
    source = guarded.post(
        "/v1/assets", headers=auth, files={"file": ("source.jpg", photo.getvalue(), "image/jpeg")}
    ).json()
    item = job(guarded, auth, model="video", input_id=source["id"]).json()
    seen = []

    # Записывает порядок вызовов, разрешает текст и запрещает изображение. Без различия между
    # ответами нельзя было бы проверить остановку именно на исходнике.
    def check(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"allowed": [request.url.path == "/v1/text"]})

    use_filter(monkeypatch, check)
    model = TestGenerator()
    result = run(guarded, auth, item["id"], model)
    assert seen == ["/v1/text", "/v1/images"] and model.calls == 0
    assert result["moderated"] and result["output_id"] is None


# Сверяет порядок анализа текста и результата. Пока проверяется выходной файл, он ещё закрыт,
# после разрешения его можно скачать.
def test_result_is_published_only_after_a_successful_output_verdict(guarded, monkeypatch):
    auth = account(guarded)
    item = job(guarded, auth).json()
    model = TestGenerator()
    seen = []

    # Разрешает анализ и при запросе изображения проверяет, что модель уже отработала, а строки
    # файла ещё нет. Иначе порядок нельзя было бы доказать по готовому результату.
    def allow(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/images":
            assert model.calls == 1
            with guarded.app.state.factory() as db:
                assert db.scalar(select(func.count()).select_from(Asset)) == 0
        return httpx.Response(200, json={"allowed": [True]})

    use_filter(monkeypatch, allow)
    result = run(guarded, auth, item["id"], model)
    assert seen == ["/v1/text", "/v1/images"]
    assert result["status"] == "succeeded"
    assert guarded.get(f"/v1/assets/{result['output_id']}", headers=auth).status_code == 200


# Проверяет доступ только с ключом, отказ для пустого или повреждённого ввода и отсутствие
# исходных байтов в ошибке. Неполное решение модели тоже отклоняется.
def test_private_service_validates_auth_input_and_model_contract():
    from bootstrap.moderator import create_app

    class LocalTestClassifier:
        # Возвращает известные ответы по порядку текстов. По ним можно сверить, что служба
        # передала каждое решение своему элементу.
        def text(self, items):
            return [True, False][: len(items)]

        # Проверяет размеры уже открытого снимка и возвращает пустой ответ. Это вызывает отказ для
        # результата модели без решения.
        def images(self, items):
            assert items[0].size == (30, 40)
            return []

    encoded = io.BytesIO()
    Image.new("RGB", (30, 40), "white").save(encoded, "JPEG")
    token = {"Authorization": "Bearer isolated-key"}
    with TestClient(create_app("isolated-key", LocalTestClassifier)) as service:
        assert service.post("/v1/text", json={"items": ["example"]}).status_code == 401
        empty = service.post("/v1/text", headers=token, json={"items": []})
        assert empty.status_code == 422
        assert empty.json() == {"detail": "Invalid classifier input"}
        response = service.post(
            "/v1/text", headers=token, json={"items": ["input one", "input two"]}
        )
        assert response.json() == {"allowed": [True, False]}
        invalid = service.post("/v1/images", headers=token, json={"items": ["****"]})
        assert invalid.status_code == 422 and "****" not in invalid.text
        invalid_model = service.post(
            "/v1/images",
            headers=token,
            json={"items": [base64.b64encode(encoded.getvalue()).decode()]},
        )
        assert invalid_model.status_code == 503


# Проверяет неподдерживаемый тип, движущееся изображение и оборванный ролик. Все три файла должны
# получить отказ ещё до отправки службе.
@pytest.mark.parametrize("kind", ["unsupported", "animated", "truncated"])
def test_unsupported_files_are_rejected_before_any_classifier_request(tmp_path, kind):
    path = tmp_path / "reference"
    mime = {
        "unsupported": "application/octet-stream",
        "animated": "image/gif",
        "truncated": "video/mp4",
    }[kind]
    if kind == "animated":
        first = Image.new("RGB", (16, 16), "red")
        second = Image.new("RGB", (16, 16), "blue")
        first.save(path, "GIF", save_all=True, append_images=[second], duration=100)
    else:
        path.write_bytes(b"invalid local media")

    # Проваливает проверку при любом сетевом запросе. Некорректные файлы не должны выходить за
    # пределы местного разбора.
    def unexpected(_request):
        pytest.fail("The classifier must not receive unsupported input")

    # Подключает недопустимый сетевой вызов и ждёт ошибки разбора файла. Так отказ до сети
    # отличается от отказа самой службы.
    async def evaluate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            with pytest.raises(FilterUnavailable):
                await content_filter.check_media(client, path, mime, result=False)

    asyncio.run(evaluate())


# Создаёт короткое видео со звуком. Проверяет, что как исходник оно даёт кадр для анализа, а как
# результат отклоняется. Отдельно сверяет ранний отказ при превышении числа кадров.
def test_audio_is_allowed_in_a_reference_but_rejected_in_a_result(tmp_path, monkeypatch):
    clip = tmp_path / "with-audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=white:s=96x96:r=24:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(clip),
        ],
        check=True,
        timeout=20,
    )
    requests = []

    # Принимает запрос изображений, запоминает кадры и разрешает каждый. Число сохранённых кадров
    # показывает, дошёл ли неподходящий ролик до службы.
    def classify(request):
        assert request.url.path == "/v1/images"
        images = json.loads(request.content)["items"]
        requests.extend(images)
        return httpx.Response(200, json={"allowed": [True] * len(images)})

    # Запускает проверку одного ролика как исходника и результата, затем снижает предел кадров.
    # Только первый проход должен обратиться к службе.
    async def evaluate():
        async with httpx.AsyncClient(
            base_url="https://filter.test", transport=httpx.MockTransport(classify)
        ) as client:
            await content_filter.check_media(client, clip, "video/mp4", result=False)
            assert len(requests) == 1
            with Image.open(io.BytesIO(base64.b64decode(requests[0]))) as frame:
                assert frame.size == (96, 96)
            with pytest.raises(FilterUnavailable) as exc:
                await content_filter.check_media(client, clip, "video/mp4", result=True)
            assert "video streams" in str(exc.value.__cause__)
            assert len(requests) == 1

            monkeypatch.setattr(content_filter, "MAX_FRAMES", 2)
            with pytest.raises(FilterUnavailable) as exc:
                await content_filter.check_media(client, clip, "video/mp4", result=False)
            assert "frame count" in str(exc.value.__cause__)
            assert len(requests) == 1

    asyncio.run(evaluate())


# Запускает медленный дочерний процесс и отменяет разбор после его старта. Ожидает завершение
# процесса, иначе отменённая работа продолжила бы занимать ресурсы.
def test_cancelled_inspection_reaps_its_child_process(monkeypatch):
    # Дожидается настоящего запуска процесса, отменяет разбор и сверяет его выход. При ошибке
    # принудительно завершает только свой пробный процесс.
    async def evaluate():
        created = asyncio.get_running_loop().create_future()
        start = asyncio.create_subprocess_exec

        # Запускает обычный дочерний процесс и сообщает проверке его состояние. Отмена должна
        # произойти уже после запуска, иначе останется непроверенным освобождение ресурсов.
        async def observe(*args, **kwargs):
            process = await start(*args, **kwargs)
            created.set_result(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
        inspection = asyncio.create_task(
            content_filter.inspect_file(sys.executable, "-c", "import time; time.sleep(60)")
        )
        process = await asyncio.wait_for(created, 5)
        assert process.returncode is None
        inspection.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(inspection, 5)
            assert process.returncode is not None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(evaluate())


# Подаёт пустой кадр и кадр на байт сверх предела. Оба отклоняются до преобразования в текст,
# иначе бесполезные байты занимали бы память.
def test_frame_limits_are_checked_before_encoding():
    for payload in (b"", b"x" * (content_filter.MAX_FRAME_BYTES + 1)):
        with pytest.raises(ValueError, match="Invalid moderation frame size"):
            content_filter.as_base64(payload)


# Подставляет процесс, который успел выйти перед остановкой. После ошибки остановки разбор всё
# равно должен дождаться выхода и вернуть отмену.
def test_cancelled_inspection_waits_for_a_process_that_has_just_exited(monkeypatch):
    observed = []

    class ExitingProcess:
        returncode = None

        # Прерывает ожидание ответа процесса. Это переводит разбор в путь отмены.
        async def communicate(self):
            raise asyncio.CancelledError

        # Отмечает попытку остановки и сообщает, что процесс уже исчез. Без этого проверка прошла
        # бы только обычную отмену.
        def kill(self):
            observed.append("kill")
            raise ProcessLookupError

        # Отмечает ожидание и выставляет код выхода. По списку действий потом видно, дождалось ли
        # приложение завершения.
        async def wait(self):
            observed.append("wait")
            self.returncode = 0

    # Подставляет процесс с заданным порядком отмены и выхода. Для этой гонки настоящий запуск не
    # нужен.
    async def start(*args, **kwargs):
        return ExitingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(content_filter.inspect_file("fixture"))
    assert observed == ["kill", "wait"]


# Сокращает срок ожидания ответа и отправляет изображение. В ответ получает ошибку времени и
# проверяет, что ожидание на стороне пробной службы отменено.
def test_output_deadline_cancels_the_pending_classifier_reply(guarded, monkeypatch, tmp_path):
    path = tmp_path / "sample.jpg"
    Image.new("RGB", (20, 20), "white").save(path)
    service = content_filter.ContentFilter(guarded.app.state.settings)
    service.timeout = 1
    started = asyncio.Event()
    cancelled = asyncio.Event()

    # Начинает обработку изображения и оставляет ответ ждать. При отмене отмечает завершение,
    # чтобы можно было обнаружить зависший сетевой вызов.
    async def stalled(request):
        assert request.url.path == "/v1/images"
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    use_filter(monkeypatch, stalled)
    with pytest.raises(FilterUnavailable) as failure:
        asyncio.run(service.check_output(path, "image/jpeg"))
    assert isinstance(failure.value.__cause__, TimeoutError)
    assert started.is_set() and cancelled.is_set()


# Проверяет отказ без папок и при неверном размере группы, затем запускает пробную службу.
# Правильное фото доходит до модели в нужном виде, ответы для неверного формата и размера
# отклоняются.
def test_moderator_starts_with_local_directories_and_validates_decoded_images(
    tmp_path, monkeypatch
):
    from bootstrap import moderator

    text = tmp_path / "text"
    photos = tmp_path / "photo"
    monkeypatch.setenv("STUDIO_MODERATION_TEXT_MODEL", str(text))
    monkeypatch.setenv("STUDIO_MODERATION_IMAGE_MODEL", str(photos))
    monkeypatch.setenv("STUDIO_MODERATION_TOKEN", "fixture-key")
    with pytest.raises(ValueError, match="local model directories"):
        moderator.from_environment()
    text.mkdir()
    photos.mkdir()
    monkeypatch.setenv("STUDIO_MODERATION_IMAGE_BATCH", "5")
    with pytest.raises(ValueError, match="supported image batch"):
        moderator.from_environment()
    monkeypatch.setenv("STUDIO_MODERATION_IMAGE_BATCH", "2")
    monkeypatch.setenv("STUDIO_MODERATION_DEVICE", "cpu")

    class Classifier:
        # Сверяет обе папки, устройство и число изображений на вызов. Так проверка подтверждает
        # чтение настроек при запуске.
        def __init__(self, text_path, photo_path, device, batch):
            assert (text_path, photo_path, device, batch) == (text, photos, "cpu", 2)

        # Проверяет размеры и цветовое представление открытого снимка и разрешает его. Модели
        # требуется уже разобранное изображение.
        def images(self, frames):
            assert [(frame.format, frame.mode, frame.size) for frame in frames] == [
                (None, "RGB", (30, 40)),
            ]
            return [True]

    monkeypatch.setattr(moderator, "LocalClassifier", Classifier)
    encoded = io.BytesIO()
    Image.new("L", (30, 40), 120).save(encoded, "JPEG")
    wrong_format = io.BytesIO()
    Image.new("RGB", (30, 40), "white").save(wrong_format, "PNG")
    token = {"Authorization": "Bearer fixture-key"}
    with TestClient(moderator.from_environment()) as service:
        assert service.get("/health", headers=token).json() == {"ok": True}
        good = service.post(
            "/v1/images",
            headers=token,
            json={"items": [base64.b64encode(encoded.getvalue()).decode()]},
        )
        assert good.status_code == 200 and good.json() == {"allowed": [True]}
        bad = service.post(
            "/v1/images",
            headers=token,
            json={"items": [base64.b64encode(wrong_format.getvalue()).decode()]},
        )
        assert bad.status_code == 422

        oversized = bytes(content_filter.MAX_FRAME_BYTES + 1)
        rejected = service.post(
            "/v1/images",
            headers=token,
            json={"items": [base64.b64encode(oversized).decode()]},
        )
        assert rejected.status_code == 422
        assert rejected.json() == {"detail": "Invalid classifier image"}


# Отменяет первый запрос, пока модель ещё работает, и отправляет второй. Следующий вызов должен
# дождаться фактического конца первого, иначе одна модель работала бы одновременно с двумя
# запросами.
def test_cancelled_moderation_keeps_the_model_busy_until_its_work_ends():
    from bootstrap import moderator

    with pytest.raises(ValueError, match="needs a token"):
        moderator.create_app("", lambda: None)

    # Удерживает первый расчёт, отменяет его сетевое ожидание и сверяет порядок двух вызовов. В
    # конце отпускает все пробные задачи даже при ошибке.
    async def evaluate():
        loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = threading.Event()
        order = []

        class Classifier:
            # Сообщает начало каждого расчёта и удерживает первый до разрешения. Запоминает конец,
            # чтобы внешняя проверка могла исключить одновременную работу.
            def text(self, items):
                label = items[0]
                order.append(("start", label))
                if label == "first":
                    loop.call_soon_threadsafe(first_started.set)
                    assert release_first.wait(5)
                else:
                    loop.call_soon_threadsafe(second_started.set)
                order.append(("finish", label))
                return [True]

        app = moderator.create_app("fixture-key", Classifier)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://filter.test",
                headers={"Authorization": "Bearer fixture-key"},
            ) as client,
        ):
            tasks = [asyncio.create_task(client.post("/v1/text", json={"items": ["first"]}))]
            first = tasks[0]
            try:
                await asyncio.wait_for(first_started.wait(), timeout=5)
                first.cancel()
                second = asyncio.create_task(client.post("/v1/text", json={"items": ["second"]}))
                tasks.append(second)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(second_started.wait(), timeout=0.05)
                assert not first.done() and not second.done()
                release_first.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(first, timeout=5)
                reply = await asyncio.wait_for(second, timeout=5)
                assert reply.json() == {"allowed": [True]}
            finally:
                release_first.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert order == [
            ("start", "first"),
            ("finish", "first"),
            ("start", "second"),
            ("finish", "second"),
        ]

    asyncio.run(evaluate())
