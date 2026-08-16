from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shlex
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
from telegram_kol_research.deployment_preflight import _WORK_SPECS


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
_WRITER_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "check_bound_close_reservation_writer_quiescence",
    _WRITER_QUIESCENCE_SCRIPT,
)
assert _WRITER_SCRIPT_SPEC is not None and _WRITER_SCRIPT_SPEC.loader is not None
_WRITER_MODULE = importlib.util.module_from_spec(_WRITER_SCRIPT_SPEC)
_WRITER_SCRIPT_SPEC.loader.exec_module(_WRITER_MODULE)
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
_WRITER_SPEC_BY_TABLE = {
    table: (column, active_state)
    for table, column, active_state in _EXPECTED_WRITER_SPECS
}
CHECKED_AT = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
CUTOFF = CHECKED_AT - timedelta(minutes=10)
_HISTORICAL_SQLITE_UTC = "2026-08-15 00:00:00.000000"


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
        "telegram-kol-monitor-snapshot.timer",
        "telegram-kol-monitor-snapshot.service",
        "telegram-kol-sentinel.timer",
        "telegram-kol-sentinel.service",
        "telegram-kol-monitor-audit.timer",
        "telegram-kol-monitor-audit.service",
        "telegram-kol-monitor-db-stage@sentinel.service",
        "telegram-kol-monitor-db-stage@audit.service",
        "telegram-kol-runtime-scanner.service",
        "telegram-kol-runtime-agent.service",
        "telegram-kol-agent-model-egress.socket",
        "telegram-kol-agent-model-egress.service",
        "telegram-kol.service",
    ):
        assert unit in section
    assert "trap finish_bound_close_reservation_window EXIT" in section
    assert "restore_bound_close_reservation_units" in section
    assert "pgrep -f '[t]elegram_kol_research|[t]elegram-kol'" in section
    assert "fresh_active_or_unknown_writer_count" in section
    assert "historical_active_or_unknown_residue_count" in section
    assert "blocking_writer_count" in section
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
    apply_start = section.index('APPLY_SUMMARY="$RECOVERY_TMP/apply-summary.json"')
    postcheck = section.index('POSTCHECK="$(sqlite3 -readonly', apply_start)
    combined_exit = section.index("APPLY_COMBINED_STATUS", postcheck)
    assert section.index("set +e", apply_start) < postcheck
    assert "PIPESTATUS" in section[apply_start:postcheck]
    assert postcheck < combined_exit
    assert "PRAGMA quick_check" in section[postcheck:combined_exit]


def test_runbook_quiescence_inventory_preserves_state_and_refuses_unit_races():
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]

    assert "QUIESCE_TIMER_UNITS=(" in section
    assert "QUIESCE_SERVICE_UNITS=(" in section
    assert "QUIESCE_SOCKET_UNITS=(" in section
    assert "QUIESCE_TRANSIENT_ONESHOT_UNITS=(" in section
    assert "ORIGINAL_UNIT_INSTALL_STATE" in section
    assert "--property=LoadState" in section
    assert "loaded) ORIGINAL_UNIT_INSTALL_STATE" in section
    assert "not-found) ORIGINAL_UNIT_INSTALL_STATE" in section
    assert "active|inactive" in section
    assert "installed:active) exit 1" in section
    assert "discover_bound_close_db_stage_units" in section
    assert "'telegram-kol-monitor-db-stage@*.service'" in section
    assert "DB_STAGE_INITIAL_INVENTORY" in section
    assert 'cmp -s "$DB_STAGE_INITIAL_INVENTORY"' in section
    assert "stop_bound_close_unit_group \"${QUIESCE_TIMER_UNITS[@]}\"" in section
    assert "stop_bound_close_unit_group \"${QUIESCE_SERVICE_UNITS[@]}\"" in section
    assert section.index(
        'stop_bound_close_unit_group "${QUIESCE_TIMER_UNITS[@]}"'
    ) < section.index(
        'stop_bound_close_unit_group "${QUIESCE_SERVICE_UNITS[@]}"'
    )
    assert "for ((INDEX=${#QUIESCE_UNITS[@]}-1; INDEX>=0; INDEX--))" in section
    assert "PartOf=telegram-kol-runtime-agent.service" in section
    assert "不读取生产数据库" in section
    assert "不调用 Deepcoin/交易所" in section


