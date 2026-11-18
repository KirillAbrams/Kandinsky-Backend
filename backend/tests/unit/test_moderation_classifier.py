# Сверяет правила разбора ответов модели, выбор устройства и порядок обработки текста и
# изображений. Подставные модели выполняют проверки без загрузки весов.

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from app.Services.Moderation.classifier import IMAGE_LABEL, TEXT_LABEL, LocalClassifier, verdict
from config.studio import Settings


# Сравнивает точные ответы для обычного, спорного и запрещённого содержимого. Только разрешающая
# отметка выбранной модели должна пропустить материал.
@pytest.mark.parametrize(
    ("answer", "label", "allowed", "expected"),
    [
        ("Safety: Safe\nBenign description", TEXT_LABEL, "Safe", True),
        ("Safety: Controversial", TEXT_LABEL, "Safe", False),
        ("Safety: Unsafe", TEXT_LABEL, "Safe", False),
        ("User Safety: safe", IMAGE_LABEL, "safe", True),
        ("User Safety: unsafe", IMAGE_LABEL, "safe", False),
    ],
)
def test_only_the_allowed_model_label_passes(answer, label, allowed, expected):
    assert verdict(answer, label, allowed) is expected


# Проверяет пустой ответ, отметку после пояснения и противоречивый ответ. Неоднозначность должна
# стать ошибкой, чтобы не пропустить содержимое по случайному слову.
@pytest.mark.parametrize(
    "answer", ["", "Explanatory text\nSafety: Safe", "Safety: Safe\nOther label: Safety: Unsafe"]
)
def test_missing_or_conflicting_model_labels_are_rejected(answer):
    with pytest.raises(ValueError, match="Invalid model verdict"):
        verdict(answer, TEXT_LABEL, "Safe")


# Подставляет загрузчики моделей и записи вызовов с управляемыми устройствами и результатами. Тест
# может наблюдать текст, группы кадров и пути весов без настоящего вычисления.
@pytest.fixture
def model_stand(monkeypatch):
    device = SimpleNamespace(cuda=False, mps=False)
    loads = []
    prompts = []
    image_sizes = []
    tensor_moves = []
    calls = []

    class Inputs(dict):
        # Запоминает ответы и форму подготовленного набора со строками одинаковой длины. Это даёт
        # коду параметры, по которым он отрежет исходный запрос от ответа.
        def __init__(self, replies):
            super().__init__(input_ids=SimpleNamespace(shape=(len(replies), 3)), replies=replies)

        # Записывает выбранное устройство и настройки чисел без перемещения данных. Проверка
        # сможет уточнить, какие настройки использованы для текста и картинок.
        def to(self, target, **options):
            tensor_moves.append((target, options))
            return self

    class Output:
        # Ставит перед каждым решением знакомые части запроса. Сценарий заметит, если чтение
        # решения случайно включит исходный текст.
        def __init__(self, replies):
            self.rows = [["prompt-0", "prompt-1", "prompt-2", reply] for reply in replies]

        # Выбирает строки и часть по позиции в каждой строке. Так проверяется точная граница, с
        # которой код читает результат модели.
        def __getitem__(self, selection):
            rows, tokens = selection
            return [row[tokens] for row in self.rows[rows]]

    class Model:
        # Запоминает вид подставной модели. Позже по нему можно отличить загрузку текста от
        # загрузки изображений.
        def __init__(self, variant):
            self.variant = variant

        # Возвращает ту же модель в цепочке подготовки. Подставная модель остаётся доступной для
        # последующей проверки выбора устройства.
        def eval(self):
            return self

        # Записывает устройство вместе с видом модели. Это показывает, что обе модели перенесены
        # туда, куда требовала настройка.
        def to(self, target):
            calls.append((self.variant, target))
            return self

        # Сверяет число записей, предел длины и отсутствие случайного выбора. Возвращает
        # подготовленные решения, чтобы дальше проверить их чтение.
        def generate(self, *, replies, input_ids, **options):
            assert input_ids.shape[0] == len(replies)
            assert options["max_new_tokens"] == 24
            assert options["do_sample"] is False
            return Output(replies)

    # Склеивает оставшиеся части ответа и сверяет режим пропуска служебных знаков. Исходный текст
    # должен быть отрезан раньше этого шага.
    def decode(rows, **options):
        assert options == {"skip_special_tokens": True}
        return ["".join(tokens) for tokens in rows]

    class Tokenizer:
        all_special_tokens = ["<control>", "plain-token"]

        # Сверяет одного автора пользовательского сообщения и возвращает переданный текст. Тест
        # сможет увидеть, как изменили управляющие знаки до подготовки.
        def apply_chat_template(self, messages, *, tokenize):
            assert tokenize is False
            assert len(messages) == 1 and messages[0]["role"] == "user"
            return messages[0]["content"]

        # Проверяет параметры подготовки текста, сохраняет строки и выдаёт два разных решения. Это
        # позволяет подтвердить и порядок, и значение ответа.
        def __call__(self, messages, **options):
            assert options == {"padding": True, "return_tensors": "pt", "add_special_tokens": False}
            prompts.extend(messages)
            return Inputs(["Safety: Safe", "Safety: Unsafe"])

        batch_decode = staticmethod(decode)

    class Processor:
        tokenizer = SimpleNamespace(padding_side="right")

        # Сверяет параметры группы, запоминает размеры изображений и выбирает решение по ширине.
        # Перестановка картинок или потеря последней порции станет заметна.
        def apply_chat_template(self, prompts, **options):
            assert options["add_generation_prompt"] is True and options["enable_thinking"] is False
            assert options["processor_kwargs"] == {"padding": True}
            sizes = [items[0]["content"][0]["image"].size for items in prompts]
            image_sizes.append(sizes)
            return Inputs(
                ["User Safety: safe" if w == 8 else "User Safety: unsafe" for w, _ in sizes]
            )

        batch_decode = staticmethod(decode)

    # Заворачивает создание подставного объекта в обычный способ загрузки с именем. Запись вызова
    # позже покажет, какая модель или средство подготовки использовалось.
    def loader(name, create):
        # Сохраняет имя, папку и параметры загрузки, затем создаёт подставной объект. Проверяет
        # выбор местных весов без открытия сетевых файлов.
        def load(path, **options):
            loads.append((name, path, options))
            return create()

        return SimpleNamespace(from_pretrained=load)

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: device.cuda),
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: device.mps)),
            float32="float32",
            bfloat16="bfloat16",
            inference_mode=nullcontext,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=loader("tokenizer", Tokenizer),
            AutoProcessor=loader("processor", Processor),
            AutoModelForCausalLM=loader("text", lambda: Model("text")),
            Gemma3ForConditionalGeneration=loader("image", lambda: Model("image")),
        ),
    )
    return SimpleNamespace(
        device=device,
        loads=loads,
        prompts=prompts,
        image_sizes=image_sizes,
        tensor_moves=tensor_moves,
        calls=calls,
    )


