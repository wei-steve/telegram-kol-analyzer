"""Retention helpers for local Telegram media files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil

from sqlalchemy import exists, or_

from telegram_kol_research.models import (
    MediaAsset,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


@dataclass(slots=True)
class MediaCleanupResult:
    scanned_assets: int = 0
    eligible_assets: int = 0
    protected_assets: int = 0
    missing_files: int = 0
    deleted_files: int = 0
    freed_bytes: int = 0
    cleared_local_paths: int = 0
    dry_run: bool = True


@dataclass(slots=True)
class MediaCleanupCandidate:
    media_asset_id: int
    file_path: Path
    file_size: int
    reference_at: datetime


def cleanup_media_files(
    session_factory,
    *,
    media_root: str | Path = "data/media",
    retain_days: int = 14,
    max_media_dir_gb: float | None = None,
    min_free_disk_gb: float | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> MediaCleanupResult:
    """Delete old non-critical media files while preserving DB history.

    The cleanup treats downloaded media as a cache: message rows, OCR text, and
    recognition records remain in SQLite.  When a file is removed, the matching
    MediaAsset.local_path is cleared so the web UI falls back to the text label
    and OCR content instead of rendering a broken image.
    """

    if retain_days < 0:
        raise ValueError("retain_days must be >= 0")
    if max_media_dir_gb is not None and max_media_dir_gb < 0:
        raise ValueError("max_media_dir_gb must be >= 0")
    if min_free_disk_gb is not None and min_free_disk_gb < 0:
        raise ValueError("min_free_disk_gb must be >= 0")

    resolved_media_root = Path(media_root).resolve()
    resolved_media_root.mkdir(parents=True, exist_ok=True)
    effective_now = now or datetime.now(UTC)
    if effective_now.tzinfo is not None:
        effective_now = effective_now.astimezone(UTC).replace(tzinfo=None)
    cutoff = effective_now - timedelta(days=retain_days)

    result = MediaCleanupResult(dry_run=dry_run)
    candidates: list[MediaCleanupCandidate] = []

    with session_factory() as session:
        rows = (
            session.query(MediaAsset, RawMessage)
            .join(RawMessage, RawMessage.id == MediaAsset.raw_message_id)
            .filter(MediaAsset.local_path.is_not(None))
            .order_by(RawMessage.posted_at.asc(), MediaAsset.created_at.asc())
            .all()
        )
        result.scanned_assets = len(rows)

        protected_ids = _load_protected_media_asset_ids(session)
        for media_asset, raw_message in rows:
            if media_asset.id in protected_ids:
                result.protected_assets += 1
                continue

            reference_at = raw_message.posted_at or media_asset.created_at
            if reference_at is None:
                reference_at = datetime.min
            if reference_at.tzinfo is not None:
                reference_at = reference_at.astimezone(UTC).replace(tzinfo=None)
            if reference_at >= cutoff:
                continue

            file_path = resolve_media_path(
                media_asset.local_path,
                media_root=resolved_media_root,
            )
            if file_path is None:
                result.protected_assets += 1
                continue
            if not file_path.exists():
                result.missing_files += 1
                result.eligible_assets += 1
                if not dry_run:
                    media_asset.local_path = None
                    result.cleared_local_paths += 1
                continue
            if not file_path.is_file():
                result.protected_assets += 1
                continue

            candidates.append(
                MediaCleanupCandidate(
                    media_asset_id=media_asset.id,
                    file_path=file_path,
                    file_size=file_path.stat().st_size,
                    reference_at=reference_at,
                )
            )

        selected = _select_candidates_to_delete(
            candidates,
            media_root=resolved_media_root,
            max_media_dir_gb=max_media_dir_gb,
            min_free_disk_gb=min_free_disk_gb,
        )
        result.eligible_assets += len(selected)

        selected_ids = {candidate.media_asset_id for candidate in selected}
        assets_by_id = {
            asset.id: asset
            for asset in session.query(MediaAsset)
            .filter(MediaAsset.id.in_(selected_ids))
            .all()
        } if selected_ids else {}
        for candidate in selected:
            if not dry_run:
                try:
                    candidate.file_path.unlink()
                except FileNotFoundError:
                    result.missing_files += 1
                else:
                    result.deleted_files += 1
                    result.freed_bytes += candidate.file_size
                asset = assets_by_id.get(candidate.media_asset_id)
                if asset is not None:
                    asset.local_path = None
                    result.cleared_local_paths += 1
            else:
                result.deleted_files += 1
                result.freed_bytes += candidate.file_size

        if not dry_run:
            session.commit()

    if not dry_run:
        _prune_empty_dirs(resolved_media_root)

    return result


def resolve_media_path(local_path: str | None, *, media_root: str | Path) -> Path | None:
    if not local_path:
        return None
    media_root_path = Path(media_root).resolve()
    normalized = local_path.replace("\\", "/")
    prefix = "data/media/"
    while normalized.startswith(prefix):
        normalized = normalized[len(prefix):]
    path = Path(normalized)
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (media_root_path / path).resolve()
    try:
        candidate.relative_to(media_root_path)
    except ValueError:
        return None
    return candidate


def _load_protected_media_asset_ids(session) -> set[int]:
    lifecycle_exists = exists().where(
        StrategyLifecycle.chat_id == RawMessage.chat_id,
        or_(
            StrategyLifecycle.message_id == RawMessage.message_id,
            StrategyLifecycle.entry_signal_message_id == RawMessage.message_id,
            StrategyLifecycle.exit_signal_message_id == RawMessage.message_id,
            StrategyLifecycle.management_signal_message_id == RawMessage.message_id,
        ),
    )
    rows = (
        session.query(MediaAsset.id)
        .join(RawMessage, RawMessage.id == MediaAsset.raw_message_id)
        .filter(
            or_(
                exists().where(SignalCandidate.raw_message_id == RawMessage.id),
                lifecycle_exists,
            )
        )
        .all()
    )
    return {int(row[0]) for row in rows}


def _select_candidates_to_delete(
    candidates: list[MediaCleanupCandidate],
    *,
    media_root: Path,
    max_media_dir_gb: float | None,
    min_free_disk_gb: float | None,
) -> list[MediaCleanupCandidate]:
    ordered = sorted(candidates, key=lambda item: (item.reference_at, item.media_asset_id))
    if max_media_dir_gb is None and min_free_disk_gb is None:
        return ordered

    current_media_size = _directory_size(media_root)
    disk_usage = shutil.disk_usage(media_root)
    projected_free = disk_usage.free
    max_media_bytes = (
        int(max_media_dir_gb * 1024 * 1024 * 1024)
        if max_media_dir_gb is not None
        else None
    )
    min_free_bytes = (
        int(min_free_disk_gb * 1024 * 1024 * 1024)
        if min_free_disk_gb is not None
        else None
    )

    media_limit_satisfied = (
        max_media_bytes is None or current_media_size <= max_media_bytes
    )
    free_limit_satisfied = (
        min_free_bytes is None or projected_free >= min_free_bytes
    )
    if media_limit_satisfied and free_limit_satisfied:
        return ordered

    selected: list[MediaCleanupCandidate] = []
    for candidate in ordered:
        selected.append(candidate)
        current_media_size -= candidate.file_size
        projected_free += candidate.file_size
        media_limit_satisfied = (
            max_media_bytes is None or current_media_size <= max_media_bytes
        )
        free_limit_satisfied = (
            min_free_bytes is None or projected_free >= min_free_bytes
        )
        if media_limit_satisfied and free_limit_satisfied:
            break

    selected_ids = {item.media_asset_id for item in selected}
    return [
        candidate
        for candidate in ordered
        if candidate.media_asset_id in selected_ids
    ]


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _prune_empty_dirs(media_root: Path) -> None:
    if not media_root.exists():
        return
    for path in sorted(
        (item for item in media_root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
