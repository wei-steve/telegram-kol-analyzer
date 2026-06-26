"""Deduplicate excessive media asset rows produced by replayed syncs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.models import MediaAsset


@dataclass(slots=True)
class MediaDedupeResult:
    duplicate_message_groups: int = 0
    scanned_assets: int = 0
    deleted_assets: int = 0
    kept_assets: int = 0


def dedupe_media_assets(
    session_factory: sessionmaker,
    *,
    media_root: str | Path = "data/media",
    dry_run: bool = True,
) -> MediaDedupeResult:
    """Keep one best media asset per raw message and remove replay duplicates."""

    result = MediaDedupeResult()
    root = Path(media_root)

    with session_factory() as session:
        duplicate_message_ids = [
            int(raw_message_id)
            for raw_message_id, count in (
                session.query(MediaAsset.raw_message_id, func.count(MediaAsset.id))
                .group_by(MediaAsset.raw_message_id)
                .having(func.count(MediaAsset.id) > 1)
                .all()
            )
            if int(count) > 1
        ]
        result.duplicate_message_groups = len(duplicate_message_ids)

        for raw_message_id in duplicate_message_ids:
            assets = (
                session.query(MediaAsset)
                .filter(MediaAsset.raw_message_id == raw_message_id)
                .order_by(MediaAsset.id.asc())
                .all()
            )
            if len(assets) <= 1:
                continue
            result.scanned_assets += len(assets)
            keep_asset = max(assets, key=lambda asset: _asset_keep_score(asset, root))
            result.kept_assets += 1
            for asset in assets:
                if asset.id == keep_asset.id:
                    continue
                result.deleted_assets += 1
                if not dry_run:
                    session.delete(asset)

        if not dry_run:
            session.commit()

    return result


def _asset_keep_score(asset: MediaAsset, media_root: Path) -> tuple[int, int, int, int]:
    has_ocr = 1 if (asset.ocr_text or "").strip() else 0
    has_path = 1 if (asset.local_path or "").strip() else 0
    path_exists = 0
    resolved_path = resolve_media_path(asset.local_path, media_root=media_root)
    if resolved_path is not None and resolved_path.exists():
        path_exists = 1
    return (has_ocr, path_exists, has_path, -int(asset.id or 0))