def test_runbook_fetch_updates_the_exact_ref_it_later_verifies():
    block = _bound_close_read_only_block()

    assert (
        '"refs/heads/codex/bound-close-reservation-recovery:$APPROVED_REF"'
        in block
    )
    assert (
        "fetch --no-tags origin \\\n+  codex/bound-close-reservation-recovery"
        not in block
    )
    assert 'rev-parse "$APPROVED_REF"' in block


def _bound_close_read_only_block() -> str:
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    return section.split("```bash", 1)[1].split("```", 1)[0]


def _bound_close_poll_snippet() -> str:
    block = _bound_close_read_only_block()
    return block.split("# BEGIN BOUND_CLOSE_QUIESCENCE_POLL", 1)[1].split(
        "# END BOUND_CLOSE_QUIESCENCE_POLL", 1
    )[0]


def test_runbook_quiescence_poll_is_bounded_and_capture_is_ready_gated():
    block = _bound_close_read_only_block()

    stopped = block.index(
        'stop_bound_close_unit_group "${QUIESCE_SOCKET_UNITS[@]}"'
    )
    deadline = block.index("QUIESCENCE_DEADLINE_EPOCH", stopped)
    helper = block.index("run_bound_close_writer_quiescence_helper", deadline)
    capture = block.index("run_bound_close_double_capture", helper)
    assert "$(( $(bound_close_now_epoch) + 720 ))" in block[deadline:helper]
    assert "QUIESCENCE_POLL_SECONDS=15" in block[deadline:helper]
    assert "sleep 600" not in block
    assert "sleep 10m" not in block
    assert deadline < helper < capture
    assert "set +e" in block[:helper]
    assert "HELPER_STATUS=$?" in block[:helper]
    assert "set -e" in block[:helper]
    assert 'case "$HELPER_STATUS" in' in block[helper:capture]
    assert "0)" in block[helper:capture]
    assert "2)" in block[helper:capture]
    assert "*) exit 1" in block[helper:capture]
    assert "require_exact_ready_projection" in block[helper:capture]
    assert "require_exact_refused_projection" in block[helper:capture]


def test_runbook_quiescence_poll_rechecks_every_local_identity_around_helper():
    block = _bound_close_read_only_block()
    poll = _bound_close_poll_snippet()

    assert "verify_all_local_quiescence_and_identity" in block
    assert "pgrep -f '[t]elegram_kol_research|[t]elegram-kol'" in block
    assert 'git -C "$PRODUCTION_ROOT" rev-parse HEAD' in block
    assert "PRODUCTION_DB_RESOLVED_PATH" in block
    assert "PRODUCTION_DB_DEVICE_INODE" in block
    assert "PROCESS_SCAN_STATUS=$?" in block
    assert "1) ;;" in block
    assert "*) return 1" in block
    helper = poll.index("run_bound_close_writer_quiescence_helper")
    assert poll.rfind(
        "verify_all_local_quiescence_and_identity", 0, helper
    ) >= 0
    assert poll.find(
        "verify_all_local_quiescence_and_identity", helper
    ) >= 0


def test_runbook_quiescence_projection_is_strict_private_and_fail_closed():
    block = _bound_close_read_only_block()

    for field in (
        "block_regardless_of_age_writer_count",
        "blocking_writer_count",
        "checked_table_count",
        "fresh_active_or_unknown_writer_count",
        "historical_active_or_unknown_residue_count",
        "missing_table_count",
        "schema_version",
        "status",
        "target_reservation_count",
        "unrecognized_or_null_state_count",
    ):
        assert field in block
    assert "type(value) is not int" in block
    assert "blocking != fresh + unrecognized + block_regardless" in block
    assert "checked + missing != 20" in block
    assert 'chmod 0600 "$WRITER_QUIESCENCE_RESULT"' in block
    assert "trap finish_bound_close_reservation_window EXIT" in block
    assert "trap 'exit 129' HUP" in block
    assert "trap 'exit 130' INT" in block
    assert "trap 'exit 143' TERM" in block


