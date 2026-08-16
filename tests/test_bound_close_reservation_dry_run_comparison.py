from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from telegram_kol_research.bound_close_reservation_recovery import (
    BoundCloseReservationObservation,
    ReservationClassification,
    build_bound_close_reservation_recovery_plan,
    serialize_bound_close_reservation_recovery_plan,
)


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "compare_bound_close_reservation_dry_runs.py"
)
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "compare_bound_close_reservation_dry_runs",
    _SCRIPT_PATH,
)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
main = _SCRIPT_MODULE.main


START = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
FP_D = "d" * 64

_WRITER_QUIESCENCE_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_bound_close_reservation_writer_quiescence.py"
)
_RECOVERY_OUTPUT_PROJECTOR = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "project_bound_close_reservation_recovery_output.py"
)
_EXPECTED_WRITER_SPECS = (
    ("deepcoin_execution_operations", "state", "entry_pending_readback"),
    ("execution_order_legs", "status", "submitting"),
    ("message_instruction_items", "status", "pending"),
    ("trade_signals", "status", "processing"),
    ("instruction_execution_contracts", "state", "deferred"),
    ("strategy_revision_batches", "status", "planned"),
    ("strategy_management_batches", "status", "ready"),
    ("strategy_management_legs", "status", "reserved"),
    ("strategy_management_components", "status", "preflighting"),
    ("position_mutation_intents", "status", "reserved"),
    ("bound_position_close_reservations", "status", "submitted"),
    ("position_backup_stop_orders", "status", "pending_readback"),
    ("position_take_profit_orders", "status", "cancel_requested"),
    ("position_protection_legs", "status", "waiting_fill"),
    ("trigger_protection_intents", "recovery_state", "retrying"),
    ("trigger_protection_stop_rescues", "status", "ready"),
    ("trigger_take_profit_convergences", "status", "reserved"),
    ("strategy_break_even_convergences", "status", "claimed"),
    ("strategy_break_even_convergence_legs", "status", "decision_reserved"),
    ("source_message_deletion_exits", "state", "reconciling"),
)


def _observation(
    *,
    reservation_ref: str = FP_A,
    classification: ReservationClassification = ReservationClassification.PROVEN_TERMINAL,
    reason_code: str = "exact_close_and_position_terminal",
) -> BoundCloseReservationObservation:
    return BoundCloseReservationObservation(
        reservation_ref=reservation_ref,
        classification=classification,
        reason_code=reason_code,
        source_fingerprint=FP_B,
        exchange_fingerprint=FP_C,
    )


def _document(
    *,
    started_at: datetime,
    source_fingerprint: str = FP_D,
    observations: tuple[BoundCloseReservationObservation, ...] | None = None,
) -> str:
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source_fingerprint,
        observations=observations or (_observation(),),
    )
    return serialize_bound_close_reservation_recovery_plan(
        plan,
        capture_started_at=started_at,
        capture_completed_at=started_at + timedelta(seconds=30),
    )


def _write_private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(capsys, *paths: Path) -> tuple[int, str, str]:
    result = main([str(path) for path in paths])
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def test_comparator_accepts_only_two_independent_semantically_stable_captures(
    tmp_path, capsys
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(started_at=START + timedelta(minutes=2)),
    )

    assert _run(capsys, first, second) == (0, '{"status":"stable"}\n', "")


@pytest.mark.parametrize("path_count", [0, 1, 3])
def test_comparator_requires_exactly_two_paths(tmp_path, capsys, path_count):
    paths = tuple(
        _write_private(
            tmp_path / f"capture-{index}.json",
            _document(started_at=START + timedelta(minutes=index)),
        )
        for index in range(path_count)
    )

    code, output, error = _run(capsys, *paths)

    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert error == ""


def test_comparator_rejects_permissions_symlinks_and_non_regular_files(
    tmp_path, capsys
):
    valid = _write_private(tmp_path / "valid.json", _document(started_at=START))
    loose = _write_private(
        tmp_path / "loose.json",
        _document(started_at=START + timedelta(minutes=2)),
    )
    loose.chmod(0o640)
    code, output, error = _run(capsys, valid, loose)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )

    target = _write_private(
        tmp_path / "target.json",
        _document(started_at=START + timedelta(minutes=3)),
    )
    symlink = tmp_path / "capture-link.json"
    symlink.symlink_to(target)
    code, output, error = _run(capsys, valid, symlink)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )

    directory = tmp_path / "capture-dir"
    directory.mkdir(mode=0o700)
    code, output, error = _run(capsys, valid, directory)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )


def _assert_refused(capsys, first: Path, second: Path) -> None:
    code, output, error = _run(capsys, first, second)
    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert error == ""


