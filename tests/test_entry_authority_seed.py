from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_authority_seed import (
    SeedPlanRefused,
    apply_entry_authority_seed_plan,
    build_entry_authority_seed_plan,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
)


NOW = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)


class FakeSeedGuard:
    def __init__(self) -> None:
        self.masked = True
        self.proof_calls = 0
        self.block_reason: str | None = None

    def prove_quiescent(self) -> None:
        assert self.masked is True
        self.proof_calls += 1

    def block(self, *, reason_code: str) -> None:
        self.block_reason = reason_code


def _database(tmp_path: Path) -> Path:
    database_path = tmp_path / "production.sqlite3"
    session_factory = create_session_factory(database_path)
    session_factory.kw["bind"].dispose()
    return database_path


def _authority_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM trading_settings WHERE key = ?",
                (ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,),
            ).fetchone()[0]
        )


def _table_counts(database_path: Path) -> dict[str, int]:
    tables = (
        "trading_settings",
        "execution_bindings",
        "execution_order_legs",
        "repair_confirmation_tokens",
        "execution_events",
        "strategy_lifecycles",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in tables
        }


def _insert_authority_row(database_path: Path, value: object) -> None:
    encoded = value if isinstance(value, str) else json.dumps(value)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO trading_settings (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (
                ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                encoded,
                NOW.isoformat(),
            ),
        )


def test_seed_plan_is_read_only_and_requires_absent_row(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    before_bytes = database_path.read_bytes()
    before_mtime_ns = database_path.stat().st_mtime_ns

    plan = build_entry_authority_seed_plan(database_path, now=NOW)

    assert plan.authority_state == "absent"
    assert plan.database_device == database_path.stat().st_dev
    assert plan.database_inode == database_path.stat().st_ino
    assert plan.quick_check == ("ok",)
    assert plan.foreign_key_violation_count == 0
    assert plan.backup_path.parent == database_path.parent
    assert len(plan.fingerprint) == 64
    assert database_path.read_bytes() == before_bytes
    assert database_path.stat().st_mtime_ns == before_mtime_ns
    assert plan.backup_path.exists() is False


@pytest.mark.parametrize(
    "document",
    [
        {
            "schema_version": 2,
            "state": "idle",
            "generation": 0,
            "released_at": NOW.isoformat(),
        },
        {"schema_version": 2, "state": "held"},
        {"schema_version": 2, "state": "blocked"},
        "not-json",
    ],
)
def test_seed_refuses_existing_idle_held_blocked_or_malformed_row(
    tmp_path: Path,
    document: object,
) -> None:
    database_path = _database(tmp_path)
    _insert_authority_row(database_path, document)

    with pytest.raises(
        SeedPlanRefused,
        match="entry_authority_seed_row_exists",
    ):
        build_entry_authority_seed_plan(database_path, now=NOW)


def test_seed_backup_uses_sqlite_backup_api_and_passes_quick_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import telegram_kol_research.entry_authority_seed as seed_module

    database_path = _database(tmp_path)
    plan = build_entry_authority_seed_plan(database_path, now=NOW)
    real_backup = seed_module._sqlite_backup
    backup_calls: list[tuple[Path, Path]] = []

    def recording_backup(source: Path, destination: Path) -> None:
        backup_calls.append((source, destination))
        real_backup(source, destination)

    monkeypatch.setattr(seed_module, "_sqlite_backup", recording_backup)
    result = apply_entry_authority_seed_plan(
        database_path,
        backup_path=plan.backup_path,
        expected_fingerprint=plan.fingerprint,
        guard=FakeSeedGuard(),
        now=NOW,
    )

    assert result.status == "seeded"
    assert backup_calls == [(database_path.resolve(), plan.backup_path)]
    with sqlite3.connect(plan.backup_path) as backup:
        assert backup.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    assert _authority_count(plan.backup_path) == 0


def test_seed_backup_failure_is_a_prewrite_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import telegram_kol_research.entry_authority_seed as seed_module

    database_path = _database(tmp_path)
    plan = build_entry_authority_seed_plan(database_path, now=NOW)
    guard = FakeSeedGuard()

    def failed_backup(*args, **kwargs) -> None:
        raise OSError("backup destination unavailable")

    monkeypatch.setattr(seed_module, "_sqlite_backup", failed_backup)
    with pytest.raises(
        SeedPlanRefused,
        match="entry_authority_seed_precommit_failed",
    ):
        apply_entry_authority_seed_plan(
            database_path,
            backup_path=plan.backup_path,
            expected_fingerprint=plan.fingerprint,
            guard=guard,
            now=NOW,
        )

    assert _authority_count(database_path) == 0
    assert guard.block_reason is None


def test_seed_apply_changes_only_one_trading_setting_count(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    before = _table_counts(database_path)
    plan = build_entry_authority_seed_plan(database_path, now=NOW)

    result = apply_entry_authority_seed_plan(
        database_path,
        backup_path=plan.backup_path,
        expected_fingerprint=plan.fingerprint,
        guard=FakeSeedGuard(),
        now=NOW,
    )

    after = _table_counts(database_path)
    assert result.status == "seeded"
    assert _authority_count(database_path) == 1
    with sqlite3.connect(database_path) as connection:
        stored_document = json.loads(
            connection.execute(
                "SELECT value_json FROM trading_settings WHERE key = ?",
                (ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,),
            ).fetchone()[0]
        )
    assert stored_document == {
        "generation": 0,
        "released_at": NOW.isoformat(),
        "schema_version": 2,
        "state": "idle",
    }
    assert after["trading_settings"] == before["trading_settings"] + 1
    assert {
        table: count
        for table, count in after.items()
        if table != "trading_settings"
    } == {
        table: count
        for table, count in before.items()
        if table != "trading_settings"
    }


def test_seed_post_commit_integrity_failure_restores_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import telegram_kol_research.entry_authority_seed as seed_module

    database_path = _database(tmp_path)
    before = _table_counts(database_path)
    plan = build_entry_authority_seed_plan(database_path, now=NOW)
    monkeypatch.setattr(
        seed_module,
        "_post_seed_snapshot_is_valid",
        lambda *args, **kwargs: False,
    )

    result = apply_entry_authority_seed_plan(
        database_path,
        backup_path=plan.backup_path,
        expected_fingerprint=plan.fingerprint,
        guard=FakeSeedGuard(),
        now=NOW,
    )

    assert result.status == "restored"
    assert result.reason_code == "entry_authority_seed_postcheck_failed"
    assert _authority_count(database_path) == 0
    assert _table_counts(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]


def test_seed_unknown_restore_keeps_runtime_persistently_masked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import telegram_kol_research.entry_authority_seed as seed_module

    database_path = _database(tmp_path)
    plan = build_entry_authority_seed_plan(database_path, now=NOW)
    guard = FakeSeedGuard()
    monkeypatch.setattr(
        seed_module,
        "_post_seed_snapshot_is_valid",
        lambda *args, **kwargs: False,
    )

    def failed_restore(*args, **kwargs) -> None:
        raise OSError("restore status unknown")

    monkeypatch.setattr(seed_module, "_restore_database", failed_restore)
    result = apply_entry_authority_seed_plan(
        database_path,
        backup_path=plan.backup_path,
        expected_fingerprint=plan.fingerprint,
        guard=guard,
        now=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "entry_authority_seed_restore_unknown"
    assert guard.block_reason == "entry_authority_seed_restore_unknown"
    assert guard.masked is True
