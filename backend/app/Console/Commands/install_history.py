# Готовит оригиналы для двух начальных чатов и кладёт их в общую папку только после сверки.
# Регистрация сможет брать готовые файлы без распаковки архива при каждом входе.

"""Проверяет и устанавливает оригиналы для стартовой истории на томе с медиа."""

import argparse
import hashlib
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from database.seeders.starter_history import CATALOG, ORIGINALS_DIR, history_catalog

URL = "https://github.com/KirillAbrams/Kandinsky-Backend/releases/download/history-v1/kandinsky-history-almaty-v2.tar"


# Сверяет общий отпечаток архива, принимает только перечисленные файлы нужного размера и проверяет
# каждый отпечаток. Переименовывает подготовленную папку целиком, чтобы регистрация не увидела
# половину набора.
def install(media_dir, archive=None):
    expected = (CATALOG.parent / "archive.sha256").read_text().strip()
    target = media_dir / ORIGINALS_DIR
    marker = target / "archive.sha256"
    if marker.is_file() and marker.read_text().strip() == expected:
        return
    media_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=media_dir) as scratch:
        staging = Path(scratch)
        if archive is None:
            archive = staging / "history.tar"
            with urllib.request.urlopen(URL, timeout=120) as source, archive.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise ValueError("Контрольная сумма архива истории не совпала")
        entries = {e["name"]: (sha, e["size"]) for sha, e in history_catalog()["assets"].items()}
        originals = staging / "originals"
        originals.mkdir()
        with tarfile.open(archive, "r:") as source:
            for member in source:
                checksum, size = entries.pop(member.name)
                if not member.isfile() or member.size != size:
                    raise ValueError("В архиве истории повреждён файл")
                path = originals / member.name
                with source.extractfile(member) as stream, path.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                        raise ValueError("Контрольная сумма файла истории не совпала")
                path.chmod(0o444)
        if entries:
            raise ValueError("В архиве истории не хватает файлов")
        (originals / "archive.sha256").write_text(expected)
        try:
            os.rename(originals, target)
        except OSError:
            if not marker.is_file() or marker.read_text().strip() != expected:
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", type=Path, default=Path("/media"))
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    install(args.media_dir, args.archive)
