"""Fail-closed, non-executing MiMo v1/v2 isolated replay helpers."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path


MAX_REPLAY_MESSAGES = 200
_POSITIVE_MESSAGE_ID = re.compile(r"^[1-9][0-9]*$")


class MimoV2ReplayInputError(ValueError):
    """Raised before replay when an isolation or bounded-input rule fails."""


@dataclass(frozen=True, slots=True)
class MimoV2ReplayInputs:
    source_database: Path
    message_id_file: Path
    media_root: Path
    artifact_dir: Path
    raw_message_ids: tuple[int, ...]


def load_replay_message_ids(
    path: str | Path,
    *,
    max_messages: int,
) -> tuple[int, ...]:
    """Load a stable, explicit and bounded raw-message ID allowlist."""

    if (
        isinstance(max_messages, bool)
        or not isinstance(max_messages, int)
        or not 1 <= max_messages <= MAX_REPLAY_MESSAGES
    ):
        raise MimoV2ReplayInputError("max_messages_invalid")
    source = Path(path)
    if _path_has_symlink_component(source) or not source.is_file():
        raise MimoV2ReplayInputError("message_id_file_invalid")
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MimoV2ReplayInputError("message_id_file_invalid") from exc
    selected: list[int] = []
    seen: set[int] = set()
    for raw_line in lines:
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        if _POSITIVE_MESSAGE_ID.fullmatch(value) is None:
            raise MimoV2ReplayInputError("message_id_invalid")
        raw_message_id = int(value)
        if raw_message_id in seen:
            continue
        if len(selected) >= max_messages:
            raise MimoV2ReplayInputError("message_id_limit_exceeded")
        selected.append(raw_message_id)
        seen.add(raw_message_id)
    if not selected:
        raise MimoV2ReplayInputError("message_id_list_empty")
    return tuple(selected)


def validate_replay_inputs(
    *,
    source_database: str | Path,
    message_id_file: str | Path,
    media_root: str | Path,
    artifact_dir: str | Path,
    max_messages: int,
) -> MimoV2ReplayInputs:
    """Validate and freeze replay boundaries before any model or database call."""

    source = _validated_regular_file(
        source_database,
        reason="source_database_invalid",
    )
    id_file = _validated_regular_file(
        message_id_file,
        reason="message_id_file_invalid",
    )
    media = _validated_directory(media_root, reason="media_root_invalid")
    artifacts = _prepare_artifact_directory(artifact_dir)
    raw_message_ids = load_replay_message_ids(
        id_file,
        max_messages=max_messages,
    )
    return MimoV2ReplayInputs(
        source_database=source,
        message_id_file=id_file,
        media_root=media,
        artifact_dir=artifacts,
        raw_message_ids=raw_message_ids,
    )


def create_read_only_replay_snapshot(
    source_database: str | Path,
    destination: str | Path,
) -> Path:
    """Copy one consistent SQLite snapshot without opening source for writes."""

    source = _validated_regular_file(
        source_database,
        reason="source_database_invalid",
    )
    target = Path(destination)
    parent = target.parent
    if (
        target.exists()
        or _path_has_symlink_component(target)
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
    ):
        raise MimoV2ReplayInputError("snapshot_destination_invalid")
    try:
        has_live_sidecars = any(
            source.with_name(source.name + suffix).exists()
            for suffix in ("-wal", "-shm")
        )
        source_uri = source.as_uri() + (
            "?mode=ro" if has_live_sidecars else "?mode=ro&immutable=1"
        )
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with sqlite3.connect(target) as target_connection:
                source_connection.backup(
                    target_connection,
                    pages=1024,
                    sleep=0.01,
                )
                target_connection.commit()
        target.chmod(0o600)
        return target.resolve(strict=True)
    except (OSError, sqlite3.Error) as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise MimoV2ReplayInputError("sqlite_online_backup_failed") from exc


def _validated_regular_file(path: str | Path, *, reason: str) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate) or not candidate.is_file():
        raise MimoV2ReplayInputError(reason)
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise MimoV2ReplayInputError(reason) from exc


def _validated_directory(path: str | Path, *, reason: str) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate) or not candidate.is_dir():
        raise MimoV2ReplayInputError(reason)
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise MimoV2ReplayInputError(reason) from exc


def _prepare_artifact_directory(path: str | Path) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate):
        raise MimoV2ReplayInputError("artifact_dir_invalid")
    try:
        if candidate.exists():
            if not candidate.is_dir():
                raise MimoV2ReplayInputError("artifact_dir_invalid")
            if any(candidate.iterdir()):
                raise MimoV2ReplayInputError("artifact_dir_not_empty")
        else:
            candidate.mkdir(mode=0o700)
        candidate.chmod(0o700)
        return candidate.resolve(strict=True)
    except MimoV2ReplayInputError:
        raise
    except OSError as exc:
        raise MimoV2ReplayInputError("artifact_dir_invalid") from exc


def _path_has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False
