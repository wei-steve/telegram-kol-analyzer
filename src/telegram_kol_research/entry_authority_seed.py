"""One-time, fail-closed bootstrap for entry exchange authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal, Protocol

from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
)


_AUTHORITY_SCHEMA_VERSION = 2
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CRITICAL_TABLES: tuple[str, ...] = (
    "execution_bindings",
    "execution_order_legs",
    "repair_confirmation_tokens",
    "execution_events",
    "strategy_lifecycles",
)


class SeedPlanRefused(RuntimeError):
    """The immutable seed preconditions were not proven."""


class SeedGuard(Protocol):
    def prove_quiescent(self) -> None: ...

    def block(self, *, reason_code: str) -> object: ...


@dataclass(frozen=True, slots=True)
class SeedPlan:
    database_path: Path
    backup_path: Path
    observed_at: datetime
    database_device: int
    database_inode: int
    schema_version: int
    authority_state: Literal["absent"]
    quick_check: tuple[str, ...]
    foreign_key_violation_count: int
    affected_counts: tuple[tuple[str, int], ...]
    critical_table_counts: tuple[tuple[str, int], ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SeedResult:
    status: Literal["seeded", "restored", "blocked"]
    plan_fingerprint: str
    backup_path: Path
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class _DatabaseSnapshot:
    database_device: int
    database_inode: int
    schema_version: int
    quick_check: tuple[str, ...]
    foreign_key_violation_count: int
    affected_counts: tuple[tuple[str, int], ...]
    critical_table_counts: tuple[tuple[str, int], ...]
    authority_values: tuple[str, ...]


def build_entry_authority_seed_plan(
    database_path: Path,
    *,
    now: datetime,
) -> SeedPlan:
    """Inspect an existing database without writing and plan one exact seed."""

    observed_at = _timestamp(now)
    resolved_database = _safe_existing_database(database_path)
    backup_path = _planned_backup_path(resolved_database, observed_at)
    _require_unused_backup_destination(backup_path)
    snapshot = _inspect_database(resolved_database)
    _require_healthy_absent_snapshot(snapshot)
    payload = _plan_payload(
        database_path=resolved_database,
        backup_path=backup_path,
        observed_at=observed_at,
        snapshot=snapshot,
    )
    return SeedPlan(
        database_path=resolved_database,
        backup_path=backup_path,
        observed_at=observed_at,
        database_device=snapshot.database_device,
        database_inode=snapshot.database_inode,
        schema_version=snapshot.schema_version,
        authority_state="absent",
        quick_check=snapshot.quick_check,
        foreign_key_violation_count=snapshot.foreign_key_violation_count,
        affected_counts=snapshot.affected_counts,
        critical_table_counts=snapshot.critical_table_counts,
        fingerprint=_sha256(payload),
    )


def apply_entry_authority_seed_plan(
    database_path: Path,
    *,
    backup_path: Path,
    expected_fingerprint: str,
    guard: SeedGuard,
    now: datetime,
) -> SeedResult:
    """Back up, seed exactly one row, and restore on known postcheck failure."""

    fingerprint = _expected_fingerprint(expected_fingerprint)
    observed_at = _timestamp(now)
    resolved_database = _safe_existing_database(database_path)
    guard.prove_quiescent()
    plan = build_entry_authority_seed_plan(resolved_database, now=observed_at)
    if plan.fingerprint != fingerprint:
        raise SeedPlanRefused("entry_authority_seed_plan_drift")
    resolved_backup = Path(backup_path).expanduser().absolute()
    if resolved_backup != plan.backup_path:
        raise SeedPlanRefused("entry_authority_seed_backup_destination_mismatch")

    connection = sqlite3.connect(resolved_database, timeout=30, isolation_level=None)
    commit_attempted = False
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        locked_snapshot = _inspect_connection(connection, resolved_database)
        if not _snapshot_matches_plan(locked_snapshot, plan):
            connection.rollback()
            raise SeedPlanRefused("entry_authority_seed_locked_recheck_failed")

        # A RESERVED writer lock blocks every competing writer. The backup uses
        # a separate read connection, so it captures the exact locked preimage.
        _sqlite_backup(resolved_database, resolved_backup)
        backup_snapshot = _inspect_database(resolved_backup)
        if not _backup_matches_preimage(backup_snapshot, locked_snapshot):
            connection.rollback()
            raise SeedPlanRefused("entry_authority_seed_backup_verification_failed")

        connection.execute(
            "INSERT INTO trading_settings (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (
                ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                _canonical_json(
                    {
                        "generation": 0,
                        "released_at": observed_at.isoformat(),
                        "schema_version": _AUTHORITY_SCHEMA_VERSION,
                        "state": "idle",
                    }
                ),
                observed_at.isoformat(),
            ),
        )
        commit_attempted = True
        connection.commit()
    except SeedPlanRefused:
        if connection.in_transaction:
            connection.rollback()
        raise
    except Exception as exc:
        rollback_succeeded = True
        if connection.in_transaction:
            try:
                connection.rollback()
            except Exception:
                rollback_succeeded = False
        if not commit_attempted and rollback_succeeded:
            raise SeedPlanRefused(
                "entry_authority_seed_precommit_failed"
            ) from exc
        return _blocked_result(
            guard=guard,
            fingerprint=fingerprint,
            backup_path=resolved_backup,
            reason_code="entry_authority_seed_write_unknown",
        )
    finally:
        connection.close()

    try:
        post_snapshot = _inspect_database(resolved_database)
        post_valid = _post_seed_snapshot_is_valid(
            post_snapshot,
            plan,
            observed_at=observed_at,
        )
    except Exception:
        post_valid = False
    if post_valid:
        return SeedResult(
            status="seeded",
            plan_fingerprint=fingerprint,
            backup_path=resolved_backup,
        )

    try:
        guard.prove_quiescent()
        _restore_database(resolved_database, resolved_backup)
        restored_snapshot = _inspect_database(resolved_database)
        if not _backup_matches_preimage(restored_snapshot, locked_snapshot):
            raise RuntimeError("restored database does not match verified preimage")
    except Exception:
        return _blocked_result(
            guard=guard,
            fingerprint=fingerprint,
            backup_path=resolved_backup,
            reason_code="entry_authority_seed_restore_unknown",
        )
    return SeedResult(
        status="restored",
        plan_fingerprint=fingerprint,
        backup_path=resolved_backup,
        reason_code="entry_authority_seed_postcheck_failed",
    )


def _inspect_database(database_path: Path) -> _DatabaseSnapshot:
    resolved = _safe_existing_database(database_path)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        return _inspect_connection(connection, resolved)
    finally:
        connection.close()


def _inspect_connection(
    connection: sqlite3.Connection,
    database_path: Path,
) -> _DatabaseSnapshot:
    metadata = _safe_database_stat(database_path)
    required_tables = {"trading_settings", *_CRITICAL_TABLES}
    present_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not required_tables.issubset(present_tables):
        raise SeedPlanRefused("entry_authority_seed_critical_table_missing")
    quick_check = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
    foreign_key_violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    trading_settings_count = _count_table(connection, "trading_settings")
    authority_values = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT value_json FROM trading_settings WHERE key = ? ORDER BY id",
            (ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,),
        ).fetchall()
    )
    return _DatabaseSnapshot(
        database_device=int(metadata.st_dev),
        database_inode=int(metadata.st_ino),
        schema_version=int(connection.execute("PRAGMA schema_version").fetchone()[0]),
        quick_check=quick_check,
        foreign_key_violation_count=len(foreign_key_violations),
        affected_counts=(
            ("authority_rows", len(authority_values)),
            ("trading_settings", trading_settings_count),
        ),
        critical_table_counts=tuple(
            (table, _count_table(connection, table)) for table in _CRITICAL_TABLES
        ),
        authority_values=authority_values,
    )


def _post_seed_snapshot_is_valid(
    snapshot: _DatabaseSnapshot,
    plan: SeedPlan,
    *,
    observed_at: datetime,
) -> bool:
    expected_document = _canonical_json(
        {
            "generation": 0,
            "released_at": observed_at.isoformat(),
            "schema_version": _AUTHORITY_SCHEMA_VERSION,
            "state": "idle",
        }
    )
    before_affected = dict(plan.affected_counts)
    after_affected = dict(snapshot.affected_counts)
    return (
        snapshot.database_device == plan.database_device
        and snapshot.database_inode == plan.database_inode
        and snapshot.schema_version == plan.schema_version
        and snapshot.quick_check == ("ok",)
        and snapshot.foreign_key_violation_count == 0
        and after_affected.get("authority_rows") == 1
        and after_affected.get("trading_settings")
        == before_affected.get("trading_settings", -1) + 1
        and snapshot.critical_table_counts == plan.critical_table_counts
        and snapshot.authority_values == (expected_document,)
    )


def _sqlite_backup(source: Path, destination: Path) -> None:
    _require_unused_backup_destination(destination)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    os.close(descriptor)
    source_connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)
    _fsync_path(destination)


def _restore_database(database_path: Path, backup_path: Path) -> None:
    source_connection = sqlite3.connect(
        f"{backup_path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    destination_connection = sqlite3.connect(database_path, timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    _fsync_path(database_path)


def _snapshot_matches_plan(snapshot: _DatabaseSnapshot, plan: SeedPlan) -> bool:
    return (
        snapshot.database_device == plan.database_device
        and snapshot.database_inode == plan.database_inode
        and snapshot.schema_version == plan.schema_version
        and snapshot.quick_check == plan.quick_check == ("ok",)
        and snapshot.foreign_key_violation_count
        == plan.foreign_key_violation_count
        == 0
        and snapshot.affected_counts == plan.affected_counts
        and snapshot.critical_table_counts == plan.critical_table_counts
        and snapshot.authority_values == ()
    )


def _backup_matches_preimage(
    snapshot: _DatabaseSnapshot,
    preimage: _DatabaseSnapshot,
) -> bool:
    # SQLite assigns a destination-local schema cookie while the backup API
    # replaces pages. Validate the schema cookie on the live preimage/plan;
    # validate backup and restore copies by integrity, rows, and table counts.
    return (
        snapshot.quick_check == preimage.quick_check == ("ok",)
        and snapshot.foreign_key_violation_count
        == preimage.foreign_key_violation_count
        == 0
        and snapshot.affected_counts == preimage.affected_counts
        and snapshot.critical_table_counts == preimage.critical_table_counts
        and snapshot.authority_values == preimage.authority_values == ()
    )


def _require_healthy_absent_snapshot(snapshot: _DatabaseSnapshot) -> None:
    if snapshot.quick_check != ("ok",):
        raise SeedPlanRefused("entry_authority_seed_quick_check_failed")
    if snapshot.foreign_key_violation_count != 0:
        raise SeedPlanRefused("entry_authority_seed_foreign_key_check_failed")
    if snapshot.authority_values:
        raise SeedPlanRefused("entry_authority_seed_row_exists")


def _plan_payload(
    *,
    database_path: Path,
    backup_path: Path,
    observed_at: datetime,
    snapshot: _DatabaseSnapshot,
) -> dict[str, object]:
    return {
        "affected_counts": dict(snapshot.affected_counts),
        "authority_state": "absent",
        "backup_path": str(backup_path),
        "critical_table_counts": dict(snapshot.critical_table_counts),
        "database_device": snapshot.database_device,
        "database_inode": snapshot.database_inode,
        "database_path": str(database_path),
        "foreign_key_violation_count": snapshot.foreign_key_violation_count,
        "observed_at": observed_at.isoformat(),
        "quick_check": list(snapshot.quick_check),
        "schema_version": snapshot.schema_version,
    }


def _planned_backup_path(database_path: Path, observed_at: datetime) -> Path:
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    return database_path.with_name(
        f"{database_path.name}.entry-authority-seed-{stamp}.backup.sqlite3"
    )


def _require_unused_backup_destination(backup_path: Path) -> None:
    parent = backup_path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise SeedPlanRefused("entry_authority_seed_backup_parent_invalid")
    if backup_path.parent != parent:
        raise SeedPlanRefused("entry_authority_seed_backup_parent_invalid")
    if backup_path.exists() or backup_path.is_symlink():
        raise SeedPlanRefused("entry_authority_seed_backup_exists")


def _safe_existing_database(database_path: Path) -> Path:
    candidate = Path(database_path).expanduser().absolute()
    metadata = _safe_database_stat(candidate)
    if candidate.resolve(strict=True) != candidate:
        raise SeedPlanRefused("entry_authority_seed_database_path_unsafe")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SeedPlanRefused("entry_authority_seed_database_path_unsafe")
    return candidate


def _safe_database_stat(database_path: Path) -> os.stat_result:
    try:
        metadata = database_path.lstat()
    except OSError as exc:
        raise SeedPlanRefused("entry_authority_seed_database_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SeedPlanRefused("entry_authority_seed_database_path_unsafe")
    return metadata


def _count_table(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"trading_settings", *_CRITICAL_TABLES}:
        raise ValueError("unsupported seed count table")
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _blocked_result(
    *,
    guard: SeedGuard,
    fingerprint: str,
    backup_path: Path,
    reason_code: str,
) -> SeedResult:
    try:
        guard.block(reason_code=reason_code)
    except Exception as exc:
        raise RuntimeError(reason_code) from exc
    return SeedResult(
        status="blocked",
        plan_fingerprint=fingerprint,
        backup_path=backup_path,
        reason_code=reason_code,
    )


def _expected_fingerprint(value: str) -> str:
    clean = str(value or "").strip()
    if not _FINGERPRINT.fullmatch(clean):
        raise SeedPlanRefused("entry_authority_seed_fingerprint_invalid")
    return clean


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SeedPlanRefused("entry_authority_seed_timestamp_invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