def test_comparator_rejects_duplicate_keys_unknown_fields_and_boundedness(
    tmp_path, capsys
):
    valid = _write_private(tmp_path / "valid.json", _document(started_at=START))

    duplicate = _document(started_at=START + timedelta(minutes=2)).replace(
        '{"action_count":1,',
        '{"action_count":1,"action_count":1,',
        1,
    )
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "duplicate.json", duplicate),
    )

    payload = json.loads(_document(started_at=START + timedelta(minutes=3)))
    payload["unknown_field"] = "TOPSECRET-MUST-NOT-BE-ECHOED"
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "unknown.json", json.dumps(payload)),
    )

    oversized = _document(started_at=START + timedelta(minutes=4)) + (" " * 70_000)
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "oversized.json", oversized),
    )

    nested = json.loads(_document(started_at=START + timedelta(minutes=5)))
    nested["unknown_field"] = [[[[[["TOPSECRET"]]]]]]
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "deep.json", json.dumps(nested)),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(status="refused", action_count=0),
        lambda payload: payload.update(exchange_writes=1),
        lambda payload: payload.update(history_replays=1),
        lambda payload: payload.update(database_writes=1),
        lambda payload: payload["counts"].update(total=2),
        lambda payload: payload.update(evidence_fingerprint="0" * 64),
        lambda payload: payload.update(exchange_snapshot_fingerprint="0" * 64),
        lambda payload: payload.update(confirmation_token="BOUND-CLOSE-0000000000000000"),
    ],
)
def test_comparator_rejects_nonready_writes_bad_counts_and_bad_seals(
    tmp_path, capsys, mutate
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    payload = json.loads(_document(started_at=START + timedelta(minutes=2)))
    mutate(payload)
    second = _write_private(tmp_path / "second.json", json.dumps(payload))

    _assert_refused(capsys, first, second)


def test_comparator_rejects_repeated_capture_identity(tmp_path, capsys):
    document = _document(started_at=START)
    first = _write_private(tmp_path / "first.json", document)
    second = _write_private(tmp_path / "second.json", document)

    _assert_refused(capsys, first, second)


def test_comparator_rejects_any_valid_semantic_drift_without_echoing_input(
    tmp_path, capsys
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(
            started_at=START + timedelta(minutes=2),
            source_fingerprint="e" * 64,
        ),
    )

    code, output, error = _run(capsys, first, second)

    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert "e" * 64 not in output
    assert error == ""


def test_comparator_does_not_write_or_change_capture_files(tmp_path, capsys):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(started_at=START + timedelta(minutes=2)),
    )
    before = [(path.read_bytes(), os.stat(path).st_mtime_ns) for path in (first, second)]

    assert _run(capsys, first, second)[0] == 0

    after = [(path.read_bytes(), os.stat(path).st_mtime_ns) for path in (first, second)]
    assert after == before


def test_runbook_closes_stopped_service_capture_and_apply_boundaries():
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]

    for unit in (
        "telegram-kol-monitor.timer",
        "telegram-kol-monitor.service",
        "telegram-kol-monitor-diagnostic.service",
        "telegram-kol-monitor-test-notification.service",
        "telegram-kol-runtime-scanner.service",
        "telegram-kol-runtime-agent.service",
        "telegram-kol.service",
    ):
        assert unit in section
    assert "trap finish_bound_close_reservation_window EXIT" in section
    assert "restore_bound_close_reservation_units" in section
    assert "pgrep -f '[t]elegram_kol_research|[t]elegram-kol'" in section
    assert "other_active_or_unknown_writer_count" in section
    assert "target_reservation_count" in section
    assert 'RESULT="$RECOVERY_TMP/dry-run-${ATTEMPT}.json"' in section
    assert 'chmod 0600 "$RESULT"' in section
    assert "compare_bound_close_reservation_dry_runs.py" in section
    assert '{"status":"stable"}' in section
    assert 'PRAGMA quick_check;' in section
    assert "PRAGMA query_only=ON;" in section
    assert "json_extract(value_json, '$.mimo_contract_mode')" in section
    assert "FROM trading_settings WHERE key='global'" in section
    assert "STOP_BOUND_CLOSE_RESERVATION_RECOVERY_BEFORE_BATCH119" in section
    assert "classification counts" in section
    assert "raw ids" in section
    assert "provider rows" in section
    assert "credentials" in section
    assert "check_bound_close_reservation_writer_quiescence.py" in section
    assert "apply-result.json" not in section
    assert 'APPLY_SUMMARY="$RECOVERY_TMP/apply-summary.json"' in section
    assert '"status","action_count","evidence_fingerprint"' in section


