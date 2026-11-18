# Проверяет форму описания и совместимость параметров генерации. Недопустимый набор должен
# отклоняться до записи задания.

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.Http.Requests.generation import BatchJobRequest, JobRequest


# Создаёт разные строки с пробелами и сверяет очищенный текст. После разбора уже сохранённого
# запроса поля остаются прежними, поэтому повтор не меняет смысл.
@given(st.text(alphabet="абвгд ", min_size=1, max_size=100).filter(lambda s: bool(s.strip())))
@settings(max_examples=40)
def test_prompt_normalization_is_idempotent(prompt):
    body = JobRequest(thread_id="x", prompt=prompt)
    assert body.prompt == prompt.strip()
    assert JobRequest(**body.model_dump()).model_dump() == body.model_dump()


# Передаёт несколько недоступных сочетаний режима и настроек и ждёт соответствующий отказ. Так
# видно, какая проверка формы сработала.
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"model": "video_sr"}, "Масштаб ×2"),
        ({"duration_seconds": 3}, "У изображения нет длительности"),
        ({"model": "video", "quality": "draft"}, "guidance=1"),
    ],
)
def test_generation_rejects_unsupported_combinations(options, message):
    with pytest.raises(ValidationError, match=message):
        JobRequest(thread_id="thread", prompt="Магнолия", **options)


# Передаёт описание из пробелов и группу для режима увеличения ролика. Оба запроса должны
# остановиться при проверке, иначе в очередь попадут пустое задание и неподдерживаемая группа.
def test_generation_rejects_whitespace_only_prompt_and_batch_upscaling():
    with pytest.raises(ValidationError, match="Опишите, что создать"):
        JobRequest(thread_id="thread", prompt=" \t ")
    with pytest.raises(ValidationError, match="создаёт один результат"):
        BatchJobRequest(
            thread_id="thread", prompt="Магнолия", count=2, model="video_sr", input_id="video"
        )