def _simulate_runbook_quiescence_poll(
    tmp_path: Path,
    *,
    helper_statuses: tuple[int, ...],
    sleep_jump: int,
    projection_fails: bool = False,
    verify_fail_on: int = 0,
) -> subprocess.CompletedProcess[str]:
    events = tmp_path / "events.txt"
    sequence = " ".join(str(value) for value in helper_statuses)
    prefix = f"""
set -euo pipefail
EVENTS={shlex.quote(str(events))}
RECOVERY_TMP={shlex.quote(str(tmp_path))}
RUNTIME_PYTHON={shlex.quote(sys.executable)}
CANDIDATE_ROOT={shlex.quote(str(tmp_path / 'candidate'))}
PRODUCTION_DB={shlex.quote(str(tmp_path / 'production.db'))}
NOW_EPOCH=100
HELPER_SEQUENCE=({sequence})
HELPER_CALL=0
VERIFY_CALL=0
PROJECTION_FAIL={int(projection_fails)}
restore_bound_close_reservation_units() {{ printf 'restore\n' >> "$EVENTS"; }}
trap restore_bound_close_reservation_units EXIT
bound_close_now_epoch() {{ printf '%s\n' "$NOW_EPOCH"; }}
sleep() {{ NOW_EPOCH=$((NOW_EPOCH + {sleep_jump})); }}
verify_all_local_quiescence_and_identity() {{
  VERIFY_CALL=$((VERIFY_CALL + 1))
  [ "$VERIFY_CALL" -ne {verify_fail_on} ]
}}
run_bound_close_writer_quiescence_helper() {{
  local output_path="$1"
  local index="$HELPER_CALL"
  HELPER_CALL=$((HELPER_CALL + 1))
  HELPER_STATUS="${{HELPER_SEQUENCE[$index]:-1}}"
  if [ "$HELPER_STATUS" -eq 0 ]; then
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":0,"checked_table_count":20,"fresh_active_or_unknown_writer_count":0,"historical_active_or_unknown_residue_count":2,"missing_table_count":0,"schema_version":1,"status":"ready","target_reservation_count":1,"unrecognized_or_null_state_count":0}}' > "$output_path"
  else
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":1,"checked_table_count":20,"fresh_active_or_unknown_writer_count":1,"historical_active_or_unknown_residue_count":1,"missing_table_count":0,"schema_version":1,"status":"refused","target_reservation_count":1,"unrecognized_or_null_state_count":0}}' > "$output_path"
  fi
}}
project_bound_close_writer_quiescence_result() {{
  [ "$PROJECTION_FAIL" -eq 0 ]
  cp "$1" "$3"
}}
require_exact_ready_projection() {{ grep -q '"status":"ready"' "$1"; }}
require_exact_refused_projection() {{ grep -q '"status":"refused"' "$1"; }}
run_bound_close_double_capture() {{ printf 'capture\n' >> "$EVENTS"; }}
"""
    return subprocess.run(
        ["bash", "-c", prefix + _bound_close_poll_snippet()],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_runbook_quiescence_poll_reaches_capture_once_only_after_ready(tmp_path):
    result = _simulate_runbook_quiescence_poll(
        tmp_path,
        helper_statuses=(2, 2, 0),
        sleep_jump=15,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "events.txt").read_text() == "capture\nrestore\n"


@pytest.mark.parametrize(
    ("helper_statuses", "sleep_jump"),
    [((2,), 720), ((1,), 15)],
)
def test_runbook_quiescence_timeout_or_helper_error_restores_without_capture(
    tmp_path,
    helper_statuses,
    sleep_jump,
):
    result = _simulate_runbook_quiescence_poll(
        tmp_path,
        helper_statuses=helper_statuses,
        sleep_jump=sleep_jump,
    )

    assert result.returncode != 0
    assert (tmp_path / "events.txt").read_text() == "restore\n"


@pytest.mark.parametrize(
    ("projection_fails", "verify_fail_on"),
    [(True, 0), (False, 2)],
)
def test_runbook_quiescence_malformed_projection_or_identity_drift_restores(
    tmp_path,
    projection_fails,
    verify_fail_on,
):
    result = _simulate_runbook_quiescence_poll(
        tmp_path,
        helper_statuses=(0,),
        sleep_jump=15,
        projection_fails=projection_fails,
        verify_fail_on=verify_fail_on,
    )

    assert result.returncode != 0
    assert (tmp_path / "events.txt").read_text() == "restore\n"


def _run_runbook_quiescence_projection(
    tmp_path: Path,
    *,
    payload: dict[str, object],
    helper_status: int,
) -> subprocess.CompletedProcess[str]:
    block = _bound_close_read_only_block()
    source = tmp_path / "helper.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    source.chmod(0o600)
    projection_source = block.split(
        '"$input_path" "$helper_status" > "$output_path" <<\'PY\'\n', 1
    )[1].split("\nPY\n", 1)[0]
    return subprocess.run(
        [sys.executable, "-", str(source), str(helper_status)],
        input=projection_source,
        capture_output=True,
        text=True,
        check=False,
    )


def _valid_quiescence_projection() -> dict[str, object]:
    return {
        "block_regardless_of_age_writer_count": 0,
        "blocking_writer_count": 0,
        "checked_table_count": 20,
        "fresh_active_or_unknown_writer_count": 0,
        "historical_active_or_unknown_residue_count": 2,
        "missing_table_count": 0,
        "schema_version": 1,
        "status": "ready",
        "target_reservation_count": 1,
        "unrecognized_or_null_state_count": 0,
    }


def test_runbook_quiescence_projection_accepts_only_canonical_ready_aggregate(
    tmp_path,
):
    payload = _valid_quiescence_projection()

    result = _run_runbook_quiescence_projection(
        tmp_path,
        payload=payload,
        helper_status=0,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(blocking_writer_count=True),
        lambda payload: payload.update(blocking_writer_count=1),
        lambda payload: payload.update(status="refused"),
        lambda payload: payload.update(unexpected=0),
    ],
)
def test_runbook_quiescence_projection_rejects_type_arithmetic_status_or_shape(
    tmp_path,
    mutate,
):
    payload = _valid_quiescence_projection()
    mutate(payload)

    result = _run_runbook_quiescence_projection(
        tmp_path,
        payload=payload,
        helper_status=0,
    )

    assert result.returncode == 2
    assert result.stdout == ""