def _writer_quiescence_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        for table, column, _active_state in _EXPECTED_WRITER_SPECS:
            connection.execute(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, "{column}" TEXT)'
            )
        connection.execute(
            "INSERT INTO bound_position_close_reservations (id, status) "
            "VALUES (1, 'submitted')"
        )


def _run_writer_quiescence(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_WRITER_QUIESCENCE_SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _project_recovery_output(
    kind: str,
    document: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_RECOVERY_OUTPUT_PROJECTOR), kind],
        input=document,
        capture_output=True,
        text=True,
        check=False,
    )


def test_recovery_output_projector_hides_capture_token_and_observations():
    document = _document(started_at=START)

    result = _project_recovery_output("capture", document)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "action_count",
        "counts",
        "database_writes",
        "evidence_fingerprint",
        "exchange_snapshot_fingerprint",
        "exchange_writes",
        "history_replays",
        "source_fingerprint",
        "status",
    }
    assert "confirmation_token" not in result.stdout
    assert "observations" not in result.stdout
    assert FP_A not in result.stdout
    assert result.stderr == ""


def test_recovery_output_projector_drops_apply_audit_event_id():
    document = json.dumps(
        {
            "action_count": 29,
            "audit_event_id": 987654,
            "evidence_fingerprint": FP_A,
            "mode": "apply",
            "schema_version": 1,
            "status": "applied",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    result = _project_recovery_output("apply-result", document)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action_count": 29,
        "evidence_fingerprint": FP_A,
        "status": "applied",
    }
    assert "audit_event_id" not in result.stdout
    assert "987654" not in result.stdout
    assert result.stderr == ""


def test_recovery_output_projector_rejects_duplicate_or_unknown_apply_fields():
    duplicate = (
        '{"action_count":1,"action_count":1,"audit_event_id":2,'
        f'"evidence_fingerprint":"{FP_A}","mode":"apply",'
        '"schema_version":1,"status":"applied"}'
    )
    unknown = json.dumps(
        {
            "action_count": 1,
            "audit_event_id": 2,
            "evidence_fingerprint": FP_A,
            "mode": "apply",
            "provider_row": "TOPSECRET",
            "schema_version": 1,
            "status": "applied",
        }
    )

    for document in (duplicate, unknown):
        result = _project_recovery_output("apply-result", document)
        assert result.returncode == 2
        assert result.stdout == '{"status":"refused"}\n'
        assert "TOPSECRET" not in result.stdout
        assert result.stderr == ""


def test_writer_quiescence_inventory_exactly_mirrors_deployment_preflight_specs(
    tmp_path,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)

    result = _run_writer_quiescence(database)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "checked_table_count": len(_EXPECTED_WRITER_SPECS),
        "missing_table_count": 0,
        "other_active_or_unknown_writer_count": 0,
        "schema_version": 1,
        "status": "ready",
        "target_reservation_count": 1,
    }
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("table", "column", "active_state"),
    [
        spec
        for spec in _EXPECTED_WRITER_SPECS
        if spec[0] != "bound_position_close_reservations"
    ],
)
def test_writer_quiescence_refuses_each_preflight_writer_table(
    tmp_path,
    table,
    column,
    active_state,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'INSERT INTO "{table}" (id, "{column}") VALUES (1, ?)',
            (active_state,),
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["other_active_or_unknown_writer_count"] == 1
    assert set(payload) == {
        "checked_table_count",
        "missing_table_count",
        "other_active_or_unknown_writer_count",
        "schema_version",
        "status",
        "target_reservation_count",
    }
    assert table not in result.stdout
    assert active_state not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("invalid_state", [None, "future_writer_state"])
@pytest.mark.parametrize(
    ("table", "column", "_active_state"),
    _EXPECTED_WRITER_SPECS,
)
def test_writer_quiescence_refuses_null_and_future_states_in_every_existing_table(
    tmp_path,
    table,
    column,
    _active_state,
    invalid_state,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f'DELETE FROM "{table}"')
        connection.execute(
            f'INSERT INTO "{table}" (id, "{column}") VALUES (1, ?)',
            (invalid_state,),
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["other_active_or_unknown_writer_count"] == 1
    assert "future_writer_state" not in result.stdout
    assert table not in result.stdout
    assert result.stderr == ""


def test_writer_quiescence_explicitly_accepts_missing_prior_schema_tables(tmp_path):
    database = tmp_path / "prior.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE bound_position_close_reservations "
            "(id INTEGER PRIMARY KEY, status TEXT)"
        )
        connection.execute(
            "INSERT INTO bound_position_close_reservations VALUES (1, 'submitted')"
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["checked_table_count"] == 1
    assert payload["missing_table_count"] == len(_EXPECTED_WRITER_SPECS) - 1
    assert payload["other_active_or_unknown_writer_count"] == 0
