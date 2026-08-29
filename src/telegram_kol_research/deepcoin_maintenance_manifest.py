"""Strict, action-specific authorization manifests for Deepcoin maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from telegram_kol_research.reviewed_pending_entry_cancel import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)


_MAX_FILE_SIZE = 8 * 1024
_MAX_AUTHORIZATION_AGE = timedelta(minutes=15)
_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMON_KEYS = frozenset(
    {
        "schema_version",
        "action",
        "action_id",
        "issued_at",
        "expires_at",
        "candidate_commit",
        "release_manifest_sha256",
        "expected_fingerprint",
    }
)
_ACTION_KEYS = {
    "seed-entry-authority": frozenset({"database_path", "backup_path"}),
    "drain-one": frozenset(
        {"database_path", "target_order_id", "evidence_sha256"}
    ),
    "bootstrap-control": frozenset(
        {
            "database_path",
            "candidate_release_path",
            "rollback_release_path",
            "unit_manifest_sha256",
        }
    ),
}


class ManifestRefused(ValueError):
    """The authorization file is not an exact, fresh, safe manifest."""


class MaintenanceAction(str, Enum):
    SEED_ENTRY_AUTHORITY = "seed-entry-authority"
    DRAIN_ONE = "drain-one"
    BOOTSTRAP_CONTROL = "bootstrap-control"


@dataclass(frozen=True, slots=True)
class MaintenanceManifest:
    schema_version: int
    action: MaintenanceAction
    action_id: str
    issued_at: datetime
    expires_at: datetime
    candidate_commit: str
    release_manifest_sha256: str
    expected_fingerprint: str
    database_path: Path
    file_sha256: str
    backup_path: Path | None = None
    target_order_id: str | None = None
    evidence_sha256: str | None = None
    candidate_release_path: Path | None = None
    rollback_release_path: Path | None = None
    unit_manifest_sha256: str | None = None


def load_maintenance_manifest(
    path: Path,
    *,
    expected_action: MaintenanceAction,
    now: datetime,
    expected_uid: int = 0,
) -> MaintenanceManifest:
    """Open without following links and parse one exact action document."""

    manifest_path = Path(path)
    raw = _read_owned_manifest(
        manifest_path,
        expected_uid=int(expected_uid),
    )
    payload = _strict_json_object(raw)
    action_value = payload.get("action")
    if action_value != expected_action.value:
        raise ManifestRefused("manifest action mismatch")
    expected_keys = _COMMON_KEYS | _ACTION_KEYS[expected_action.value]
    if frozenset(payload) != expected_keys:
        raise ManifestRefused("manifest keys are not exact")
    if payload.get("schema_version") != 1:
        raise ManifestRefused("manifest schema_version is invalid")

    action_id = _text(payload, "action_id")
    if not _ACTION_ID.fullmatch(action_id):
        raise ManifestRefused("manifest action_id is invalid")
    issued_at = _timestamp(payload, "issued_at")
    expires_at = _timestamp(payload, "expires_at")
    observed_at = _normalize_time(now)
    if (
        issued_at > observed_at
        or expires_at <= observed_at
        or expires_at <= issued_at
        or expires_at - issued_at > _MAX_AUTHORIZATION_AGE
    ):
        raise ManifestRefused("manifest expiry is invalid")

    candidate_commit = _hex(payload, "candidate_commit", _COMMIT)
    release_hash = _hex(
        payload,
        "release_manifest_sha256",
        _SHA256,
    )
    expected_fingerprint = _hex(
        payload,
        "expected_fingerprint",
        _SHA256,
    )
    database_path = _absolute_path(payload, "database_path")
    values: dict[str, Any] = {}
    if expected_action is MaintenanceAction.SEED_ENTRY_AUTHORITY:
        values["backup_path"] = _absolute_path(payload, "backup_path")
        if values["backup_path"] == database_path:
            raise ManifestRefused("manifest backup_path must be distinct")
    elif expected_action is MaintenanceAction.DRAIN_ONE:
        target = payload.get("target_order_id")
        canonical = {
            row.order_id for row in REVIEWED_PENDING_ENTRY_TARGETS
        }
        if type(target) is not str or target not in canonical:
            raise ManifestRefused("manifest target_order_id is invalid")
        values["target_order_id"] = target
        values["evidence_sha256"] = _hex(
            payload,
            "evidence_sha256",
            _SHA256,
        )
    else:
        candidate_path = _absolute_path(payload, "candidate_release_path")
        rollback_path = _absolute_path(payload, "rollback_release_path")
        if candidate_path == rollback_path:
            raise ManifestRefused("manifest release paths must be distinct")
        values.update(
            candidate_release_path=candidate_path,
            rollback_release_path=rollback_path,
            unit_manifest_sha256=_hex(
                payload,
                "unit_manifest_sha256",
                _SHA256,
            ),
        )
    return MaintenanceManifest(
        schema_version=1,
        action=expected_action,
        action_id=action_id,
        issued_at=issued_at,
        expires_at=expires_at,
        candidate_commit=candidate_commit,
        release_manifest_sha256=release_hash,
        expected_fingerprint=expected_fingerprint,
        database_path=database_path,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        **values,
    )


def _read_owned_manifest(path: Path, *, expected_uid: int) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ManifestRefused("manifest file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ManifestRefused("manifest must be one regular file")
    if before.st_uid != expected_uid:
        raise ManifestRefused("manifest owner is invalid")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ManifestRefused("manifest mode is invalid")
    if before.st_size <= 1 or before.st_size > _MAX_FILE_SIZE:
        raise ManifestRefused("manifest size is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManifestRefused("manifest must be one regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != expected_uid
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
        ):
            raise ManifestRefused("manifest file identity changed")
        raw = os.read(descriptor, _MAX_FILE_SIZE + 1)
        after = os.fstat(descriptor)
        if len(raw) != opened.st_size or len(raw) > _MAX_FILE_SIZE:
            raise ManifestRefused("manifest size is invalid")
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ManifestRefused("manifest file identity changed")
        return raw
    finally:
        os.close(descriptor)


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManifestRefused(f"manifest duplicated field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestRefused("manifest JSON is invalid") from exc
    if type(value) is not dict:
        raise ManifestRefused("manifest JSON must be an object")
    return value


def _text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str:
        raise ManifestRefused(f"manifest {field} is invalid")
    return value


def _hex(payload, field, pattern) -> str:
    value = _text(payload, field)
    if not pattern.fullmatch(value):
        raise ManifestRefused(f"manifest {field} is invalid")
    return value


def _timestamp(payload: dict[str, Any], field: str) -> datetime:
    try:
        return _normalize_time(datetime.fromisoformat(_text(payload, field)))
    except ValueError as exc:
        raise ManifestRefused(f"manifest {field} is invalid") from exc


def _normalize_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ManifestRefused("manifest observation time is invalid")
    if value.tzinfo is None:
        raise ManifestRefused("manifest timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _absolute_path(payload: dict[str, Any], field: str) -> Path:
    value = _text(payload, field)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(value) > 512:
        raise ManifestRefused(f"manifest {field} is invalid")
    return path