def test_apply_window_failure_simulation_runs_postcheck_before_exit():
    simulation = subprocess.run(
        [
            "bash",
            "-c",
            "set -uo pipefail; events=(); set +e; "
            "false | true; pipe_status=(\"${PIPESTATUS[@]}\"); "
            "events+=(apply); false; summary_status=$?; "
            "events+=(postcheck); false; postcheck_status=$?; "
            "events+=(quick_check); true; quick_status=$?; set -e; "
            "combined=0; "
            "for value in \"${pipe_status[@]}\" \"$summary_status\" "
            "\"$postcheck_status\" \"$quick_status\"; do "
            "if [ \"$value\" -ne 0 ]; then combined=9; fi; done; "
            "printf '%s\\n' \"${events[*]}\"; exit \"$combined\"",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert simulation.returncode == 9
    assert simulation.stdout == "apply postcheck quick_check\n"
    assert simulation.stderr == ""


def _writer_quiescence_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        for spec in _WORK_SPECS:
            connection.execute(
                f'CREATE TABLE "{spec.table}" ('
                f'id INTEGER PRIMARY KEY, "{spec.state_column}" TEXT, '
                f'"{spec.time_column}" DEFAULT \'{_HISTORICAL_SQLITE_UTC}\')'
            )
        connection.execute("CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE execution_events (id INTEGER PRIMARY KEY)")
        connection.execute(
            "INSERT INTO bound_position_close_reservations "
            "(id, status, updated_at) VALUES (1, 'submitted', ?)",
            (_HISTORICAL_SQLITE_UTC,),
        )


def _inspect_writer_quiescence(path: Path, *, now: datetime = CHECKED_AT):
    return _WRITER_MODULE.inspect_writer_quiescence(path, now=now)


def _sqlite_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(
        sep=" ", timespec="microseconds"
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


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_recovery_output_projector_requires_integer_apply_schema_version(
    schema_version,
):
    document = json.dumps(
        {
            "action_count": 29,
            "audit_event_id": 987654,
            "evidence_fingerprint": FP_A,
            "mode": "apply",
            "schema_version": schema_version,
            "status": "applied",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    result = _project_recovery_output("apply-result", document)

    assert result.returncode == 2
    assert result.stdout == '{"status":"refused"}\n'
    assert result.stderr == ""


def test_writer_quiescence_inventory_exactly_mirrors_deployment_preflight_specs(
    tmp_path,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)

    result = _run_writer_quiescence(database)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "block_regardless_of_age_writer_count": 0,
        "blocking_writer_count": 0,
        "checked_table_count": len(_EXPECTED_WRITER_SPECS),
        "fresh_active_or_unknown_writer_count": 0,
        "historical_active_or_unknown_residue_count": 0,
        "missing_table_count": 0,
        "schema_version": 1,
        "status": "ready",
        "target_reservation_count": 1,
        "unrecognized_or_null_state_count": 0,
    }
    assert result.stderr == ""


def test_writer_quiescence_contract_includes_reviewed_nonwriter_states():
    helper_text = _WRITER_QUIESCENCE_SCRIPT.read_text(encoding="utf-8")

    assert '"resolved"' in helper_text
    assert '"waiting_backup_stop"' in helper_text
    assert "ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES" in helper_text
    assert "trigger_take_profit_convergence.py" in helper_text


@pytest.mark.parametrize(
    ("table", "state"),
    [
        (table, state)
        for table, states in {
            "deepcoin_execution_operations": (
                "pre_submit_deferred", "completed", "submission_failed_no_exposure"
            ),
            "execution_order_legs": (
                "planned", "reserved", "submitted", "pending", "open", "active",
                "filled", "partially_filled", "partial", "confirmed", "succeeded",
                "failed", "rejected", "cancelled", "canceled", "manually_cancelled",
                "exchange_cancelled", "manually_closed", "closed", "expired",
                "invalidated", "blocked",
            ),
            "message_instruction_items": ("submitted", "succeeded", "failed"),
            "trade_signals": (
                "submitted", "recovery_required", "confirmed", "failed", "rejected",
                "blocked", "skipped", "expired", "cancelled",
            ),
            "instruction_execution_contracts": (
                "verified", "failed", "expired", "completed"
            ),
            "strategy_revision_batches": ("succeeded", "failed", "blocked"),
            "strategy_management_batches": (
                "blocked", "failed", "succeeded", "resolved", "shadow_planned", "completed"
            ),
            "strategy_management_legs": (
                "confirmed", "definitely_rejected", "failed", "blocked", "succeeded",
                "resolved", "restored", "safely_skipped",
            ),
            "strategy_management_components": (
                "blocked", "confirmed", "operator_required", "safely_skipped"
            ),
            "position_mutation_intents": ("not_sent", "confirmed", "rejected", "blocked"),
            "bound_position_close_reservations": ("confirmed",),
            "position_backup_stop_orders": (
                "not_sent", "active", "verified", "cancelled", "superseded",
                "unverified_exchange", "failed", "rejected", "expired", "missing",
            ),
            "position_take_profit_orders": (
                "active", "cancelled", "filled", "expired", "conflicted", "completed"
            ),
            "position_protection_legs": ("verified", "filled", "failed", "blocked", "missing"),
            "trigger_protection_intents": ("adopted", "failed", "resolved"),
            "trigger_protection_stop_rescues": ("confirmed", "succeeded", "failed", "blocked"),
            "trigger_take_profit_convergences": (
                "waiting_position", "waiting_backup_stop", "completed", "conflicted", "blocked"
            ),
            "strategy_break_even_convergences": (
                "blocked", "shadow_deciding", "shadow_planned", "completed",
                "failed_terminal", "succeeded",
            ),
            "strategy_break_even_convergence_legs": (
                "planned", "shadow_planned", "stop_confirmed", "verified", "confirmed",
                "succeeded", "failed_terminal", "blocked",
            ),
            "source_message_deletion_exits": (
                "succeeded", "failed", "blocked", "unbound"
            ),
        }.items()
        for state in states
    ],
)
def test_writer_quiescence_accepts_every_reviewed_nonwriter_state(
    tmp_path,
    table,
    state,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    column, _active_state = _WRITER_SPEC_BY_TABLE[table]
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'INSERT INTO "{table}" (id, "{column}") VALUES (?, ?)',
            (2, state),
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 0, (table, state, result.stdout, result.stderr)
    assert json.loads(result.stdout)["status"] == "ready"


def test_writer_quiescence_refuses_unreviewed_trigger_protection_state(tmp_path):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trigger_protection_intents (id, recovery_state) "
            "VALUES (1, 'blocked')"
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["unrecognized_or_null_state_count"] == 1


@pytest.mark.parametrize(
    ("table", "column", "active_state"),
    [
        (spec.table, spec.state_column, state)
        for spec in _WORK_SPECS
        if spec.table != "bound_position_close_reservations"
        for state in sorted(set(spec.active_states) | set(spec.unknown_states))
    ],
)
def test_writer_quiescence_refuses_each_preflight_active_or_unknown_state(
    tmp_path,
    table,
    column,
    active_state,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        time_column = next(
            spec.time_column for spec in _WORK_SPECS if spec.table == table
        )
        connection.execute(
            f'INSERT INTO "{table}" (id, "{column}", "{time_column}") '
            "VALUES (1, ?, '2999-01-01 00:00:00.000000')",
            (active_state,),
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    count_field = (
        "block_regardless_of_age_writer_count"
        if table == "deepcoin_execution_operations"
        else "fresh_active_or_unknown_writer_count"
    )
    assert payload[count_field] == 1
    assert set(payload) == {
        "block_regardless_of_age_writer_count",
        "blocking_writer_count",
        "checked_table_count",
        "fresh_active_or_unknown_writer_count",
        "historical_active_or_unknown_residue_count",
        "missing_table_count",
        "schema_version",
        "status",
        "target_reservation_count",
        "unrecognized_or_null_state_count",
    }
    assert table not in result.stdout
    assert json.dumps(active_state) not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "target_state",
    [
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    ],
)
def test_writer_quiescence_exempts_only_exact_target_population(
    tmp_path,
    target_state,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bound_position_close_reservations SET status = ? WHERE id = 1",
            (target_state,),
        )

    result = _run_writer_quiescence(database)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["target_reservation_count"] == 1
    assert payload["blocking_writer_count"] == 0


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
    expected_field = (
        "block_regardless_of_age_writer_count"
        if table == "deepcoin_execution_operations"
        else "unrecognized_or_null_state_count"
    )
    assert payload[expected_field] == 1
    assert "future_writer_state" not in result.stdout
    assert table not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("known_state", ["submitting", "unknown"])
@pytest.mark.parametrize(
    ("updated_at", "expected_status", "fresh", "historical"),
    [
        (CUTOFF - timedelta(microseconds=1), "ready", 0, 1),
        (CUTOFF, "refused", 1, 0),
        (CUTOFF + timedelta(microseconds=1), "refused", 1, 0),
    ],
)
def test_writer_quiescence_known_active_or_unknown_state_uses_exact_cutoff(
    tmp_path,
    known_state,
    updated_at,
    expected_status,
    fresh,
    historical,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO execution_order_legs (id, status, updated_at) "
            "VALUES (1, ?, ?)",
            (known_state, _sqlite_utc(updated_at)),
        )

    payload = _inspect_writer_quiescence(database)

    assert payload["status"] == expected_status
    assert payload["fresh_active_or_unknown_writer_count"] == fresh
    assert payload["historical_active_or_unknown_residue_count"] == historical
    assert payload["blocking_writer_count"] == fresh


def test_writer_quiescence_future_timestamp_is_fresh_and_blocking(tmp_path):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (1, 'pending', ?)",
            (_sqlite_utc(CHECKED_AT + timedelta(days=1)),),
        )

    payload = _inspect_writer_quiescence(database)

    assert payload["status"] == "refused"
    assert payload["fresh_active_or_unknown_writer_count"] == 1
    assert payload["historical_active_or_unknown_residue_count"] == 0
    assert payload["blocking_writer_count"] == 1


def test_writer_quiescence_accepts_explicit_utc_timestamp(tmp_path):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (1, 'pending', ?)",
            ((CUTOFF - timedelta(microseconds=1)).isoformat(),),
        )

    payload = _inspect_writer_quiescence(database)

    assert payload["status"] == "ready"
    assert payload["historical_active_or_unknown_residue_count"] == 1


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        None,
        123,
        "not-a-timestamp",
        "2026-08-15T12:00:00-07:00",
        "2026-08-15T19:00:00+0000",
    ],
)
def test_writer_quiescence_rejects_invalid_or_non_utc_timestamp(
    tmp_path,
    invalid_timestamp,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (1, 'pending', ?)",
            (invalid_timestamp,),
        )

    with pytest.raises(
        _WRITER_MODULE._WriterQuiescenceError,
        match="writer_quiescence_timestamp_invalid",
    ):
        _inspect_writer_quiescence(database)


@pytest.mark.parametrize(
    "updated_at",
    [CUTOFF - timedelta(microseconds=1), CUTOFF + timedelta(days=1)],
)
def test_writer_quiescence_unrecognized_state_blocks_regardless_of_timestamp(
    tmp_path,
    updated_at,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (1, 'future_writer_state', ?)",
            (_sqlite_utc(updated_at),),
        )

    payload = _inspect_writer_quiescence(database)

    assert payload["status"] == "refused"
    assert payload["unrecognized_or_null_state_count"] == 1
    assert payload["fresh_active_or_unknown_writer_count"] == 0
    assert payload["historical_active_or_unknown_residue_count"] == 0
    assert payload["blocking_writer_count"] == 1


@pytest.mark.parametrize("state", ["entry_unknown", None, "future_writer_state"])
@pytest.mark.parametrize(
    "updated_at",
    [CUTOFF - timedelta(microseconds=1), CUTOFF + timedelta(days=1)],
)
def test_writer_quiescence_deepcoin_nonterminal_blocks_regardless_of_timestamp(
    tmp_path,
    state,
    updated_at,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO deepcoin_execution_operations (id, state, updated_at) "
            "VALUES (1, ?, ?)",
            (state, _sqlite_utc(updated_at)),
        )

    payload = _inspect_writer_quiescence(database)

    assert payload["status"] == "refused"
    assert payload["block_regardless_of_age_writer_count"] == 1
    assert payload["fresh_active_or_unknown_writer_count"] == 0
    assert payload["historical_active_or_unknown_residue_count"] == 0
    assert payload["blocking_writer_count"] == 1


def test_writer_quiescence_bounded_table_inspection_refuses_overflow(tmp_path):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (?, 'pending', ?)",
            (
                (row_id, _HISTORICAL_SQLITE_UTC)
                for row_id in range(
                    1, _WRITER_MODULE._MAX_INSPECTED_ROWS_PER_TABLE + 2
                )
            ),
        )

    with pytest.raises(
        _WRITER_MODULE._WriterQuiescenceError,
        match="writer_quiescence_inspection_limit_exceeded",
    ):
        _inspect_writer_quiescence(database)


@pytest.mark.parametrize("invalid_count", [True, 1.0])
def test_writer_quiescence_result_rejects_non_exact_integer_counts(invalid_count):
    with pytest.raises(
        _WRITER_MODULE._WriterQuiescenceError,
        match="writer_quiescence_result_invalid",
    ):
        _WRITER_MODULE._build_result(
            checked_table_count=invalid_count,
            missing_table_count=0,
            target_reservation_count=1,
            fresh_active_or_unknown_writer_count=0,
            historical_active_or_unknown_residue_count=0,
            unrecognized_or_null_state_count=0,
            block_regardless_of_age_writer_count=0,
        )


def test_writer_quiescence_production_shaped_aggregate_transitions_to_ready(
    tmp_path,
):
    database = tmp_path / "writers.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM bound_position_close_reservations")
        connection.executemany(
            "INSERT INTO bound_position_close_reservations "
            "(id, status, updated_at) VALUES (?, 'submitted', ?)",
            ((row_id, _HISTORICAL_SQLITE_UTC) for row_id in range(1, 30)),
        )
        connection.executemany(
            "INSERT INTO execution_order_legs (id, status, updated_at) "
            "VALUES (?, 'submitting', ?)",
            ((row_id, _HISTORICAL_SQLITE_UTC) for row_id in range(1, 514)),
        )
        connection.executemany(
            "INSERT INTO strategy_management_legs (id, status, updated_at) "
            "VALUES (?, 'restored', ?)",
            ((row_id, _HISTORICAL_SQLITE_UTC) for row_id in range(1, 3)),
        )
        connection.executemany(
            "INSERT INTO position_backup_stop_orders (id, status, updated_at) "
            "VALUES (?, 'missing', ?)",
            ((row_id, _HISTORICAL_SQLITE_UTC) for row_id in range(1, 17)),
        )
        connection.executemany(
            "INSERT INTO source_message_deletion_exits (id, state, updated_at) "
            "VALUES (?, 'unbound', ?)",
            ((row_id, _HISTORICAL_SQLITE_UTC) for row_id in range(1, 76)),
        )
        connection.executemany(
            "INSERT INTO message_instruction_items (id, status, updated_at) "
            "VALUES (?, 'pending', ?)",
            ((row_id, _sqlite_utc(CUTOFF)) for row_id in range(1, 3)),
        )

    blocked = _inspect_writer_quiescence(database)

    assert {
        key: blocked[key]
        for key in (
            "fresh_active_or_unknown_writer_count",
            "historical_active_or_unknown_residue_count",
            "target_reservation_count",
            "unrecognized_or_null_state_count",
            "block_regardless_of_age_writer_count",
            "blocking_writer_count",
            "status",
        )
    } == {
        "fresh_active_or_unknown_writer_count": 2,
        "historical_active_or_unknown_residue_count": 513,
        "target_reservation_count": 29,
        "unrecognized_or_null_state_count": 0,
        "block_regardless_of_age_writer_count": 0,
        "blocking_writer_count": 2,
        "status": "refused",
    }

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET updated_at = ?",
            (_sqlite_utc(CUTOFF - timedelta(microseconds=1)),),
        )

    ready = _inspect_writer_quiescence(database)

    assert ready["status"] == "ready"
    assert ready["fresh_active_or_unknown_writer_count"] == 0
    assert ready["historical_active_or_unknown_residue_count"] == 515
    assert ready["target_reservation_count"] == 29
    assert ready["unrecognized_or_null_state_count"] == 0
    assert ready["block_regardless_of_age_writer_count"] == 0
    assert ready["blocking_writer_count"] == 0


