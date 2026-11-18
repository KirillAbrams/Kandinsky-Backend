# Проверяет чтение размеров ролика, счётчики шагов и кадры, передаваемые на сохранение. Тяжёлую
# работу заменяет модель с известными номерами кадров.

import base64
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.Services.Gpu.Runner.video_sr import DenoiseProgress, source_geometry, upscale
from tests.support.fixtures import MOBILE_FIXTURES


# Создаёт короткие ролики по обе стороны границ размеров и длительности. Проверяет, что решение
# принимается по размерам и длительности, прочитанным из файла.
@pytest.mark.parametrize(
    ("width", "height", "duration", "allowed"),
    [
        (128, 128, 1, True),
        (540, 960, 5, True),
        (126, 128, 1, False),
        (962, 128, 1, False),
        (800, 800, 1, False),
        (128, 128, 0.5, False),
        (128, 128, 5.5, False),
    ],
)
def test_video_resolution_checks_real_mp4_metadata(tmp_path, width, height, duration, allowed):
    source = tmp_path / "source.mp4"
    # Два кадра в секунду нужны только для точной длительности проверочного ролика.
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=size={width}x{height}:rate=2:duration={duration}",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    if allowed:
        assert source_geometry(source) == (width, height)
    else:
        with pytest.raises(ValueError, match="Invalid MP4"):
            source_geometry(source)


# Сверяет число шагов по частям ролика и запускает счётчик ещё раз. Следующая работа должна
# начаться с нуля после сброса прежнего процента.
def test_denoise_progress_reports_full_tile_count_and_resets_between_runs():
    samples = []
    progress = DenoiseProgress(lambda done, total: samples.append((done, total)))
    progress.start(3, 2)
    progress.update()
    progress.update(5)
    progress.close()
    progress.start(1, 2)
    progress.update(2)
    assert samples == [(0, 6), (1, 6), (6, 6), (0, 2), (2, 2)]


class InputFrames:
    # Запоминает номера кадров и описывает форму входного набора. Тест может сверить размер и
    # порядок без хранения изображений всех кадров.
    def __init__(self, indices):
        self.indices = tuple(indices)
        self.shape = (len(self.indices), 3, 256, 256)

    # Возвращает набор с выбранным отрезком кадров. Это показывает, какие кадры осталось передать
    # модели после ограничения длины.
    def __getitem__(self, span):
        return InputFrames(self.indices[span])

    # Повторяет выбранные кадры при неизменных остальных размерах. Проверка видит, что добивка
    # использовала последний готовый кадр для добивки.
    def expand(self, count, *dimensions):
        assert dimensions == (-1, -1, -1)
        return InputFrames(self.indices * count)


# Проверяет дополнение последним кадром, предел длины и отказ при неверном выходном размере.
# Сверяет частоту сохранения, очистку временной папки и наличие процентов только по запросу.
@pytest.mark.parametrize(
    ("total", "kept", "padding", "width", "track_progress"),
    [(10, 10, 7, 512, True), (136, 121, 0, 512, False), (9, 9, 0, 256, False)],
    ids=["pad-with-last-frame", "cap-at-121-frames", "reject-wrong-size"],
)
def test_upscale_passes_correct_frames_and_fps_to_encoder(
    monkeypatch, tmp_path, total, kept, padding, width, track_progress
):
    original = (MOBILE_FIXTURES / "media/video-01.mp4").read_bytes()
    source_paths = []
    samples = []
    output = tmp_path / "result.mp4"

    # Запоминает временный путь и сверяет исходные байты. Возвращает известную частоту и один
    # исходный кадр для контролируемой смены частоты.
    def read_source(source):
        source_paths.append(source)
        assert source.read_bytes() == original
        return InputFrames([0]), 12

    # Проверяет, что получил ожидаемые кадр и частоту, затем выдаёт набор нужной длины. Сценарий
    # может отдельно проверить дополнение и обрезку.
    def resample(frames, fps):
        assert frames.indices == (0,)
        assert fps == 12
        return InputFrames(range(total)), 24

    class OutputFrames:
        # Сохраняет последовательность после обработки. При сохранении тест сравнит её с тем, что
        # было передано модели.
        def __init__(self, indices):
            self.indices = indices

        # Проверяет выбор первого результата и всех каналов и применяет отрезок по времени.
        # Возвращает форму, по которой код решает, можно ли сохранять видео.
        def __getitem__(self, selection):
            batch, channels, time = selection
            assert batch == 0
            assert channels == slice(None)
            indices = self.indices[time]
            return SimpleNamespace(indices=indices, shape=(3, len(indices), 512, width))

    # Сверяет номера входных кадров, исходное число и способ выдачи процентов. Отдаёт
    # предсказуемую последовательность, чтобы проверить путь до сохранения без вычислений.
    def run_model(*, video, seed, show_progress, **kwargs):
        assert video.indices == tuple(range(kept)) + (kept - 1,) * padding
        assert seed == 917
        assert show_progress is False
        if track_progress:
            reporter = kwargs["progress"]
            reporter.start(2, 2)
            reporter.update(4)
            reporter.close()
        else:
            assert kwargs == {}
        return SimpleNamespace(frames=OutputFrames(video.indices))

    models = tmp_path / "models"
    monkeypatch.setenv("KANDINSKY_MODELS", str(models))
    make_config = Mock(return_value=object())
    load_model = Mock(return_value=run_model)
    mux = Mock()
    for module, substitute in {
        "torch": SimpleNamespace(
            cat=lambda parts: InputFrames(parts[0].indices + parts[1].indices),
            inference_mode=nullcontext,
        ),
        "kandinsky_sr.core.algo.mux": SimpleNamespace(mux_video_audio=mux),
        "kandinsky_sr.pipeline.config": SimpleNamespace(SRConfig=make_config),
        "kandinsky_sr.pipeline.factory": SimpleNamespace(load_sr_pipeline=load_model),
        "kandinsky_sr.pipeline.video_io": SimpleNamespace(
            read_video_tchw_uint8=read_source, resample_to_target_fps=resample
        ),
    }.items():
        monkeypatch.setitem(sys.modules, module, substitute)

    # Записывает шаги и общий объём для последующей сверки. Если счётчик подключился без запроса
    # или потерял обновление, список это покажет.
    def report(done, count):
        samples.append((done, count))

    request = {"video": base64.b64encode(original).decode(), "seed": 917}
    if width == 512:
        upscale(request, output, report=report if track_progress else None)
        args, kwargs = mux.call_args
        assert mux.call_count == 1
        assert args[0].indices == tuple(range(kept))
        assert args[1:] == (None, output)
        assert kwargs == {"fps": 24}
    else:
        with pytest.raises(
            ValueError, match=r"returned \(3, 9, 512, 256\), expected \(3, 9, 512, 512\)"
        ):
            upscale(request, output)
        mux.assert_not_called()
    make_config.assert_called_once_with(
        checkpoint_path=str(models / "video_sr"), resolution_scale=2, num_steps=2, device="cuda:0"
    )
    load_model.assert_called_once_with(
        make_config.return_value, "cuda:0", force=True, resolution_scale=2
    )
    assert samples == ([(0, 4), (4, 4)] if track_progress else [])
    assert len(source_paths) == 1
    assert not source_paths[0].parent.exists()
