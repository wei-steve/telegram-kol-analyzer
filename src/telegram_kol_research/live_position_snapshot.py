from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4


_TYPE_KEY = "__telegram_kol_snapshot_type__"


@dataclass(frozen=True)
class LivePositionSnapshot:
    payload: dict[str, Any]
    captured_at: datetime
    version: str
    last_error: str | None = None
    refreshing: bool = False


class LivePositionSnapshotStore:
    """Thread-safe persisted cache for display-only Deepcoin position data."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._snapshot: LivePositionSnapshot | None = None
        self._refreshing = False
        self._last_error: str | None = None
        self.last_load_error: str | None = None
        self._loaded_file_signature: tuple[int, int, int] | None = None
        self._load()

    def read(self) -> LivePositionSnapshot | None:
        with self._lock:
            self._reload_if_changed()
            if self._snapshot is None:
                return None
            return replace(
                self._snapshot,
                payload=copy.deepcopy(self._snapshot.payload),
                last_error=self._last_error,
                refreshing=self._refreshing,
            )

    def begin_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            return True

    def finish_success(
        self,
        payload: dict[str, Any],
        *,
        captured_at: datetime,
    ) -> LivePositionSnapshot:
        normalized_at = _normalize_datetime(captured_at)
        snapshot = LivePositionSnapshot(
            payload=copy.deepcopy(payload),
            captured_at=normalized_at,
            version=f"{normalized_at.isoformat()}-{uuid4().hex}",
        )
        with self._lock:
            try:
                self._persist(snapshot)
            finally:
                self._refreshing = False
            self._snapshot = snapshot
            self._last_error = None
            return replace(snapshot, payload=copy.deepcopy(snapshot.payload))

    def finish_failure(self, error: str) -> None:
        with self._lock:
            self._last_error = str(error)
            self._refreshing = False

    def _load(self) -> None:
        if not self.path.exists():
            self._loaded_file_signature = None
            return
        try:
            persisted = json.loads(self.path.read_text(encoding="utf-8"))
            payload = _decode_json_value(persisted["payload"])
            captured_at = _normalize_datetime(
                datetime.fromisoformat(str(persisted["captured_at"]))
            )
            version = str(persisted["version"])
            if not isinstance(payload, dict) or not version:
                raise ValueError("invalid live position snapshot")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.last_load_error = str(exc)
            self._loaded_file_signature = self._file_signature()
            return
        self._snapshot = LivePositionSnapshot(
            payload=payload,
            captured_at=captured_at,
            version=version,
        )
        self.last_load_error = None
        self._loaded_file_signature = self._file_signature()

    def _reload_if_changed(self) -> None:
        signature = self._file_signature()
        if signature != self._loaded_file_signature:
            self._load()

    def _file_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _persist(self, snapshot: LivePositionSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        body = json.dumps(
            {
                "schema_version": 1,
                "version": snapshot.version,
                "captured_at": snapshot.captured_at.isoformat(),
                "payload": _encode_json_value(snapshot.payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            temporary_path.write_text(body, encoding="utf-8")
            temporary_path.replace(self.path)
            self._loaded_file_signature = self._file_signature()
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {
            _TYPE_KEY: "datetime",
            "value": _normalize_datetime(value).isoformat(),
        }
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, dict):
        return {str(key): _encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported live position snapshot value: {type(value).__name__}")


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        value_type = value.get(_TYPE_KEY)
        if value_type == "datetime":
            return _normalize_datetime(datetime.fromisoformat(str(value["value"])))
        if value_type == "decimal":
            return Decimal(str(value["value"]))
        return {key: _decode_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    return value
