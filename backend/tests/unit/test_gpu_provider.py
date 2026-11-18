# Проверяет обмен с вычислительной машиной на подставных ответах. Сценарии сверяют команды, ход,
# получение файла и отказы без платного запуска.

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.Providers.generation import make_provider
from app.Services.Gpu import provider as providers


# Возвращает способ собрать исполнителя с подставным обменом. Так проверка может считать обращения
# и не ждать настоящих пауз опроса.
@pytest.fixture
def gpu_transport(monkeypatch):
    actual_client = httpx.AsyncClient

    # Подставляет обработчик сетевых ответов и заменяет задержку между запросами. Исполнитель
    # проходит свой обычный путь без выхода в сеть.
    def configure(handler):
        # Сверяет длительность ожидаемой паузы и сразу продолжает работу. Так тест остаётся
        # быстрым и замечает смену частоты опроса.
        async def skip_poll_delay(seconds):
            assert seconds == 2

        # Создаёт обычный клиент с подставной передачей запросов. Настройки адреса и ключа по
        # прежнему проходят через рабочий код.
        def client(**options):
            return actual_client(**options, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(providers.httpx, "AsyncClient", client)
        monkeypatch.setattr(providers.asyncio, "sleep", skip_poll_delay)
        return providers.GpuProvider("https://gpu.test/", "isolated-provider-test")

    return configure


# Даёт одинаковую запись задания с текстом и параметрами размера. Сценарии могут сравнивать полную
# отправку, меняя только нужный ответ машины.
@pytest.fixture
def generation():
    return SimpleNamespace(
        id="generation",
        run_id=None,
        prompt="Магнолия",
        model="image",
        aspect="5:3",
        seed=42,
        quality="standard",
        resolution="compact",
        duration_seconds=5,
        negative_prompt="",
        guidance_scale=None,
    )


# Проходит очередь, работу и готовый результат без слушателя процентов. Проверяет файл и
# отсутствие лишних запросов после успеха.
def test_gpu_writes_image_and_accepts_running_progress_without_a_callback(
    gpu_transport, generation, tmp_path
):
    steps = iter(
        [
            ("POST", "/jobs/generation", {"status": "queued"}),
            ("GET", "/jobs/generation", {"status": "running", "progress": 30}),
            ("GET", "/jobs/generation", {"status": "succeeded"}),
            ("GET", "/jobs/generation/result", None),
        ]
    )

    # Сверяет порядок команд, ключ и параметры исходной работы. Отдаёт готовые байты только по
    # адресу результата, чтобы преждевременное чтение стало заметно.
    def respond(request):
        method, path, state = next(steps)
        assert (request.method, request.url.path) == (method, path)
        assert request.headers["Authorization"] == "Bearer isolated-provider-test"
        if method == "POST":
            body = json.loads(request.content)
            assert (body["canonical_job_id"], body["aspect"], body["image"]) == (
                "generation",
                "5:3",
                None,
            )
        if state is None:
            return httpx.Response(200, content=b"rendered image")
        return httpx.Response(200, json=state)

    output = tmp_path / "result.png"
    assert asyncio.run(gpu_transport(respond).generate(generation, output, None)) == "image/png"
    assert output.read_bytes() == b"rendered image"
    assert list(steps) == []


# Возвращает отмену или ошибку и проверяет завершение без создания файла. Опрос не должен
# принимать конечный отказ за ожидание.
@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_gpu_terminal_failure_never_downloads_a_file(gpu_transport, generation, tmp_path, status):
    # Выдаёт выбранное конечное состояние и требует адрес самого задания. Попытка затем запросить
    # файл нарушит это ожидание.
    def respond(request):
        assert request.url.path == "/jobs/generation"
        return httpx.Response(200, json={"status": status})

    output = tmp_path / "result.png"
    with pytest.raises(RuntimeError, match="GPU generation failed or was cancelled"):
        asyncio.run(gpu_transport(respond).generate(generation, output, None))
    assert not output.exists()


# Держит задание в очереди до полного предела запросов. Проверяет отказ по времени и отсутствие
# неполного результата.
def test_gpu_stops_polling_a_job_that_never_finishes(gpu_transport, generation, tmp_path):
    polls = 0

    # Считает обращения за состоянием и возвращает ожидание при каждом. Тест может точно проверить
    # предел, даже не ожидая сетевых пауз.
    def respond(request):
        nonlocal polls
        assert request.url.path == "/jobs/generation"
        polls += request.method == "GET"
        return httpx.Response(200, json={"status": "queued"})

    output = tmp_path / "result.png"
    with pytest.raises(TimeoutError, match="GPU generation timeout"):
        asyncio.run(gpu_transport(respond).generate(generation, output, None))
    assert polls == 900
    assert not output.exists()


# Подаёт результат чуть больше допустимого и сверяет отказ, записанный объём и закрытие ответа.
# Недопустимая порция не должна попасть в файл.
def test_gpu_rejects_oversized_result_and_closes_the_stream(gpu_transport, generation):
    class Oversized(httpx.AsyncByteStream):
        closed = False

        # Выдаёт одинаковые маленькие порции до превышения общего размера. Благодаря этому тест не
        # хранит весь большой ответ в памяти.
        async def __aiter__(self):
            chunk = b"r" * (1024 * 1024)
            for _ in range(257):
                yield chunk

        # Отмечает закрытие потока. Проверка по этой отметке обнаружит оставленное соединение
        # после отказа.
        async def aclose(self):
            self.closed = True

    class Output:
        written = 0

        # Сверяет режим записи и возвращает счётчик вместо файла. Так сценарий измеряет поток без
        # записи большого файла на диск.
        def open(self, mode):
            assert mode == "wb"
            return self

        # Отдаёт счётчик в блок записи результата. Производственный код по-прежнему открывает его
        # привычным способом.
        def __enter__(self):
            return self

        # Даёт исключению выйти наружу после закрытия области записи. Тест должен увидеть
        # настоящий отказ по размеру.
        def __exit__(self, *exc):
            pass

        # Суммирует байты, не сохраняя содержимое. Так можно доказать, что лишняя порция не была
        # принята.
        def write(self, data):
            self.written += len(data)

    stream = Oversized()

    # Сообщает о готовом задании, а по запросу результата выдаёт слишком большой поток.
    # Исполнитель узнаёт превышение только после чтения порций.
    def respond(request):
        if request.url.path.endswith("/result"):
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, json={"status": "succeeded"})

    output = Output()
    with pytest.raises(ValueError, match="GPU result too large"):
        asyncio.run(gpu_transport(respond).generate(generation, output, None))
    assert output.written == 256 * 1024 * 1024
    assert stream.closed