# Перебирает доступные устройства и ручное указание процессора. Сравнивает местные пути, режим
# чтения весов и настройки подготовки, чтобы ошибка выбора не стала незаметной.
@pytest.mark.parametrize(
    ("explicit", "cuda", "mps", "target", "dtype"),
    [
        (None, True, True, "cuda", "bfloat16"),
        (None, False, True, "mps", "bfloat16"),
        (None, False, False, "cpu", "float32"),
        ("cpu", True, True, "cpu", "float32"),
    ],
)
def test_classifiers_load_local_assets_on_the_selected_device(
    model_stand, explicit, cuda, mps, target, dtype
):
    model_stand.device.cuda = cuda
    model_stand.device.mps = mps
    classifier = LocalClassifier("/fixture/text", "/fixture/image", device=explicit)
    assert model_stand.calls == [("text", target), ("image", target)]
    model = {"local_files_only": True, "dtype": dtype, "use_safetensors": True}
    assert model_stand.loads == [
        ("tokenizer", "/fixture/text", {"local_files_only": True, "padding_side": "left"}),
        ("text", "/fixture/text", model),
        ("processor", "/fixture/image", {"local_files_only": True}),
        ("image", "/fixture/image", model),
    ]
    assert classifier.processor.tokenizer.padding_side == "left"


# Проверяет обезвреживание служебных знаков, отделение исходного текста и порядок решений по двум
# порциям изображений. Также сверяет перенос данных на выбранное устройство.
def test_classification_excludes_input_tokens_and_preserves_label_order_across_batches(model_stand):
    classifier = LocalClassifier(
        "/fixture/text", "/fixture/image", device="cpu", image_batch_size=2
    )
    assert classifier.text(["A <control> marker", "A plain-token marker"]) == [True, False]
    assert model_stand.prompts == ["A ‹control› marker", "A plain-token marker"]
    images = [Image.new("RGB", (width, 4)) for width in (8, 6, 8)]
    assert classifier.images(images) == [True, False, True]
    assert model_stand.image_sizes == [[(8, 4), (6, 4)], [(8, 4)]]
    assert model_stand.tensor_moves == [
        ("cpu", {}),
        ("cpu", {"dtype": "float32"}),
        ("cpu", {"dtype": "float32"}),
    ]


# Включает обязательную проверку с неподходящим адресом и ждёт отказа при чтении настроек. Ошибка
# должна обнаружиться до первой генерации.
def test_required_moderation_rejects_invalid_service_configuration():
    with pytest.raises(ValidationError, match="Required moderation needs"):
        Settings(moderation_mode="required", moderation_origin="file:///tmp/model")
