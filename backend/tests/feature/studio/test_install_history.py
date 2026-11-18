# Проверяет установку исходных файлов для начальных чатов из проверяемого архива. Ошибка проверки
# или одновременный запуск не должны оставлять наполовину готовый набор.

import hashlib
import io
import runpy
import stat
import sys
import tarfile

import pytest

from app.Console.Commands import install_history
from database.seeders import starter_history


# Собирает малый архив с заданными файлами и подставляет описание с контрольными суммами.
# Позволяет намеренно разойтись с описанием, не загружая настоящий набор материалов.
def write_archive(tmp_path, monkeypatch, *, contents, catalog=None, archive_checksum=None):
    archive = tmp_path / "history.tar"
    with tarfile.open(archive, "w") as destination:
        for name, data in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            destination.addfile(entry, io.BytesIO(data))
    catalog = contents if catalog is None else catalog
    assets = {
        hashlib.sha256(data).hexdigest(): {"name": name, "size": len(data)}
        for name, data in catalog.items()
    }
    expected = archive_checksum or hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / "archive.sha256").write_text(expected)

    for module in (install_history, starter_history):
        monkeypatch.setattr(module, "CATALOG", tmp_path / "history.json.gz")
        monkeypatch.setattr(module, "history_catalog", lambda: {"assets": assets})
    return archive, expected


# Запускает установку, сверяет байты, права только на чтение и отметку о готовности. Повтор
# принимает уже готовый набор, иначе команда меняла бы рабочие исходники без причины.
def test_history_command_installs_verified_files_and_accepts_a_repeat(tmp_path, monkeypatch):
    contents = {"first.jpg": b"catalog original", "video.mp4": b"second original"}
    archive, expected = write_archive(tmp_path, monkeypatch, contents=contents)
    media = tmp_path / "media"
    module = "app.Console.Commands.install_history"
    monkeypatch.setattr(sys, "argv", [module, "--media-dir", str(media), "--archive", str(archive)])
    runpy.run_path(install_history.__file__, run_name="__main__")

    published = media / install_history.ORIGINALS_DIR
    for name, data in contents.items():
        assert (published / name).read_bytes() == data
        assert stat.S_IMODE((published / name).stat().st_mode) == 0o444
    assert (published / "archive.sha256").read_text() == expected
    archive.unlink()
    install_history.install(media, archive)
    assert list(media.iterdir()) == [published]


# Подставляет несколько видов повреждённого набора и проверяет отказ. После ошибки не должно
# остаться даже исправных файлов из этого архива. Иначе неполный набор выглядел бы готовым.
@pytest.mark.parametrize(
    ("contents", "catalog", "archive_checksum", "message"),
    [
        ({"first.jpg": b"ok"}, None, "0" * 64, "сумма архива истории"),
        ({"first.jpg": b"a"}, {"first.jpg": b"longer"}, None, "повреждён файл"),
        ({"first.jpg": b"a"}, {"first.jpg": b"b"}, None, "сумма файла истории"),
        ({}, {"first.jpg": b"a"}, None, "не хватает файлов"),
    ],
)
def test_invalid_archive_leaves_no_partial_originals(
    tmp_path, monkeypatch, contents, catalog, archive_checksum, message
):
    archive, _ = write_archive(
        tmp_path, monkeypatch, contents=contents, catalog=catalog, archive_checksum=archive_checksum
    )
    media = tmp_path / "media"
    with pytest.raises(ValueError, match=message):
        install_history.install(media, archive)
    assert list(media.iterdir()) == []


# Подменяет скачивание пробным архивом и проверяет, что команда его проверяет и публикует. При
# повторе готовый набор используется без нового скачивания.
def test_downloaded_archive_is_checked_and_published_once(tmp_path, monkeypatch):
    archive, _ = write_archive(tmp_path, monkeypatch, contents={"first.jpg": b"original"})
    payload = archive.read_bytes()
    urls = []

    # Запоминает адрес и срок ожидания и возвращает пробные байты. Так проверяется вызов
    # скачивания без внешней сети.
    def open_archive(url, *, timeout):
        urls.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(install_history.urllib.request, "urlopen", open_archive)
    media = tmp_path / "media"
    install_history.install(media)
    install_history.install(media)

    assert urls == [(install_history.URL, 120)]
    assert (media / install_history.ORIGINALS_DIR / "first.jpg").read_bytes() == b"original"


# Имитирует одновременную публикацию другим установщиком. Своя попытка может принять чужой
# результат только с правильной отметкой о готовности, иначе набор мог бы оказаться неполным.
@pytest.mark.parametrize("valid_marker", [True, False])
def test_concurrent_archive_publish_requires_a_valid_marker(tmp_path, monkeypatch, valid_marker):
    archive, expected = write_archive(tmp_path, monkeypatch, contents={"first.jpg": b"original"})
    media = tmp_path / "media"
    published = media / install_history.ORIGINALS_DIR

    # Публикует набор от имени первого установщика перед попыткой второго, затем имитирует отказ
    # переименования. Это воспроизводит гонку в нужном порядке.
    def publish_first(_source, target):
        assert target == published
        target.mkdir()
        (target / "archive.sha256").write_text(expected if valid_marker else "invalid")
        (target / "first.jpg").write_bytes(b"first installer")
        raise FileExistsError(target)

    monkeypatch.setattr(install_history.os, "rename", publish_first)
    if valid_marker:
        install_history.install(media, archive)
    else:
        with pytest.raises(FileExistsError):
            install_history.install(media, archive)
    assert list(media.iterdir()) == [published]
    assert (published / "first.jpg").read_bytes() == b"first installer"