# Сверяет разные адреса отмены попытки и удаления исходной работы, затем проверяет три ответа.
# Только явно подтверждённое удаление считается завершённым.
@pytest.mark.parametrize("code", [204, 200, 403])
def test_gpu_cancels_an_attempt_and_requires_erasure_ack(gpu_transport, code):
    requests = []

    # Запоминает способ обращения, адрес и дополнительные поля и отдаёт выбранный статус. По
    # записанному адресу видно, отделил ли код попытку от исходной работы.
    def respond(request):
        requests.append((request.method, request.url.path, request.url.query))
        return httpx.Response(code)

    provider = gpu_transport(respond)
    asyncio.run(provider.cancel("attempt"))
    if code == 204:
        asyncio.run(provider.erase("generation"))
    elif code == 200:
        with pytest.raises(providers.GpuUnavailable, match="did not confirm"):
            asyncio.run(provider.erase("generation", "attempt"))
    else:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            asyncio.run(provider.erase("generation", "attempt"))
        assert failure.value.response.status_code == 403

    assert requests == [
        ("POST", "/jobs/attempt/cancel", b""),
        ("DELETE", "/jobs/generation", b"" if code == 204 else b"run_id=attempt"),
    ]


# Проверяет местный исполнитель, отказ при пустом ключе, настроенную вычислительную службу и
# незнакомое значение. Ошибка настроек должна остановить выбор модели.
def test_provider_configuration_selects_only_supported_modes():
    settings = SimpleNamespace(provider="demo", gpu_url="https://gpu.test/", gpu_token="")
    assert isinstance(make_provider(settings), providers.DemoProvider)
    settings.provider = "kandinsky"
    with pytest.raises(ValueError, match="STUDIO_GPU_TOKEN"):
        make_provider(settings)
    settings.gpu_token = "isolated-provider-test"
    assert isinstance(make_provider(settings), providers.GpuProvider)
    settings.provider = "unknown"
    with pytest.raises(ValueError, match="Provider must be demo or kandinsky"):
        make_provider(settings)