@pytest.mark.parametrize(
    "missing",
    [
        (),
        ("trigger_take_profit_convergences",),
        (
            "strategy_break_even_convergences",
            "strategy_break_even_convergence_legs",
        ),
        (
            "trigger_take_profit_convergences",
            "strategy_break_even_convergences",
            "strategy_break_even_convergence_legs",
        ),
        ("deepcoin_execution_operations",),
    ],
)
def test_writer_quiescence_accepts_only_audited_prior_schema_missing_sets(
    tmp_path,
    missing,
):
    database = tmp_path / "prior.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        for table in missing:
            connection.execute(f'DROP TABLE "{table}"')

    result = _run_writer_quiescence(database)

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["missing_table_count"] == len(missing)


@pytest.mark.parametrize(
    "missing_table",
    [
        "strategy_management_batches",
        "bound_position_close_reservations",
        "execution_bindings",
        "execution_events",
        "execution_order_legs",
        "position_mutation_intents",
    ],
)
def test_writer_quiescence_rejects_unapproved_or_source_required_missing_table(
    tmp_path,
    missing_table,
):
    database = tmp_path / "missing.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f'DROP TABLE "{missing_table}"')

    result = _run_writer_quiescence(database)

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "error"


def test_writer_quiescence_rejects_nineteen_missing_work_tables(tmp_path):
    database = tmp_path / "missing.db"
    _writer_quiescence_database(database)
    with sqlite3.connect(database) as connection:
        for table, _column, _active in _EXPECTED_WRITER_SPECS:
            if table != "bound_position_close_reservations":
                connection.execute(f'DROP TABLE "{table}"')

    result = _run_writer_quiescence(database)

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "error"
