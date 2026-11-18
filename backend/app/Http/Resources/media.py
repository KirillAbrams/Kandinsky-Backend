"""Собирает короткий ответ для списка файлов и успешной загрузки."""

from app.Models import Asset


def asset_summary(asset: Asset) -> dict[str, str | int]:
    return {"id": asset.id, "mime": asset.mime, "size": asset.size}
