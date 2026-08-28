"""Persistent fail-closed guard for bounded runtime maintenance windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Protocol
import uuid


MAINTENANCE_UNITS: tuple[str, ...] = (
    "telegram-kol-web.service",
    "telegram-kol-ingest.service",
    "telegram-kol-worker.service",
    "telegram-kol-monitor.timer",
    "telegram-kol-monitor.service",
)
_SCHEMA_VERSION = 1
_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GuardError(RuntimeError):
    """The runtime could not be proven safely inhibited or restored."""


@dataclass(frozen=True, slots=True)
class UnitPreimage:
    unit: str
    enabled_state: str
    active_state: str
    masked: bool


@dataclass(frozen=True, slots=True)
class GuardReceipt:
    schema_version: int
    action_id: str
    entered_at: datetime
    safe_to_restore: bool
    blocked_reason: str | None
    units: tuple[UnitPreimage, ...]
    fingerprint: str


class RuntimeControlAdapter(Protocol):
    def inspect_unit(self, unit: str) -> UnitPreimage: ...

    def mask_unit(self, unit: str) -> None: ...

    def stop_unit(self, unit: str) -> None: ...

    def unmask_unit(self, unit: str) -> None: ...

    def start_unit(self, unit: str) -> None: ...

    def restore_enabled_state(self, unit: str, state: str) -> None: ...

    def is_masked(self, unit: str) -> bool: ...

    def main_pid(self, unit: str) -> int: ...

    def cgroup_pids(self, unit: str) -> tuple[int, ...]: ...

    def matching_processes(self) -> tuple[int, ...]: ...


class MaintenanceRuntimeGuard:
    """Own the host maintenance lock and exact systemd service preimage."""

    def __init__(
        self,
        *,
        runtime: RuntimeControlAdapter,
        receipt_path: Path = Path(
            "/var/lib/telegram-kol-maintenance/guard.json"
        ),
        lock_path: Path = Path("/run/lock/telegram-kol-maintenance.lock"),
        expected_uid: int = 0,
    ) -> None:
        self.runtime = runtime
        self.receipt_path = Path(receipt_path)
        self.lock_path = Path(lock_path)
        self.expected_uid = int(expected_uid)
        self._lock_descriptor: int | None = None

    def enter(self, *, action_id: str) -> GuardReceipt:
        clean_action_id = _action_id(action_id)
        self._acquire_lock()
        if self.receipt_path.exists():
            raise GuardError("maintenance_receipt_exists")
        units = tuple(
            self.runtime.inspect_unit(unit) for unit in MAINTENANCE_UNITS
        )
        if tuple(row.unit for row in units) != MAINTENANCE_UNITS:
            raise GuardError("maintenance_unit_preimage_invalid")
        receipt = _new_receipt(
            action_id=clean_action_id,
            units=units,
            entered_at=datetime.now(UTC),
        )
        self._publish(receipt)
        try:
            for unit in MAINTENANCE_UNITS:
                self.runtime.mask_unit(unit)
            for unit in MAINTENANCE_UNITS:
                self.runtime.stop_unit(unit)
            self.prove_quiescent()
        except Exception:
            self.block(reason_code="maintenance_guard_enter_failed")
            raise
        return self._load_receipt()

    def prove_quiescent(self) -> None:
        self._require_lock()
        for unit in MAINTENANCE_UNITS:
            if self.runtime.main_pid(unit) != 0:
                raise GuardError("maintenance_unit_main_pid_present")
            if self.runtime.cgroup_pids(unit):
                raise GuardError("maintenance_unit_cgroup_not_empty")
        first = self.runtime.matching_processes()
        if first:
            raise GuardError("maintenance_matching_process_present")
        second = self.runtime.matching_processes()
        if second:
            raise GuardError("maintenance_matching_process_present")

    def mark_safe_to_restore(
        self,
        *,
        expected_fingerprint: str,
    ) -> GuardReceipt:
        self._require_lock()
        receipt = self._load_receipt()
        if receipt.fingerprint != _fingerprint(expected_fingerprint):
            raise GuardError("maintenance_receipt_fingerprint_mismatch")
        if receipt.blocked_reason is not None:
            raise GuardError("maintenance_restore_blocked")
        updated = _with_receipt_state(
            receipt,
            safe_to_restore=True,
            blocked_reason=None,
        )
        self._publish(updated)
        return updated

    def restore(self, *, expected_fingerprint: str) -> None:
        self._require_lock()
        receipt = self._load_receipt()
        if receipt.fingerprint != _fingerprint(expected_fingerprint):
            raise GuardError("maintenance_receipt_fingerprint_mismatch")
        if not receipt.safe_to_restore or receipt.blocked_reason is not None:
            raise GuardError("maintenance_restore_not_safe")
        for row in receipt.units:
            if not row.masked:
                self.runtime.unmask_unit(row.unit)
            self.runtime.restore_enabled_state(row.unit, row.enabled_state)
            if row.active_state == "active":
                self.runtime.start_unit(row.unit)
        self._remove_receipt()

    def block(self, *, reason_code: str) -> GuardReceipt:
        self._require_lock()
        clean_reason = _reason_code(reason_code)
        receipt = self._load_receipt()
        updated = _with_receipt_state(
            receipt,
            safe_to_restore=False,
            blocked_reason=clean_reason,
        )
        self._publish(updated)
        return updated

    def reconcile_after_restart(self) -> GuardReceipt:
        self._acquire_lock()
        receipt = self._load_receipt()
        for unit in MAINTENANCE_UNITS:
            self.runtime.mask_unit(unit)
        for unit in MAINTENANCE_UNITS:
            self.runtime.stop_unit(unit)
        self.prove_quiescent()
        if receipt.safe_to_restore or receipt.blocked_reason is None:
            return self.block(reason_code="maintenance_recovery_required")
        return receipt

    def close(self) -> None:
        descriptor = self._lock_descriptor
        self._lock_descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _acquire_lock(self) -> None:
        if self._lock_descriptor is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise GuardError("maintenance_host_lock_unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                os.close(descriptor)
            except UnboundLocalError:
                pass
            raise GuardError("maintenance_host_lock_busy") from exc
        except Exception:
            try:
                os.close(descriptor)
            except UnboundLocalError:
                pass
            raise
        self._lock_descriptor = descriptor

    def _require_lock(self) -> None:
        if self._lock_descriptor is None:
            raise GuardError("maintenance_host_lock_required")

    def _load_receipt(self) -> GuardReceipt:
        try:
            metadata = self.receipt_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise GuardError("maintenance_receipt_unsafe")
            encoded = self.receipt_path.read_bytes()
            if len(encoded) > 64 * 1024:
                raise GuardError("maintenance_receipt_invalid")
            payload = json.loads(encoded.decode("utf-8"))
            return _parse_receipt(payload)
        except GuardError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GuardError("maintenance_receipt_invalid") from exc

    def _publish(self, receipt: GuardReceipt) -> None:
        directory = self.receipt_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = _receipt_payload(receipt)
        encoded = _canonical_json(payload)
        temporary = directory / f".{self.receipt_path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.receipt_path)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise GuardError("maintenance_receipt_publish_failed") from exc

    def _remove_receipt(self) -> None:
        try:
            self.receipt_path.unlink()
            directory_descriptor = os.open(
                self.receipt_path.parent,
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise GuardError("maintenance_receipt_remove_failed") from exc

    def __enter__(self) -> "MaintenanceRuntimeGuard":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _new_receipt(
    *,
    action_id: str,
    units: tuple[UnitPreimage, ...],
    entered_at: datetime,
) -> GuardReceipt:
    base = GuardReceipt(
        schema_version=_SCHEMA_VERSION,
        action_id=action_id,
        entered_at=entered_at.astimezone(UTC),
        safe_to_restore=False,
        blocked_reason=None,
        units=units,
        fingerprint="0" * 64,
    )
    return _with_receipt_state(
        base,
        safe_to_restore=False,
        blocked_reason=None,
    )


def _with_receipt_state(
    receipt: GuardReceipt,
    *,
    safe_to_restore: bool,
    blocked_reason: str | None,
) -> GuardReceipt:
    without_fingerprint = {
        "action_id": receipt.action_id,
        "blocked_reason": blocked_reason,
        "entered_at": receipt.entered_at.isoformat(),
        "safe_to_restore": safe_to_restore,
        "schema_version": receipt.schema_version,
        "units": [_unit_payload(row) for row in receipt.units],
    }
    fingerprint = hashlib.sha256(
        _canonical_json(without_fingerprint)
    ).hexdigest()
    return GuardReceipt(
        schema_version=receipt.schema_version,
        action_id=receipt.action_id,
        entered_at=receipt.entered_at,
        safe_to_restore=safe_to_restore,
        blocked_reason=blocked_reason,
        units=receipt.units,
        fingerprint=fingerprint,
    )


def _receipt_payload(receipt: GuardReceipt) -> dict[str, object]:
    return {
        "action_id": receipt.action_id,
        "blocked_reason": receipt.blocked_reason,
        "entered_at": receipt.entered_at.isoformat(),
        "fingerprint": receipt.fingerprint,
        "safe_to_restore": receipt.safe_to_restore,
        "schema_version": receipt.schema_version,
        "units": [_unit_payload(row) for row in receipt.units],
    }


def _unit_payload(row: UnitPreimage) -> dict[str, object]:
    return {
        "active_state": row.active_state,
        "enabled_state": row.enabled_state,
        "masked": row.masked,
        "unit": row.unit,
    }


def _parse_receipt(payload: object) -> GuardReceipt:
    if not isinstance(payload, dict) or set(payload) != {
        "action_id",
        "blocked_reason",
        "entered_at",
        "fingerprint",
        "safe_to_restore",
        "schema_version",
        "units",
    }:
        raise GuardError("maintenance_receipt_invalid")
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise GuardError("maintenance_receipt_invalid")
    action_id = _action_id(payload["action_id"])
    entered_at = _timestamp(payload["entered_at"])
    if type(payload["safe_to_restore"]) is not bool:
        raise GuardError("maintenance_receipt_invalid")
    blocked_reason = payload["blocked_reason"]
    if blocked_reason is not None:
        blocked_reason = _reason_code(blocked_reason)
    rows = payload["units"]
    if not isinstance(rows, list) or len(rows) != len(MAINTENANCE_UNITS):
        raise GuardError("maintenance_receipt_invalid")
    units = tuple(_parse_unit(row) for row in rows)
    if tuple(row.unit for row in units) != MAINTENANCE_UNITS:
        raise GuardError("maintenance_receipt_invalid")
    fingerprint = _fingerprint(payload["fingerprint"])
    receipt = GuardReceipt(
        schema_version=_SCHEMA_VERSION,
        action_id=action_id,
        entered_at=entered_at,
        safe_to_restore=payload["safe_to_restore"],
        blocked_reason=blocked_reason,
        units=units,
        fingerprint=fingerprint,
    )
    expected = _with_receipt_state(
        receipt,
        safe_to_restore=receipt.safe_to_restore,
        blocked_reason=receipt.blocked_reason,
    )
    if expected.fingerprint != fingerprint:
        raise GuardError("maintenance_receipt_invalid")
    return receipt


def _parse_unit(payload: object) -> UnitPreimage:
    if not isinstance(payload, dict) or set(payload) != {
        "active_state",
        "enabled_state",
        "masked",
        "unit",
    }:
        raise GuardError("maintenance_receipt_invalid")
    unit = str(payload["unit"] or "")
    enabled_state = str(payload["enabled_state"] or "")
    active_state = str(payload["active_state"] or "")
    if (
        unit not in MAINTENANCE_UNITS
        or not enabled_state
        or len(enabled_state) > 32
        or not active_state
        or len(active_state) > 32
        or type(payload["masked"]) is not bool
    ):
        raise GuardError("maintenance_receipt_invalid")
    return UnitPreimage(
        unit=unit,
        enabled_state=enabled_state,
        active_state=active_state,
        masked=payload["masked"],
    )


def _action_id(value: object) -> str:
    clean = str(value or "").strip()
    if not _ACTION_ID.fullmatch(clean):
        raise GuardError("maintenance_action_id_invalid")
    return clean


def _reason_code(value: object) -> str:
    clean = str(value or "").strip()
    if not _REASON_CODE.fullmatch(clean):
        raise GuardError("maintenance_reason_code_invalid")
    return clean


def _fingerprint(value: object) -> str:
    clean = str(value or "").strip()
    if not _SHA256.fullmatch(clean):
        raise GuardError("maintenance_receipt_fingerprint_invalid")
    return clean


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise GuardError("maintenance_receipt_invalid") from exc
    if parsed.tzinfo is None:
        raise GuardError("maintenance_receipt_invalid")
    return parsed.astimezone(UTC)


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
