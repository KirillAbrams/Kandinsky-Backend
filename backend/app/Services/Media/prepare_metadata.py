"""Досчитывает отпечаток и обложку при первом чтении файла."""

import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from app.Models import Asset, User
from app.Policies.ownership import owned
from app.Services.Media.files import display_photo_preview, preview_metadata


def prepare_asset_metadata(
    db: Session, user: User, asset_id: str, media_dir: Path, *, display_preview: bool
) -> dict[str, object]:
    """Досчитывает отпечаток и обложку при первом запросе свойств файла."""
    asset = owned(db, Asset, asset_id, user)
    if asset.sha256 is None:
        with (media_dir / asset.filename).open("rb") as file:
            asset.sha256 = hashlib.file_digest(file, "sha256").hexdigest()
        db.commit()
    if asset.preview_info is None:
        asset.preview_info = preview_metadata(media_dir / asset.filename, asset.mime)
        db.commit()
    info = asset.preview_info
    if display_preview and asset.mime.startswith("image/"):
        if poster := display_photo_preview(media_dir / asset.filename):
            info = {**info, "poster": poster}
    return {
        "id": asset.id,
        "size": asset.size,
        "mime": asset.mime,
        "sha256": asset.sha256,
        **info,
    }
