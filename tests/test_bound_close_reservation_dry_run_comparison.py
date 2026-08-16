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
import time

import pytest

from telegram_kol_research.bound_close_reservation_recovery import (
    BoundCloseReservationObservation,
    MAX_RECOVERY_PLAN_BYTES,
    ReservationClassification,
    build_bound_close_reservation_recovery_plan,
    serialize_bound_close_reservation_recovery_plan,
)
from telegram_kol_research.bound_close_writer_quiescence import (
    inspect_bound_close_writer_quiescence,
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
_WRITER_QUIESCENCE_LIBRARY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "telegram_kol_research"
    / "bound_close_writer_quiescence.py"
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


def test_bound_close_runbook_deadline_contract_is_explicit():
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]

    for expected in (
        "单次 capture 的绝对硬上限为 180 秒",
        "完成即立即返回",
        "停服窗口的 12 分钟 absolute deadline 不变",
        "timeout 仍为 `UNKNOWN / exchange_capture_timeout`",
        "不能在同一停服窗口重试",
    ):
        assert expected in section

    writer_definition = section.index("write_joint_stopped_runner() {")
    writer_invocation = section.index(
        'write_joint_stopped_runner "$STOPPED_PHASE_RUNNER"'
    )
    first_unit_stop = section.index("QUIESCE_ATTEMPTED=1")
    assert writer_definition < writer_invocation < first_unit_stop
    assert "timeout --signal=KILL" in section
    assert "--kill-after" not in section


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


@pytest.mark.parametrize(
    ("list_unit_files_status", "expected_status"),
    [(1, 0), (2, 1)],
)
def test_db_stage_discovery_distinguishes_no_matches_from_systemctl_failure(
    tmp_path,
    list_unit_files_status,
    expected_status,
):
    block = _bound_close_read_only_block()
    function = block.split("discover_bound_close_db_stage_units() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    output = tmp_path / "inventory.txt"
    script = f"""
set -euo pipefail
QUIESCE_DB_STAGE_SEED_UNITS=(
  telegram-kol-monitor-db-stage@sentinel.service
  telegram-kol-monitor-db-stage@audit.service
)
systemctl() {{
  case "$1" in
    list-units) return 0 ;;
    list-unit-files) return {list_unit_files_status} ;;
    *) return 91 ;;
  esac
}}
discover_bound_close_db_stage_units() {{
{function}
}}
discover_bound_close_db_stage_units "$1"
"""

    result = subprocess.run(
        ["bash", "-c", script, "bash", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_status, result.stderr
    if expected_status == 0:
        assert output.read_text(encoding="utf-8").splitlines() == [
            "telegram-kol-monitor-db-stage@audit.service",
            "telegram-kol-monitor-db-stage@sentinel.service",
        ]
    assert result.stderr == ""


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


def test_runbook_freezes_monitor_timer_before_resetting_legacy_failed_state():
    block = _bound_close_read_only_block()
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    approval_text = section.split("```bash", 1)[0]

    reset_token = (
        "I_APPROVE_LEGACY_MONITOR_FAILED_STATE_RESET_FOR_"
        "BOUND_CLOSE_READ_ONLY_WINDOW"
    )
    assert "BOUND_CLOSE_LEGACY_MONITOR_RESET_APPROVAL" in block
    assert reset_token in block
    assert reset_token in approval_text
    assert "failed` 重置为 `inactive`" in approval_text
    assert "active|inactive|failed" in block
    assert 'if [ "$active_state" = failed ]' in block
    assert '[ "$unit" != telegram-kol-monitor.service ]; then' in block
    stop_timer = block.index(
        'stop_bound_close_unit_group "${QUIESCE_TIMER_UNITS[@]}"'
    )
    verify_timer = block.index(
        'verify_bound_close_unit_group_inactive "${QUIESCE_TIMER_UNITS[@]}"',
        stop_timer,
    )
    reset_call = block.index(
        "\nreset_bound_close_legacy_monitor_after_timer_freeze\n",
        stop_timer,
    )
    stop_services = block.index(
        'stop_bound_close_unit_group "${QUIESCE_SERVICE_UNITS[@]}"'
    )
    assert stop_timer < verify_timer < reset_call < stop_services
    reset_function = block.split(
        "reset_bound_close_legacy_monitor_after_timer_freeze() {", 1
    )[1].split("\n}\n", 1)[0]
    assert reset_function.index("systemctl stop telegram-kol-monitor.service") < (
        reset_function.index("systemctl reset-failed telegram-kol-monitor.service")
    )
    assert (
        'ORIGINAL_UNIT_STATE["telegram-kol-monitor.service"]=inactive'
        in reset_function
    )
    restore = block.split("restore_bound_close_reservation_units() {", 1)[1].split(
        "finish_bound_close_reservation_window() {", 1
    )[0]
    assert "installed:failed)" in restore
    assert "systemctl is-failed --quiet" in restore


@pytest.mark.parametrize(
    (
        "install_state",
        "original_state",
        "current_state",
        "expected_log",
        "expected_state",
        "expected_status",
    ),
    [
        (
            "installed",
            "failed",
            "failed",
            "reset-failed telegram-kol-monitor.service\n",
            "inactive",
            0,
        ),
        ("installed", "inactive", "inactive", "", "inactive", 0),
        ("installed", "failed", "inactive", "", "inactive", 0),
        (
            "installed",
            "inactive",
            "failed",
            "reset-failed telegram-kol-monitor.service\n",
            "inactive",
            0,
        ),
        (
            "installed",
            "inactive",
            "activating",
            "stop telegram-kol-monitor.service\n",
            "inactive",
            0,
        ),
        (
            "installed",
            "failed",
            "activating",
            "stop telegram-kol-monitor.service\n",
            "inactive",
            0,
        ),
        ("installed", "failed", "active", "", "failed", 1),
        ("installed", "failed", "deactivating", "", "failed", 1),
        ("installed", "failed", "unknown", "", "failed", 1),
        ("installed", "failed", "", "", "failed", 1),
        ("absent", "absent", "absent", "", "absent", 0),
    ],
)
def test_timer_frozen_monitor_convergence_is_closed(
    install_state,
    original_state,
    current_state,
    expected_log,
    expected_state,
    expected_status,
):
    block = _bound_close_read_only_block()
    function = block.split(
        "reset_bound_close_legacy_monitor_after_timer_freeze() {", 1
    )[1].split("\n}\n", 1)[0]
    function = function.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.service]}",
        "$INSTALL_STATE",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_STATE",
    ).replace(
        'ORIGINAL_UNIT_STATE["telegram-kol-monitor.service"]=inactive',
        "ORIGINAL_STATE=inactive",
    )
    script = f"""
set -euo pipefail
INSTALL_STATE={install_state}
ORIGINAL_STATE={original_state}
CURRENT_STATE={current_state}
systemctl() {{
  case "$1" in
    stop|reset-failed)
      printf '%s %s\\n' "$1" "$2"
      CURRENT_STATE=inactive
      ;;
    is-active) printf '%s\\n' "$CURRENT_STATE" ;;
    *) return 91 ;;
  esac
}}
sudo() {{ "$@"; }}
run_bound_close_external_command_before_deadline() {{ "$@"; }}
reset_bound_close_legacy_monitor_after_timer_freeze() {{
{function}
}}
set +e
reset_bound_close_legacy_monitor_after_timer_freeze
STATUS=$?
set -e
printf 'STATE=%s\\nSTATUS=%s\\n' "$ORIGINAL_STATE" "$STATUS"
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        expected_log + f"STATE={expected_state}\nSTATUS={expected_status}\n"
    )
    assert result.stderr == ""


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


def _bound_close_live_prequiescence_snippet() -> str:
    block = _bound_close_read_only_block()
    return block.split("# BEGIN BOUND_CLOSE_LIVE_PREQUIESCENCE", 1)[1].split(
        "# END BOUND_CLOSE_LIVE_PREQUIESCENCE", 1
    )[0]


def _bound_close_shell_function(block: str, name: str) -> str:
    return block.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]


def test_joint_window_requires_new_exact_approval_and_disclaims_mutation():
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    approval_text = section.split("```bash", 1)[0]
    block = _bound_close_read_only_block()
    joint_token = (
        "I_APPROVE_BOUND_CLOSE_BATCH119_ALL_DB_UNITS_STOPPED_"
        "JOINT_READ_ONLY_CAPTURE"
    )
    old_token = (
        "I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_"
        "READ_ONLY_DOUBLE_CAPTURE"
    )

    assert joint_token in approval_text
    assert joint_token in block
    assert old_token not in block
    assert "不授权 apply" in approval_text
    assert "不授权交易所写入" in approval_text
    assert "不授权部署" in approval_text
    assert "不授权启用 MiMo v2" in approval_text


def test_joint_window_takes_three_admissions_and_four_ordered_fresh_captures():
    block = _bound_close_read_only_block()
    live = _bound_close_shell_function(block, "run_joint_live_admissions")
    stopped = _bound_close_shell_function(block, "run_joint_stopped_phase")

    admission = _bound_close_shell_function(block, "run_joint_admission")
    assert "inspect-bound-close-batch119-joint-recovery" in admission
    assert live.count("run_joint_admission") == 2
    assert "sleep \"$JOINT_LIVE_POLL_SECONDS\"" in live
    assert live.index("joint-live-1.json") < live.index("sleep ")
    assert live.index("sleep ") < live.index("joint-live-2.json")
    assert live.count("compare_bound_close_batch119_joint_admissions.py") == 1

    assert stopped.count("run_joint_admission") == 1
    assert stopped.count("compare_bound_close_batch119_joint_admissions.py") == 1
    ordered = (
        "run_joint_batch119_capture 1",
        "run_joint_bound_close_capture 1",
        "run_joint_batch119_capture 2",
        "run_joint_bound_close_capture 2",
    )
    offsets = [stopped.index(command) for command in ordered]
    assert offsets == sorted(offsets)
    assert stopped.count("compare_batch119_dry_runs.py") == 1
    assert stopped.count("compare_bound_close_reservation_dry_runs.py") == 1
    assert "apply" not in stopped.lower()
    assert "notify" not in stopped.lower()


def test_joint_stopped_phase_reserves_complete_four_capture_budget():
    block = _bound_close_read_only_block()
    stopped = _bound_close_shell_function(block, "run_joint_stopped_phase")
    first_capture = stopped.index("run_joint_batch119_capture 1")
    admission = stopped.index("run_joint_admission")

    assert "JOINT_CAPTURE_ADMISSION_SECONDS=660" in block
    assert '"$REMAINING_SECONDS" -ge "$JOINT_CAPTURE_ADMISSION_SECONDS"' in stopped
    assert admission < stopped.index("JOINT_CAPTURE_ADMISSION_SECONDS", admission) < first_capture
    assert "QUIESCENCE_DEADLINE_EPOCH" in stopped
    assert "verify_all_local_quiescence_and_identity" in stopped


def test_joint_private_documents_are_0600_and_removed_by_existing_trap():
    block = _bound_close_read_only_block()
    live = _bound_close_shell_function(block, "run_joint_live_admissions")
    stopped = _bound_close_shell_function(block, "run_joint_stopped_phase")

    for filename in (
        "joint-live-1.json",
        "joint-live-2.json",
        "joint-post-stop.json",
        "batch119-1.json",
        "bound-close-1.json",
        "batch119-2.json",
        "bound-close-2.json",
    ):
        assert filename in live + stopped
    admission = _bound_close_shell_function(block, "run_joint_admission")
    batch_capture = _bound_close_shell_function(
        block, "run_joint_batch119_capture"
    )
    bound_capture = _bound_close_shell_function(
        block, "run_joint_bound_close_capture"
    )
    assert "chmod 0600" in admission
    assert batch_capture.count("chmod 0600") >= 2
    assert "chmod 0600" in bound_capture
    assert live.count("chmod 0600") >= 2
    assert stopped.count("chmod 0600") >= 4
    cleanup = _bound_close_shell_function(
        block, "restore_bound_close_reservation_units"
    )
    assert 'rm -rf -- "$RECOVERY_TMP"' in cleanup


def test_complete_joint_recovery_bash_block_is_syntax_valid(tmp_path):
    extracted = tmp_path / "joint-recovery-window.sh"
    extracted.write_text(_bound_close_read_only_block(), encoding="utf-8")

    result = subprocess.run(
        ["bash", "-n", str(extracted)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_joint_live_admissions_are_separated_and_compared_with_identity_checks(
    tmp_path,
):
    block = _bound_close_read_only_block()
    live = _bound_close_shell_function(block, "run_joint_live_admissions")
    events = tmp_path / "events.txt"
    runtime = tmp_path / "runtime"
    runtime.write_text(
        "#!/bin/bash\n"
        "printf 'compare:%s\\n' \"$(basename \"${1:-module}\")\" >> \"$EVENTS\"\n"
        "printf '{}\\n'\n",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    script = f"""
set -euo pipefail
umask 077
RECOVERY_TMP={shlex.quote(str(tmp_path))}
CANDIDATE_ROOT={shlex.quote(str(tmp_path))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
PRODUCTION_DB=/unused
JOINT_LIVE_ADMISSION_RESULT="$RECOVERY_TMP/result.json"
EVENTS={shlex.quote(str(events))}
export EVENTS
verify_all_local_identity_before_stop() {{ printf 'identity\n' >> "$EVENTS"; }}
run_joint_admission() {{
  printf 'admission:%s\n' "$(basename "$1")" >> "$EVENTS"
  printf '{{}}\n' > "$1"
  chmod 0600 "$1"
}}
sleep() {{ printf 'sleep:%s\n' "$1" >> "$EVENTS"; }}
run_joint_live_admissions() {{
{live}
}}
run_joint_live_admissions
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    lines = events.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "identity",
        "admission:joint-live-1.json",
        "identity",
        "sleep:15",
        "identity",
        "admission:joint-live-2.json",
        "identity",
        "compare:compare_bound_close_batch119_joint_admissions.py",
        "compare:project_bound_close_batch119_joint_output.py",
    ]


def test_joint_live_material_drift_refuses_before_any_unit_stop(tmp_path):
    block = _bound_close_read_only_block()
    live = _bound_close_shell_function(block, "run_joint_live_admissions")
    events = tmp_path / "events.txt"
    runtime = tmp_path / "runtime"
    runtime.write_text(
        "#!/bin/bash\n"
        "case \"$(basename \"${1:-module}\")\" in\n"
        "  compare_bound_close_batch119_joint_admissions.py) exit 2 ;;\n"
        "  *) printf '{}\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    script = f"""
set -euo pipefail
umask 077
RECOVERY_TMP={shlex.quote(str(tmp_path))}
CANDIDATE_ROOT={shlex.quote(str(tmp_path))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
PRODUCTION_DB=/unused
JOINT_LIVE_ADMISSION_RESULT="$RECOVERY_TMP/result.json"
EVENTS={shlex.quote(str(events))}
verify_all_local_identity_before_stop() {{ :; }}
run_joint_admission() {{
  printf 'admission\n' >> "$EVENTS"
  printf '{{}}\n' > "$1"
  chmod 0600 "$1"
}}
sleep() {{ :; }}
stop_bound_close_unit_group() {{ printf 'STOP\n' >> "$EVENTS"; }}
run_joint_live_admissions() {{
{live}
}}
run_joint_live_admissions
stop_bound_close_unit_group forbidden
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert events.read_text(encoding="utf-8") == "admission\nadmission\n"


@pytest.mark.parametrize(
    ("fail_at", "expected_captures"),
    [
        ("batch-1", ["batch-1"]),
        ("bound-1", ["batch-1", "bound-1"]),
        ("batch-2", ["batch-1", "bound-1", "batch-2"]),
    ],
)
def test_joint_stopped_capture_refusal_makes_later_captures_unreachable(
    tmp_path,
    fail_at,
    expected_captures,
):
    block = _bound_close_read_only_block()
    stopped = _bound_close_shell_function(block, "run_joint_stopped_phase")
    events = tmp_path / "events.txt"
    runtime = tmp_path / "runtime"
    runtime.write_text("#!/bin/bash\nprintf '{}\\n'\n", encoding="utf-8")
    runtime.chmod(0o700)
    (tmp_path / "joint-live-2.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "joint-live-2.json").chmod(0o600)
    script = f"""
set -euo pipefail
umask 077
RECOVERY_TMP={shlex.quote(str(tmp_path))}
CANDIDATE_ROOT={shlex.quote(str(tmp_path))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
QUIESCENCE_DEADLINE_EPOCH=9999999999
JOINT_CAPTURE_ADMISSION_SECONDS=660
EVENTS={shlex.quote(str(events))}
FAIL_AT={shlex.quote(fail_at)}
bound_close_now_epoch() {{ printf '1\n'; }}
sleep() {{ :; }}
verify_bound_close_quiescence() {{ :; }}
verify_all_local_quiescence_and_identity() {{ :; }}
run_joint_admission() {{ printf '{{}}\n' > "$1"; chmod 0600 "$1"; }}
run_joint_batch119_capture() {{
  printf 'batch-%s\n' "$1" >> "$EVENTS"
  [ "$FAIL_AT" != "batch-$1" ]
}}
run_joint_bound_close_capture() {{
  printf 'bound-%s\n' "$1" >> "$EVENTS"
  [ "$FAIL_AT" != "bound-$1" ]
}}
run_joint_stopped_phase() {{
{stopped}
}}
run_joint_stopped_phase
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert events.read_text(encoding="utf-8").splitlines() == expected_captures


def test_joint_post_stop_material_drift_makes_all_captures_unreachable(tmp_path):
    block = _bound_close_read_only_block()
    stopped = _bound_close_shell_function(block, "run_joint_stopped_phase")
    events = tmp_path / "events.txt"
    runtime = tmp_path / "runtime"
    runtime.write_text(
        "#!/bin/bash\n"
        "case \"$(basename \"${1:-module}\")\" in\n"
        "  compare_bound_close_batch119_joint_admissions.py) exit 2 ;;\n"
        "  *) printf '{}\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    (tmp_path / "joint-live-2.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "joint-live-2.json").chmod(0o600)
    script = f"""
set -euo pipefail
umask 077
RECOVERY_TMP={shlex.quote(str(tmp_path))}
CANDIDATE_ROOT={shlex.quote(str(tmp_path))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
QUIESCENCE_DEADLINE_EPOCH=9999999999
JOINT_CAPTURE_ADMISSION_SECONDS=660
EVENTS={shlex.quote(str(events))}
bound_close_now_epoch() {{ printf '1\n'; }}
sleep() {{ :; }}
verify_bound_close_quiescence() {{ :; }}
verify_all_local_quiescence_and_identity() {{ :; }}
run_joint_admission() {{ printf '{{}}\n' > "$1"; chmod 0600 "$1"; }}
run_joint_batch119_capture() {{ printf 'CAPTURE\n' >> "$EVENTS"; }}
run_joint_bound_close_capture() {{ printf 'CAPTURE\n' >> "$EVENTS"; }}
run_joint_stopped_phase() {{
{stopped}
}}
run_joint_stopped_phase
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert not events.exists()


def _bound_close_diagnostic_validator_function(block: str) -> str:
    body = block.split("validate_bound_close_capture_diagnostic() {", 1)[1]
    return body.split("\nPY\n}\n", 1)[0] + "\nPY"


def test_runbook_refused_capture_prints_diagnostic_only_after_restore(tmp_path):
    block = _bound_close_read_only_block()
    capture_function = _bound_close_shell_function(
        block, "run_bound_close_double_capture"
    )
    finish_function = _bound_close_shell_function(
        block, "finish_bound_close_reservation_window"
    )
    validator_function = _bound_close_diagnostic_validator_function(block)
    recovery_tmp = tmp_path / "recovery"
    recovery_tmp.mkdir()
    production_root = tmp_path / "production"
    production_root.mkdir()
    events = tmp_path / "events.txt"
    capture_document = tmp_path / "capture.json"
    capture_document.write_text(
        _document(
            started_at=START,
            observations=(
                _observation(
                    classification=ReservationClassification.UNKNOWN,
                    reason_code="exchange_history_incomplete",
                ),
            ),
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime-python"
    runtime.write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = -m ]; then
  printf 'capture-1\\n' >> "$EVENTS"
  cat "$CAPTURE_DOCUMENT"
  exit 2
fi
case "${1:-}" in
  -) exec "$REAL_PYTHON" "$@" ;;
  *project_bound_close_reservation_recovery_output.py)
    printf 'project-%s\\n' "${2:-missing}" >> "$EVENTS"
    exec "$REAL_PYTHON" "$@"
    ;;
  *compare_bound_close_reservation_dry_runs.py)
    printf 'compare\\n' >> "$EVENTS"
    exec "$REAL_PYTHON" "$@"
    ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    project_root = Path(__file__).resolve().parents[1]
    script = f"""
set -euo pipefail
export EVENTS={shlex.quote(str(events))}
export CAPTURE_DOCUMENT={shlex.quote(str(capture_document))}
export REAL_PYTHON={shlex.quote(sys.executable)}
RECOVERY_TMP={shlex.quote(str(recovery_tmp))}
CANDIDATE_ROOT={shlex.quote(str(project_root))}
PRODUCTION_ROOT={shlex.quote(str(production_root))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
PRODUCTION_DB={shlex.quote(str(tmp_path / 'production.db'))}
BOUND_CLOSE_SAFE_DIAGNOSTIC=''
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/diagnostic-handoff.json"
QUIESCENCE_DEADLINE_EPOCH=820
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
bound_close_now_epoch() {{ printf '100\n'; }}
verify_all_local_quiescence_and_identity() {{ :; }}
restore_bound_close_reservation_units() {{
  printf 'restore\\n'
  rm -rf -- "$RECOVERY_TMP"
}}
finish_bound_close_reservation_window() {{
{finish_function}
}}
validate_bound_close_capture_diagnostic() {{
{validator_function}
}}
run_bound_close_double_capture() {{
{capture_function}
}}
trap finish_bound_close_reservation_window EXIT
run_bound_close_double_capture
"""

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == (
        "restore\n"
        '{"action_count":0,"counts":{"active":0,"proven_terminal":0,'
        '"total":1,"unknown":1},"database_writes":0,"exchange_writes":0,'
        '"history_replays":0,"reason_counts":{"exchange_history_incomplete":1},'
        '"status":"refused"}\n'
    )
    assert events.read_text(encoding="utf-8") == (
        "capture-1\nproject-capture-diagnostic\n"
    )
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def _simulate_runbook_capture_failure(
    tmp_path: Path,
    *,
    capture_status: int,
    capture_document: str,
    diagnostic_status: int | None = None,
    diagnostic_output: str | None = None,
    diagnostic_stderr: str = "",
    validator_stderr: str = "",
    cleanup_status: int = 0,
    verify_fail_at: int | None = None,
    record_capture_cwd: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    block = _bound_close_read_only_block()
    capture_function = _bound_close_shell_function(
        block, "run_bound_close_double_capture"
    )
    finish_function = _bound_close_shell_function(
        block, "finish_bound_close_reservation_window"
    )
    validator_function = _bound_close_diagnostic_validator_function(block)
    recovery_tmp = tmp_path / "recovery"
    recovery_tmp.mkdir()
    events = tmp_path / "events.txt"
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(capture_document, encoding="utf-8")
    diagnostic_path = tmp_path / "diagnostic.json"
    diagnostic_path.write_text(diagnostic_output or "", encoding="utf-8")
    verify_counter = tmp_path / "verify-counter.txt"
    verify_counter.write_text("0", encoding="utf-8")
    production_root = tmp_path / "production"
    (production_root / "config").mkdir(parents=True)
    (production_root / "config" / "telegram.env").write_text(
        "DEEPCOIN_API_KEY=test-only\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime-python"
    diagnostic_branch = (
        "cat \"$DIAGNOSTIC_OUTPUT\"\n"
        f"    exit {diagnostic_status}"
        if diagnostic_status is not None
        else 'exec "$REAL_PYTHON" "$@"'
    )
    runtime.write_text(
        f"""#!/bin/bash
set -euo pipefail
if [ "${{1:-}}" = -m ]; then
  if [ "$RECORD_CAPTURE_CWD" = 1 ]; then
    printf 'capture-cwd=%s\\n' "$PWD" >> "$EVENTS"
  fi
  printf 'capture-1\\n' >> "$EVENTS"
  cat "$CAPTURE_DOCUMENT"
  exit {capture_status}
fi
case "${{1:-}}" in
  -)
    printf '%s' "$VALIDATOR_STDERR" >&2
    exec "$REAL_PYTHON" "$@"
    ;;
  *project_bound_close_reservation_recovery_output.py)
    printf 'project-%s\\n' "${{2:-missing}}" >> "$EVENTS"
    printf '%s' "$DIAGNOSTIC_STDERR" >&2
    {diagnostic_branch}
    ;;
  *compare_bound_close_reservation_dry_runs.py)
    printf 'compare\\n' >> "$EVENTS"
    exec "$REAL_PYTHON" "$@"
    ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    project_root = Path(__file__).resolve().parents[1]
    script = f"""
set -euo pipefail
export EVENTS={shlex.quote(str(events))}
export CAPTURE_DOCUMENT={shlex.quote(str(capture_path))}
export DIAGNOSTIC_OUTPUT={shlex.quote(str(diagnostic_path))}
export DIAGNOSTIC_STDERR={shlex.quote(diagnostic_stderr)}
export VALIDATOR_STDERR={shlex.quote(validator_stderr)}
export VERIFY_COUNTER={shlex.quote(str(verify_counter))}
export RECORD_CAPTURE_CWD={1 if record_capture_cwd else 0}
export REAL_PYTHON={shlex.quote(sys.executable)}
RECOVERY_TMP={shlex.quote(str(recovery_tmp))}
CANDIDATE_ROOT={shlex.quote(str(project_root))}
PRODUCTION_ROOT={shlex.quote(str(production_root))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
PRODUCTION_DB={shlex.quote(str(tmp_path / 'production.db'))}
BOUND_CLOSE_SAFE_DIAGNOSTIC=''
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/diagnostic-handoff.json"
QUIESCENCE_DEADLINE_EPOCH=820
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
bound_close_now_epoch() {{ printf '100\n'; }}
verify_all_local_quiescence_and_identity() {{
  count="$(cat "$VERIFY_COUNTER")"
  count=$((count + 1))
  printf '%s' "$count" > "$VERIFY_COUNTER"
  if [ "$count" -eq {verify_fail_at if verify_fail_at is not None else -1} ]; then
    return 7
  fi
}}
restore_bound_close_reservation_units() {{
  printf 'restore\\n'
  rm -rf -- "$RECOVERY_TMP"
  return {cleanup_status}
}}
finish_bound_close_reservation_window() {{
{finish_function}
}}
validate_bound_close_capture_diagnostic() {{
{validator_function}
}}
run_bound_close_double_capture() {{
{capture_function}
}}
trap finish_bound_close_reservation_window EXIT
run_bound_close_double_capture
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, events, recovery_tmp


def test_runbook_capture_executes_cli_from_production_root(tmp_path):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document=_document(
            started_at=START,
            observations=(
                _observation(
                    classification=ReservationClassification.UNKNOWN,
                    reason_code="exchange_history_incomplete",
                ),
            ),
        ),
        record_capture_cwd=True,
    )

    assert result.returncode == 2
    assert events.read_text(encoding="utf-8").splitlines()[0] == (
        f"capture-cwd={tmp_path / 'production'}"
    )
    assert result.stderr == ""
    assert not recovery_tmp.exists()


def test_runbook_validates_closed_production_deepcoin_environment_before_stop():
    block = _bound_close_read_only_block()
    config_path = 'PRODUCTION_DEEPCOIN_ENV="$PRODUCTION_ROOT/config/telegram.env"'
    config_regular = 'test -f "$PRODUCTION_DEEPCOIN_ENV"'
    config_symlink = 'test ! -L "$PRODUCTION_DEEPCOIN_ENV"'
    config_owner_mode = (
        'test "$(stat -Lc \'%u:%a\' -- "$PRODUCTION_DEEPCOIN_ENV")" = \'0:600\''
    )
    no_shadow = 'test ! -e "$PRODUCTION_ROOT/.env"'
    no_ambient = "if env | grep '^DEEPCOIN_' >/dev/null; then"
    stop_boundary = block.index("QUIESCE_ATTEMPTED=1")

    for required in (
        config_path,
        config_regular,
        config_symlink,
        config_owner_mode,
        no_shadow,
        no_ambient,
    ):
        assert required in block
        assert block.index(required) < stop_boundary


def test_runbook_rejects_ambient_deepcoin_in_large_environment_before_stop():
    block = _bound_close_read_only_block()
    guard_start = block.index("if env | grep")
    guard_end = block.index("\nfi", guard_start) + len("\nfi")
    guard = block[guard_start:guard_end]
    environment = {
        "DEEPCOIN_API_KEY": "test-only-never-print",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    environment.update(
        {f"BOUND_CLOSE_PADDING_{index:04d}": "x" * 512 for index in range(400)}
    )

    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{guard}\nprintf 'incorrectly-allowed\\n'"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


def test_every_bound_close_recovery_cli_runs_from_production_root():
    project_root = Path(__file__).resolve().parents[1]
    runbook = (project_root / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    command = (
        "telegram_kol_research.cli recover-bound-position-close-reservations"
    )

    assert section.count(command) == 4
    assert section.count('cd "$PRODUCTION_ROOT"') == section.count(command)


@pytest.mark.parametrize(
    ("capture_status", "document"),
    [
        (
            0,
            lambda: _document(
                started_at=START,
                observations=(
                    _observation(
                        classification=ReservationClassification.UNKNOWN,
                        reason_code="exchange_history_incomplete",
                    ),
                ),
            ),
        ),
        (2, lambda: _document(started_at=START)),
    ],
)
def test_runbook_capture_exit_and_document_status_mismatch_is_unavailable(
    tmp_path,
    capture_status,
    document,
):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=capture_status,
        capture_document=document(),
    )

    assert result.returncode != 0
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert "capture-1\n" in events.read_text(encoding="utf-8")
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()
    assert result.stderr == ""


@pytest.mark.parametrize("capture_status", [1, 3, 126, 127, 129, 130, 143])
def test_runbook_unexpected_capture_exit_is_diagnostic_unavailable(
    tmp_path,
    capture_status,
):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=capture_status,
        capture_document="{",
    )

    assert result.returncode != 0
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert events.read_text(encoding="utf-8").startswith("capture-1\n")
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def test_runbook_projector_failure_is_diagnostic_unavailable(tmp_path):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document="TOPSECRET-RAW-CAPTURE",
        diagnostic_status=2,
        diagnostic_output='{"status":"diagnostic_unavailable"}\n',
    )

    assert result.returncode != 0
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert "TOPSECRET" not in result.stdout
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def test_runbook_projector_stderr_never_reaches_operator(tmp_path):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document="TOPSECRET-RAW-CAPTURE",
        diagnostic_status=2,
        diagnostic_output='{"status":"diagnostic_unavailable"}\n',
        diagnostic_stderr="TOPSECRET-PATH-OR-TRACEBACK",
    )

    assert result.returncode != 0
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert "TOPSECRET" not in result.stdout
    assert result.stderr == ""
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()


def test_runbook_validator_stderr_never_reaches_operator(tmp_path):
    refused = _document(
        started_at=START,
        observations=(
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    )
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document=refused,
        validator_stderr="TOPSECRET-VALIDATOR-TRACEBACK",
    )

    assert result.returncode == 2
    assert result.stdout.endswith('"status":"refused"}\n')
    assert "TOPSECRET" not in result.stdout
    assert result.stderr == ""
    assert events.read_text(encoding="utf-8") == (
        "capture-1\nproject-capture-diagnostic\n"
    )
    assert not recovery_tmp.exists()


def test_runbook_second_attempt_failure_clears_first_ready_diagnostic(tmp_path):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=0,
        capture_document=_document(started_at=START),
        verify_fail_at=3,
    )

    assert result.returncode == 7
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert events.read_text(encoding="utf-8") == (
        "capture-1\nproject-capture-diagnostic\nproject-capture\n"
    )
    assert result.stderr == ""
    assert not recovery_tmp.exists()


@pytest.mark.parametrize(
    "diagnostic_output",
    [
        "",
        "{",
        '{"status":"refused","provider_row":"TOPSECRET"}\n',
        '{"status":"refused"}' + (" " * 20_000),
    ],
)
def test_runbook_invalid_projector_success_output_is_unavailable(
    tmp_path,
    diagnostic_output,
):
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document="TOPSECRET-RAW-CAPTURE",
        diagnostic_status=0,
        diagnostic_output=diagnostic_output,
    )

    assert result.returncode != 0
    assert result.stdout == 'restore\n{"status":"diagnostic_unavailable"}\n'
    assert "TOPSECRET" not in result.stdout
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def test_runbook_cleanup_failure_stays_nonzero_after_safe_refusal(tmp_path):
    refused = _document(
        started_at=START,
        observations=(
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    )
    result, events, recovery_tmp = _simulate_runbook_capture_failure(
        tmp_path,
        capture_status=2,
        capture_document=refused,
        cleanup_status=7,
    )

    assert result.returncode == 7
    assert result.stdout.startswith("restore\n")
    assert result.stdout.endswith('"status":"refused"}\n')
    assert "compare\n" not in events.read_text(encoding="utf-8")
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def test_runbook_two_ready_captures_still_compare_once_and_restore(tmp_path):
    block = _bound_close_read_only_block()
    capture_function = _bound_close_shell_function(
        block, "run_bound_close_double_capture"
    )
    finish_function = _bound_close_shell_function(
        block, "finish_bound_close_reservation_window"
    )
    validator_function = _bound_close_diagnostic_validator_function(block)
    recovery_tmp = tmp_path / "recovery"
    recovery_tmp.mkdir()
    production_root = tmp_path / "production"
    production_root.mkdir()
    events = tmp_path / "events.txt"
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(_document(started_at=START), encoding="utf-8")
    second.write_text(
        _document(started_at=START + timedelta(minutes=2)), encoding="utf-8"
    )
    runtime = tmp_path / "runtime-python"
    runtime.write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = -m ]; then
  count="$(cat "$CAPTURE_COUNTER")"
  count=$((count + 1))
  printf '%s' "$count" > "$CAPTURE_COUNTER"
  printf 'capture-%s\n' "$count" >> "$EVENTS"
  case "$count" in
    1) cat "$CAPTURE_DOCUMENT_1" ;;
    2) cat "$CAPTURE_DOCUMENT_2" ;;
    *) exit 92 ;;
  esac
  exit 0
fi
case "${1:-}" in
  -) exec "$REAL_PYTHON" "$@" ;;
  *project_bound_close_reservation_recovery_output.py)
    printf 'project-%s\n' "${2:-missing}" >> "$EVENTS"
    exec "$REAL_PYTHON" "$@"
    ;;
  *compare_bound_close_reservation_dry_runs.py)
    printf 'compare\n' >> "$EVENTS"
    exec "$REAL_PYTHON" "$@"
    ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    runtime.chmod(0o700)
    project_root = Path(__file__).resolve().parents[1]
    script = f"""
set -euo pipefail
export EVENTS={shlex.quote(str(events))}
export CAPTURE_COUNTER={shlex.quote(str(counter))}
export CAPTURE_DOCUMENT_1={shlex.quote(str(first))}
export CAPTURE_DOCUMENT_2={shlex.quote(str(second))}
export REAL_PYTHON={shlex.quote(sys.executable)}
RECOVERY_TMP={shlex.quote(str(recovery_tmp))}
CANDIDATE_ROOT={shlex.quote(str(project_root))}
PRODUCTION_ROOT={shlex.quote(str(production_root))}
RUNTIME_PYTHON={shlex.quote(str(runtime))}
PRODUCTION_DB={shlex.quote(str(tmp_path / 'production.db'))}
BOUND_CLOSE_SAFE_DIAGNOSTIC=''
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/diagnostic-handoff.json"
QUIESCENCE_DEADLINE_EPOCH=820
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
bound_close_now_epoch() {{ printf '100\n'; }}
verify_all_local_quiescence_and_identity() {{ :; }}
restore_bound_close_reservation_units() {{
  printf 'restore\\n'
  rm -rf -- "$RECOVERY_TMP"
}}
finish_bound_close_reservation_window() {{
{finish_function}
}}
validate_bound_close_capture_diagnostic() {{
{validator_function}
}}
run_bound_close_double_capture() {{
{capture_function}
}}
trap finish_bound_close_reservation_window EXIT
run_bound_close_double_capture
"""

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('{"status":"stable"}\nrestore\n')
    assert result.stdout.endswith('"status":"ready"}\n')
    assert events.read_text(encoding="utf-8") == (
        "capture-1\n"
        "project-capture-diagnostic\n"
        "project-capture\n"
        "capture-2\n"
        "project-capture-diagnostic\n"
        "project-capture\n"
        "compare\n"
    )
    assert not recovery_tmp.exists()
    assert result.stderr == ""


def test_runbook_stopped_recheck_is_bounded_and_capture_is_ready_gated():
    block = _bound_close_read_only_block()

    first_stop = block.index("QUIESCE_ATTEMPTED=1")
    deadline = block.rfind(
        'QUIESCENCE_DEADLINE_EPOCH="$(( $(bound_close_now_epoch) + 720 ))"',
        0,
        first_stop,
    )
    phase = _bound_close_poll_snippet()
    helper = phase.index("run_bound_close_writer_quiescence_helper")
    capture = phase.index("run_bound_close_double_capture", helper)
    assert deadline >= 0
    assert "QUIESCENCE_POLL_SECONDS" not in phase
    assert "sleep " not in phase
    assert "sleep 600" not in block
    assert "sleep 10m" not in block
    assert helper < capture
    assert 'case "$HELPER_STATUS" in' in phase[helper:capture]
    assert "0)" in phase[helper:capture]
    assert "2)" in phase[helper:capture]
    assert "*) return 1" in phase[helper:capture]
    assert "require_exact_ready_projection" in phase[helper:capture]
    assert "require_exact_refused_projection" in phase[helper:capture]


def test_post_stop_writer_check_is_single_shot_without_aging_sleep():
    stopped = _bound_close_poll_snippet()

    assert "while :" not in stopped
    assert "sleep " not in stopped
    assert stopped.count("run_bound_close_writer_quiescence_helper") == 1
    assert stopped.count("run_bound_close_double_capture_before_deadline") == 1
    assert stopped.index("run_bound_close_writer_quiescence_helper") < stopped.index(
        "run_bound_close_double_capture_before_deadline"
    )


def test_live_prequiescence_precedes_runner_build_and_every_unit_stop():
    block = _bound_close_read_only_block()

    live_start = block.index("run_joint_live_admissions() {")
    live_end = block.index("\n}\n", live_start)
    runner_build = block.index(
        'write_joint_stopped_runner "$STOPPED_PHASE_RUNNER"'
    )
    first_stop = block.index("QUIESCE_ATTEMPTED=1")

    assert live_start < live_end < runner_build < first_stop
    live = block[live_start:live_end]
    assert "$(( $(bound_close_now_epoch) + 720 ))" in block[live_end:runner_build]
    assert "local JOINT_LIVE_POLL_SECONDS=15" in live
    assert "verify_all_local_identity_before_stop" in live
    assert "verify_all_local_quiescence_and_identity" not in live
    assert "stop_bound_close_unit_group" not in live
    assert "run_joint_batch119_capture" not in live


def test_live_and_stopped_writer_stages_share_process_group_hard_deadline():
    block = _bound_close_read_only_block()

    live_build = block.index(
        'write_joint_live_runner "$LIVE_PREQUIESCENCE_RUNNER"'
    )
    live_run = block.index(
        'run_bound_close_runner_before_deadline \\\n  "$LIVE_PREQUIESCENCE_RUNNER"',
        live_build,
    )
    first_stop = block.index("QUIESCE_ATTEMPTED=1")
    stopped_build = block.index(
        'write_joint_stopped_runner "$STOPPED_PHASE_RUNNER"'
    )
    stopped_run = block.index(
        'run_bound_close_runner_before_deadline \\\n  "$STOPPED_PHASE_RUNNER"',
        first_stop,
    )

    assert live_build < live_run < stopped_build < first_stop < stopped_run
    deadline_runner = _bound_close_shell_function(
        block, "run_bound_close_runner_before_deadline"
    )
    assert "timeout --signal=KILL" in deadline_runner
    assert "--kill-after" not in deadline_runner


def test_process_group_deadline_interrupts_blocked_writer_stage(tmp_path):
    block = _bound_close_read_only_block()
    deadline_runner = _bound_close_shell_function(
        block, "run_bound_close_runner_before_deadline"
    )
    events = tmp_path / "events.txt"
    runner = tmp_path / "blocked-runner.sh"
    timeout_command = tmp_path / "timeout"
    timeout_command.write_text(
        f"""#!{sys.executable}
import os
import signal
import subprocess
import sys

arguments = sys.argv[1:]
while arguments and arguments[0].startswith("--"):
    arguments.pop(0)
duration = float(arguments.pop(0))
process = subprocess.Popen(arguments, start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=duration))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    raise SystemExit(124)
""",
        encoding="utf-8",
    )
    timeout_command.chmod(0o700)
    runner.write_text(
        f"#!/bin/bash\ntrap '' TERM\nprintf 'started\\n' >> {shlex.quote(str(events))}\nsleep 5\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    script = f"""
set -euo pipefail
PATH={shlex.quote(str(tmp_path))}:$PATH
RUNNER={shlex.quote(str(runner))}
EVENTS={shlex.quote(str(events))}
bound_close_now_epoch() {{ date +%s; }}
restore() {{ printf 'restore\n' >> "$EVENTS"; }}
run_bound_close_runner_before_deadline() {{
{deadline_runner}
}}
trap restore EXIT
run_bound_close_runner_before_deadline "$RUNNER" "$(( $(date +%s) + 2 ))"
"""

    started_at = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=4,
    )

    assert result.returncode != 0
    assert time.monotonic() - started_at < 4
    assert events.read_text(encoding="utf-8") == "started\nrestore\n"


def test_stopped_deadline_interrupts_blocked_initial_quiescence_check(tmp_path):
    block = _bound_close_read_only_block()
    stopped_phase = _bound_close_shell_function(block, "run_bound_close_stopped_phase")
    deadline_runner = _bound_close_shell_function(
        block, "run_bound_close_runner_before_deadline"
    )
    events = tmp_path / "events.txt"
    runner = tmp_path / "stopped-runner.sh"
    timeout_command = tmp_path / "timeout"
    timeout_command.write_text(
        f"""#!{sys.executable}
import os
import signal
import subprocess
import sys

arguments = sys.argv[1:]
while arguments and arguments[0].startswith("--"):
    arguments.pop(0)
duration = float(arguments.pop(0))
process = subprocess.Popen(arguments, start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=duration))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    raise SystemExit(124)
""",
        encoding="utf-8",
    )
    timeout_command.chmod(0o700)
    runner.write_text(
        f"""#!/bin/bash
set -euo pipefail
EVENTS={shlex.quote(str(events))}
QUIESCENCE_DEADLINE_EPOCH="$1"
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
RECOVERY_TMP={shlex.quote(str(tmp_path))}
WRITER_QUIESCENCE_RAW="$RECOVERY_TMP/raw.json"
WRITER_QUIESCENCE_PROJECTION="$RECOVERY_TMP/projection.json"
WRITER_QUIESCENCE_RESULT="$RECOVERY_TMP/result.json"
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/diagnostic.json"
bound_close_now_epoch() {{ date +%s; }}
sleep() {{ command sleep "$@"; }}
verify_bound_close_quiescence() {{
  printf 'verify-start\n' >> "$EVENTS"
  sleep 5
}}
verify_all_local_quiescence_and_identity() {{ :; }}
run_bound_close_writer_quiescence_helper() {{ return 91; }}
project_bound_close_writer_quiescence_result() {{ return 91; }}
require_exact_ready_projection() {{ return 91; }}
require_exact_refused_projection() {{ return 91; }}
run_bound_close_double_capture_before_deadline() {{ return 91; }}
run_bound_close_stopped_phase() {{
{stopped_phase}
}}
run_bound_close_stopped_phase
""",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    script = f"""
set -euo pipefail
PATH={shlex.quote(str(tmp_path))}:$PATH
EVENTS={shlex.quote(str(events))}
RUNNER={shlex.quote(str(runner))}
bound_close_now_epoch() {{ date +%s; }}
restore() {{ printf 'restore\n' >> "$EVENTS"; }}
run_bound_close_runner_before_deadline() {{
{deadline_runner}
}}
trap restore EXIT
printf 'stop\n' >> "$EVENTS"
run_bound_close_runner_before_deadline "$RUNNER" "$(( $(date +%s) + 2 ))"
"""

    started_at = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=4,
    )

    assert result.returncode != 0
    assert time.monotonic() - started_at < 4
    assert events.read_text(encoding="utf-8") == "stop\nverify-start\nrestore\n"


def test_stopped_deadline_is_created_before_first_unit_stop():
    block = _bound_close_read_only_block()
    deadline = block.index(
        'QUIESCENCE_DEADLINE_EPOCH="$(( $(bound_close_now_epoch) + 720 ))"'
    )
    first_stop = block.index("QUIESCE_ATTEMPTED=1")

    assert deadline < first_stop


@pytest.mark.parametrize(
    ("install_state", "original_state", "current_load", "current_state", "expected"),
    [
        ("installed", "active", "loaded", "active", 0),
        ("installed", "active", "loaded", "inactive", 1),
        ("installed", "inactive", "not-found", "inactive", 1),
        ("absent", "absent", "not-found", "inactive", 0),
        ("absent", "absent", "loaded", "inactive", 1),
    ],
)
def test_original_unit_state_verifier_rejects_pre_stop_drift(
    install_state,
    original_state,
    current_load,
    current_state,
    expected,
):
    block = _bound_close_read_only_block()
    verifier = _bound_close_shell_function(
        block, "verify_bound_close_unit_group_original_state"
    )
    verifier = verifier.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[$unit]}", "$INSTALL_STATE"
    ).replace("${ORIGINAL_UNIT_STATE[$unit]}", "$ORIGINAL_STATE")
    script = f"""
set -euo pipefail
INSTALL_STATE={install_state}
ORIGINAL_STATE={original_state}
CURRENT_LOAD={current_load}
CURRENT_STATE={current_state}
systemctl() {{
  case "$1" in
    show) printf '%s\n' "$CURRENT_LOAD" ;;
    is-active) printf '%s\n' "$CURRENT_STATE" ;;
    *) return 91 ;;
  esac
}}
verify_bound_close_unit_group_original_state() {{
{verifier}
}}
verify_bound_close_unit_group_original_state example.service
"""

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    ("monitor_load", "monitor_state", "timer_load", "timer_state", "expected"),
    [
        ("loaded", "failed", "loaded", "active", 0),
        ("loaded", "activating", "loaded", "active", 0),
        ("loaded", "inactive", "loaded", "active", 0),
        ("loaded", "active", "loaded", "active", 1),
        ("loaded", "deactivating", "loaded", "active", 1),
        ("loaded", "unknown", "loaded", "active", 1),
        ("not-found", "failed", "loaded", "active", 1),
        ("loaded", "failed", "loaded", "inactive", 1),
        ("loaded", "failed", "not-found", "inactive", 1),
    ],
)
@pytest.mark.parametrize("original_monitor_state", ["failed", "inactive"])
def test_legacy_monitor_live_state_exception_is_closed(
    monitor_load,
    monitor_state,
    timer_load,
    timer_state,
    expected,
    original_monitor_state,
):
    block = _bound_close_read_only_block()
    verifier = _bound_close_shell_function(
        block, "verify_bound_close_legacy_monitor_live_state"
    )
    verifier = verifier.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_STATE",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_STATE",
    )
    script = f"""
set -euo pipefail
ORIGINAL_MONITOR_INSTALL=installed
ORIGINAL_TIMER_INSTALL=installed
ORIGINAL_MONITOR_STATE={original_monitor_state}
ORIGINAL_TIMER_STATE=active
MONITOR_LOAD={monitor_load}
MONITOR_STATE={monitor_state}
TIMER_LOAD={timer_load}
TIMER_STATE={timer_state}
systemctl() {{
  local unit="$2"
  case "$1:$unit" in
    show:telegram-kol-monitor.service) printf '%s\n' "$MONITOR_LOAD" ;;
    is-active:telegram-kol-monitor.service) printf '%s\n' "$MONITOR_STATE" ;;
    show:telegram-kol-monitor.timer) printf '%s\n' "$TIMER_LOAD" ;;
    is-active:telegram-kol-monitor.timer) printf '%s\n' "$TIMER_STATE" ;;
    *) return 91 ;;
  esac
}}
verify_bound_close_legacy_monitor_live_state() {{
{verifier}
}}
verify_bound_close_legacy_monitor_live_state
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    (
        "original_monitor_install",
        "original_monitor_state",
        "original_timer_install",
        "original_timer_state",
    ),
    [
        ("absent", "absent", "installed", "active"),
        ("installed", "active", "installed", "active"),
        ("installed", "failed", "absent", "absent"),
        ("installed", "failed", "installed", "inactive"),
    ],
)
def test_legacy_monitor_live_state_exception_requires_approved_original_snapshot(
    original_monitor_install,
    original_monitor_state,
    original_timer_install,
    original_timer_state,
):
    block = _bound_close_read_only_block()
    verifier = _bound_close_shell_function(
        block, "verify_bound_close_legacy_monitor_live_state"
    )
    verifier = verifier.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_STATE",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_STATE",
    )
    script = f"""
set -euo pipefail
ORIGINAL_MONITOR_INSTALL={original_monitor_install}
ORIGINAL_TIMER_INSTALL={original_timer_install}
ORIGINAL_MONITOR_STATE={original_monitor_state}
ORIGINAL_TIMER_STATE={original_timer_state}
systemctl() {{
  case "$1:$2" in
    show:*) printf 'loaded\n' ;;
    is-active:telegram-kol-monitor.service) printf 'failed\n' ;;
    is-active:telegram-kol-monitor.timer) printf 'active\n' ;;
    *) return 91 ;;
  esac
}}
verify_bound_close_legacy_monitor_live_state() {{
{verifier}
}}
verify_bound_close_legacy_monitor_live_state
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, result.stderr


@pytest.mark.parametrize(
    ("core_load", "core_state", "socket_load", "expected"),
    [
        ("loaded", "active", "loaded", 0),
        ("loaded", "inactive", "loaded", 1),
        ("loaded", "active", "not-found", 1),
    ],
)
def test_live_unit_verifier_keeps_core_and_install_states_exact(
    core_load, core_state, socket_load, expected
):
    block = _bound_close_read_only_block()
    live_verifier = _bound_close_shell_function(
        block, "verify_bound_close_unit_group_live_state"
    )
    live_verifier = live_verifier.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[$unit]}", "$MONITOR_INSTALL"
    )
    script = f"""
set -euo pipefail
MONITOR_INSTALL=installed
CORE_LOAD={core_load}
CORE_STATE={core_state}
SOCKET_LOAD={socket_load}
verify_bound_close_legacy_monitor_live_state() {{ return 0; }}
verify_bound_close_unit_group_original_state() {{
  case "$1" in
    telegram-kol-monitor.timer) return 0 ;;
    telegram-kol.service)
      [ "$CORE_LOAD" = loaded ] && [ "$CORE_STATE" = active ]
      ;;
    telegram-kol-agent-model-egress.socket)
      [ "$SOCKET_LOAD" = loaded ]
      ;;
    *) return 91 ;;
  esac
}}
verify_bound_close_unit_group_live_state() {{
{live_verifier}
}}
verify_bound_close_unit_group_live_state \
  telegram-kol-monitor.service telegram-kol-monitor.timer \
  telegram-kol.service telegram-kol-agent-model-egress.socket
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == expected, result.stderr


def test_live_identity_verifier_checks_all_original_unit_states():
    block = _bound_close_read_only_block()
    verifier = _bound_close_shell_function(block, "verify_all_local_identity_before_stop")

    assert 'verify_bound_close_unit_group_live_state "${QUIESCE_UNITS[@]}"' in verifier


def test_original_unit_state_is_rechecked_immediately_before_first_stop():
    block = _bound_close_read_only_block()
    stopped_runner = block.index(
        'write_joint_stopped_runner "$STOPPED_PHASE_RUNNER"'
    )
    first_stop = block.index("QUIESCE_ATTEMPTED=1")
    final_verify = block.rfind(
        "verify_all_local_identity_before_stop", stopped_runner, first_stop
    )

    assert stopped_runner < final_verify < first_stop


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
QUIESCENCE_DEADLINE_EPOCH=820
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/capture-diagnostic-handoff.json"
WRITER_QUIESCENCE_RAW="$RECOVERY_TMP/writer-quiescence-raw.json"
WRITER_QUIESCENCE_PROJECTION="$RECOVERY_TMP/writer-quiescence-projection.json"
WRITER_QUIESCENCE_RESULT="$RECOVERY_TMP/writer-quiescence.json"
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
run_bound_close_double_capture_before_deadline() {{
  printf 'capture\n' >> "$EVENTS"
}}
"""
    script = (
        prefix
        + "\nrun_bound_close_stopped_phase() {\n"
        + _bound_close_poll_snippet()
        + "\n}\nrun_bound_close_stopped_phase\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _simulate_live_prequiescence(
    tmp_path: Path,
    *,
    helper_statuses: tuple[int, ...],
    sleep_jump: int,
    projection_fails: bool = False,
    verify_fail_on: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    events = tmp_path / "live-events.txt"
    sequence = " ".join(str(value) for value in helper_statuses)
    script = f"""
set -euo pipefail
EVENTS={shlex.quote(str(events))}
RECOVERY_TMP={shlex.quote(str(tmp_path))}
NOW_EPOCH=100
LIVE_PREQUIESCENCE_DEADLINE_EPOCH=820
LIVE_PREQUIESCENCE_RESULT="$RECOVERY_TMP/live-writer-quiescence.json"
LIVE_PREQUIESCENCE_ATTEMPT=0
LIVE_PREQUIESCENCE_POLL_SECONDS=15
LIVE_PREQUIESCENCE_PROJECTION=''
LIVE_PREQUIESCENCE_RAW=''
LIVE_PREQUIESCENCE_REMAINING_SECONDS=0
LIVE_PREQUIESCENCE_SLEEP_SECONDS=0
HELPER_SEQUENCE=({sequence})
HELPER_CALL=0
VERIFY_CALL=0
PROJECTION_FAIL={int(projection_fails)}
bound_close_now_epoch() {{ printf '%s\n' "$NOW_EPOCH"; }}
sleep() {{
  printf 'sleep\n' >> "$EVENTS"
  NOW_EPOCH=$((NOW_EPOCH + {sleep_jump}))
}}
verify_all_local_identity_before_stop() {{
  VERIFY_CALL=$((VERIFY_CALL + 1))
  printf 'verify\n' >> "$EVENTS"
  [ "$VERIFY_CALL" -ne {verify_fail_on} ]
}}
run_bound_close_writer_quiescence_helper() {{
  local output_path="$1"
  local index="$HELPER_CALL"
  HELPER_CALL=$((HELPER_CALL + 1))
  HELPER_STATUS="${{HELPER_SEQUENCE[$index]:-1}}"
  printf 'helper\n' >> "$EVENTS"
  if [ "$HELPER_STATUS" -eq 0 ]; then
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":0,"checked_table_count":20,"fresh_active_or_unknown_writer_count":0,"historical_active_or_unknown_residue_count":2,"missing_table_count":0,"schema_version":1,"status":"ready","target_reservation_count":29,"unrecognized_or_null_state_count":0}}' > "$output_path"
  else
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":1,"checked_table_count":20,"fresh_active_or_unknown_writer_count":1,"historical_active_or_unknown_residue_count":1,"missing_table_count":0,"schema_version":1,"status":"refused","target_reservation_count":29,"unrecognized_or_null_state_count":0}}' > "$output_path"
  fi
}}
project_bound_close_writer_quiescence_result() {{
  printf 'project\n' >> "$EVENTS"
  [ "$PROJECTION_FAIL" -eq 0 ]
  cp "$1" "$3"
}}
require_exact_ready_projection() {{ grep -q '"status":"ready"' "$1"; }}
require_exact_refused_projection() {{ grep -q '"status":"refused"' "$1"; }}
stop_bound_close_unit_group() {{ printf 'stop\n' >> "$EVENTS"; }}
run_bound_close_live_prequiescence() {{
{_bound_close_live_prequiescence_snippet()}
}}
run_bound_close_live_prequiescence
printf 'live-ready\n' >> "$EVENTS"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, events


def _simulate_live_monitor_state_transitions(
    tmp_path: Path,
    *,
    monitor_states: tuple[str, ...],
    timer_drift_on: int = 0,
    core_drift_on: int = 0,
    install_drift_on: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    block = _bound_close_read_only_block()
    legacy_verifier = _bound_close_shell_function(
        block, "verify_bound_close_legacy_monitor_live_state"
    )
    legacy_verifier = legacy_verifier.replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_INSTALL",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}",
        "$ORIGINAL_MONITOR_STATE",
    ).replace(
        "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.timer]}",
        "$ORIGINAL_TIMER_STATE",
    )
    events = tmp_path / "live-monitor-events.txt"
    states = " ".join(shlex.quote(value) for value in monitor_states)
    script = f"""
set -euo pipefail
EVENTS={shlex.quote(str(events))}
RECOVERY_TMP={shlex.quote(str(tmp_path))}
NOW_EPOCH=100
LIVE_PREQUIESCENCE_DEADLINE_EPOCH=820
LIVE_PREQUIESCENCE_RESULT="$RECOVERY_TMP/live-writer-quiescence.json"
LIVE_PREQUIESCENCE_ATTEMPT=0
LIVE_PREQUIESCENCE_POLL_SECONDS=15
LIVE_PREQUIESCENCE_PROJECTION=''
LIVE_PREQUIESCENCE_RAW=''
LIVE_PREQUIESCENCE_REMAINING_SECONDS=0
LIVE_PREQUIESCENCE_SLEEP_SECONDS=0
HELPER_SEQUENCE=(2 2 0)
HELPER_CALL=0
VERIFY_CALL=0
MONITOR_STATES=({states})
TIMER_DRIFT_ON={timer_drift_on}
CORE_DRIFT_ON={core_drift_on}
INSTALL_DRIFT_ON={install_drift_on}
ORIGINAL_MONITOR_INSTALL=installed
ORIGINAL_TIMER_INSTALL=installed
ORIGINAL_MONITOR_STATE=failed
ORIGINAL_TIMER_STATE=active
bound_close_now_epoch() {{ printf '%s\n' "$NOW_EPOCH"; }}
sleep() {{
  printf 'sleep\n' >> "$EVENTS"
  NOW_EPOCH=$((NOW_EPOCH + 15))
}}
systemctl() {{
  case "$1:$2" in
    show:telegram-kol-monitor.service)
      if [ "$VERIFY_CALL" -eq "$INSTALL_DRIFT_ON" ]; then
        printf 'not-found\n'
      else
        printf 'loaded\n'
      fi
      ;;
    is-active:telegram-kol-monitor.service)
      printf '%s\n' "${{MONITOR_STATES[$((VERIFY_CALL - 1))]:-unknown}}"
      ;;
    show:telegram-kol-monitor.timer) printf 'loaded\n' ;;
    is-active:telegram-kol-monitor.timer)
      if [ "$VERIFY_CALL" -eq "$TIMER_DRIFT_ON" ]; then
        printf 'inactive\n'
      else
        printf 'active\n'
      fi
      ;;
    *) return 91 ;;
  esac
}}
verify_bound_close_legacy_monitor_live_state() {{
{legacy_verifier}
}}
verify_all_local_identity_before_stop() {{
  VERIFY_CALL=$((VERIFY_CALL + 1))
  printf 'verify\n' >> "$EVENTS"
  verify_bound_close_legacy_monitor_live_state
  [ "$VERIFY_CALL" -ne "$CORE_DRIFT_ON" ]
}}
run_bound_close_writer_quiescence_helper() {{
  local output_path="$1"
  local index="$HELPER_CALL"
  HELPER_CALL=$((HELPER_CALL + 1))
  HELPER_STATUS="${{HELPER_SEQUENCE[$index]:-1}}"
  printf 'helper\n' >> "$EVENTS"
  if [ "$HELPER_STATUS" -eq 0 ]; then
    printf '%s\n' '{{"schema_version":1,"status":"ready"}}' > "$output_path"
  else
    printf '%s\n' '{{"schema_version":1,"status":"refused"}}' > "$output_path"
  fi
}}
project_bound_close_writer_quiescence_result() {{ cp "$1" "$3"; }}
require_exact_ready_projection() {{ grep -q '"status":"ready"' "$1"; }}
require_exact_refused_projection() {{ grep -q '"status":"refused"' "$1"; }}
stop_bound_close_unit_group() {{ printf 'stop\n' >> "$EVENTS"; }}
run_bound_close_double_capture() {{ printf 'capture\n' >> "$EVENTS"; }}
run_bound_close_live_prequiescence() {{
{_bound_close_live_prequiescence_snippet()}
}}
run_bound_close_live_prequiescence
printf 'live-ready\n' >> "$EVENTS"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, events


def _simulate_stopped_recheck(
    tmp_path: Path,
    *,
    helper_status: int,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    events = tmp_path / "stopped-events.txt"
    script = f"""
set -euo pipefail
EVENTS={shlex.quote(str(events))}
RECOVERY_TMP={shlex.quote(str(tmp_path))}
NOW_EPOCH=100
BOUND_CLOSE_SAFE_DIAGNOSTIC=''
QUIESCENCE_DEADLINE_EPOCH=820
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/capture-diagnostic-handoff.json"
WRITER_QUIESCENCE_RAW="$RECOVERY_TMP/writer-quiescence-raw.json"
WRITER_QUIESCENCE_PROJECTION="$RECOVERY_TMP/writer-quiescence-projection.json"
WRITER_QUIESCENCE_RESULT="$RECOVERY_TMP/writer-quiescence.json"
bound_close_now_epoch() {{ printf '%s\n' "$NOW_EPOCH"; }}
sleep() {{
  printf 'sleep\n' >> "$EVENTS"
  NOW_EPOCH=$((NOW_EPOCH + 15))
}}
verify_all_local_quiescence_and_identity() {{ printf 'verify\n' >> "$EVENTS"; }}
run_bound_close_writer_quiescence_helper() {{
  local output_path="$1"
  HELPER_STATUS={helper_status}
  printf 'helper\n' >> "$EVENTS"
  if [ "$HELPER_STATUS" -eq 0 ]; then
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":0,"checked_table_count":20,"fresh_active_or_unknown_writer_count":0,"historical_active_or_unknown_residue_count":2,"missing_table_count":0,"schema_version":1,"status":"ready","target_reservation_count":29,"unrecognized_or_null_state_count":0}}' > "$output_path"
  else
    printf '%s\n' '{{"block_regardless_of_age_writer_count":0,"blocking_writer_count":1,"checked_table_count":20,"fresh_active_or_unknown_writer_count":1,"historical_active_or_unknown_residue_count":1,"missing_table_count":0,"schema_version":1,"status":"refused","target_reservation_count":29,"unrecognized_or_null_state_count":0}}' > "$output_path"
  fi
}}
project_bound_close_writer_quiescence_result() {{
  printf 'project\n' >> "$EVENTS"
  cp "$1" "$3"
}}
require_exact_ready_projection() {{ grep -q '"status":"ready"' "$1"; }}
require_exact_refused_projection() {{ grep -q '"status":"refused"' "$1"; }}
run_bound_close_double_capture_before_deadline() {{
  printf 'capture\n' >> "$EVENTS"
}}
restore() {{ printf 'restore\n' >> "$EVENTS"; }}
trap restore EXIT
printf 'stop\n' >> "$EVENTS"
run_bound_close_stopped_phase() {{
{_bound_close_poll_snippet()}
}}
run_bound_close_stopped_phase
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, events


def test_post_stop_writer_race_restores_without_exchange_capture(tmp_path):
    result, events = _simulate_stopped_recheck(tmp_path, helper_status=2)

    assert result.returncode != 0
    event_lines = events.read_text(encoding="utf-8").splitlines()
    assert event_lines.count("helper") == 1
    assert "sleep" not in event_lines
    assert "capture" not in event_lines
    assert event_lines[0] == "stop"
    assert event_lines[-1] == "restore"


def test_post_stop_writer_ready_invokes_helper_once_then_double_capture(tmp_path):
    result, events = _simulate_stopped_recheck(tmp_path, helper_status=0)

    assert result.returncode == 0, result.stderr
    event_lines = events.read_text(encoding="utf-8").splitlines()
    assert event_lines.count("helper") == 1
    assert event_lines.count("capture") == 1
    assert "sleep" not in event_lines
    assert event_lines[0] == "stop"
    assert event_lines[-1] == "restore"


def test_live_prequiescence_refused_then_ready_never_stops_services(tmp_path):
    result, events = _simulate_live_prequiescence(
        tmp_path,
        helper_statuses=(2, 2, 0),
        sleep_jump=15,
    )

    assert result.returncode == 0, result.stderr
    event_lines = events.read_text(encoding="utf-8").splitlines()
    assert event_lines.count("helper") == 3
    assert event_lines.count("verify") == 6
    assert event_lines.count("sleep") == 2
    assert event_lines[-1] == "live-ready"
    assert "stop" not in event_lines


def test_live_wait_accepts_timer_driven_failed_activating_failed(tmp_path):
    result, events = _simulate_live_monitor_state_transitions(
        tmp_path,
        monitor_states=(
            "failed",
            "failed",
            "activating",
            "activating",
            "failed",
            "failed",
        ),
    )

    assert result.returncode == 0, result.stderr
    event_lines = events.read_text(encoding="utf-8").splitlines()
    assert event_lines.count("helper") == 3
    assert event_lines.count("verify") == 6
    assert event_lines[-1] == "live-ready"
    assert "stop" not in event_lines
    assert "capture" not in event_lines


@pytest.mark.parametrize(
    ("monitor_states", "timer_drift_on", "core_drift_on", "install_drift_on"),
    [
        (("failed",) * 6, 2, 0, 0),
        (("active",) * 6, 0, 0, 0),
        (("failed",) * 6, 0, 2, 0),
        (("failed",) * 6, 0, 0, 2),
    ],
)
def test_live_wait_monitor_exception_counterexamples_fail_before_stop_or_capture(
    tmp_path,
    monitor_states,
    timer_drift_on,
    core_drift_on,
    install_drift_on,
):
    result, events = _simulate_live_monitor_state_transitions(
        tmp_path,
        monitor_states=monitor_states,
        timer_drift_on=timer_drift_on,
        core_drift_on=core_drift_on,
        install_drift_on=install_drift_on,
    )

    assert result.returncode != 0
    event_lines = events.read_text(encoding="utf-8").splitlines()
    assert "stop" not in event_lines
    assert "capture" not in event_lines
    assert "live-ready" not in event_lines


def test_live_prequiescence_timeout_never_stops_services(tmp_path):
    result, events = _simulate_live_prequiescence(
        tmp_path,
        helper_statuses=(2,),
        sleep_jump=720,
    )

    assert result.returncode != 0
    assert "stop" not in events.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("helper_statuses", "projection_fails", "verify_fail_on"),
    [((1,), False, 0), ((2,), True, 0), ((2,), False, 1), ((2,), False, 2)],
)
def test_live_prequiescence_errors_fail_before_any_stop(
    tmp_path,
    helper_statuses,
    projection_fails,
    verify_fail_on,
):
    result, events = _simulate_live_prequiescence(
        tmp_path,
        helper_statuses=helper_statuses,
        sleep_jump=15,
        projection_fails=projection_fails,
        verify_fail_on=verify_fail_on,
    )

    assert result.returncode != 0
    assert "stop" not in events.read_text(encoding="utf-8").splitlines()


def test_runbook_post_stop_recheck_reaches_capture_once_only_when_ready(tmp_path):
    result = _simulate_runbook_quiescence_poll(
        tmp_path,
        helper_statuses=(0,),
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


def test_runbook_refuses_late_ready_without_full_double_capture_budget(tmp_path):
    result = _simulate_runbook_quiescence_poll(
        tmp_path,
        helper_statuses=((2,) * 21) + (0,),
        sleep_jump=15,
    )

    assert result.returncode != 0
    assert (tmp_path / "events.txt").read_text() == "restore\n"


def test_runbook_outer_deadline_interrupts_a_blocked_capture_phase(tmp_path):
    block = _bound_close_read_only_block()
    runner = _bound_close_shell_function(
        block, "run_bound_close_double_capture_before_deadline"
    )
    events = tmp_path / "events.txt"
    timeout_command = tmp_path / "timeout"
    timeout_command.write_text(
        f"""#!{sys.executable}
import os
import signal
import subprocess
import sys

arguments = sys.argv[1:]
deadline_signal = signal.SIGTERM
while arguments and arguments[0].startswith("--"):
    option = arguments.pop(0)
    if option == "--signal=KILL":
        deadline_signal = signal.SIGKILL
duration = float(arguments.pop(0))
process = subprocess.Popen(arguments, start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=duration))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, deadline_signal)
    process.wait()
    raise SystemExit(124)
""",
        encoding="utf-8",
    )
    timeout_command.chmod(0o700)
    script = f"""
set -euo pipefail
PATH={shlex.quote(str(tmp_path))}:$PATH
RECOVERY_TMP={shlex.quote(str(tmp_path))}
EVENTS={shlex.quote(str(events))}
QUIESCENCE_DEADLINE_EPOCH=$(( $(date +%s) + 2 ))
BOUND_CLOSE_CAPTURE_HARD_LIMIT_SECONDS=180
BOUND_CLOSE_CAPTURE_OVERHEAD_RESERVE_SECONDS=60
BOUND_CLOSE_SAFE_DIAGNOSTIC=''
BOUND_CLOSE_DIAGNOSTIC_HANDOFF="$RECOVERY_TMP/diagnostic-handoff.json"
CAPTURE_RUNNER="$RECOVERY_TMP/capture-runner.sh"
printf '%s\n' '#!/bin/bash' "trap '' TERM" \
  {shlex.quote(f"printf 'started\\n' >> {events}")} \
  'sleep 5' > "$CAPTURE_RUNNER"
chmod 0700 "$CAPTURE_RUNNER"
bound_close_now_epoch() {{ date +%s; }}
restore() {{ printf 'restore\n' >> "$EVENTS"; }}
run_bound_close_double_capture_before_deadline() {{
{runner}
}}
trap restore EXIT
run_bound_close_double_capture_before_deadline
"""

    started_at = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=4,
    )

    assert result.returncode != 0
    assert time.monotonic() - started_at < 4
    assert events.read_text(encoding="utf-8") == "started\nrestore\n", result.stderr


def test_runbook_prebuilt_capture_runner_is_private_and_syntax_valid(tmp_path):
    block = _bound_close_read_only_block()
    writer = _bound_close_shell_function(block, "write_bound_close_capture_runner")
    validator = _bound_close_diagnostic_validator_function(block)
    capture = _bound_close_shell_function(block, "run_bound_close_double_capture")
    runner_path = tmp_path / "capture-runner.sh"
    script = f"""
set -euo pipefail
RECOVERY_TMP={shlex.quote(str(tmp_path))}
CANDIDATE_ROOT=/candidate
PRODUCTION_ROOT=/production
RUNTIME_PYTHON=/runtime-python
PRODUCTION_DB=/production/data/research.db
ORIGINAL_SHA={'a' * 40}
PRODUCTION_DB_RESOLVED_PATH=/production/data/research.db
PRODUCTION_DB_DEVICE_INODE=1:2
DB_STAGE_INITIAL_INVENTORY="$RECOVERY_TMP/initial"
DB_STAGE_CURRENT_INVENTORY="$RECOVERY_TMP/current"
declare -a QUIESCE_DB_STAGE_SEED_UNITS=()
declare -a QUIESCE_TIMER_UNITS=()
declare -a QUIESCE_SERVICE_UNITS=()
declare -a QUIESCE_SOCKET_UNITS=()
declare -a ORIGINAL_UNIT_INSTALL_STATE=()
bound_close_now_epoch() {{ date +%s; }}
discover_bound_close_db_stage_units() {{ :; }}
run_bound_close_external_command_before_deadline() {{ "$@"; }}
verify_bound_close_unit_group_inactive() {{ :; }}
verify_bound_close_quiescence() {{ :; }}
verify_all_local_quiescence_and_identity() {{ :; }}
validate_bound_close_capture_diagnostic() {{
{validator}
}}
run_bound_close_double_capture() {{
{capture}
}}
write_bound_close_capture_runner() {{
{writer}
}}
write_bound_close_capture_runner {shlex.quote(str(runner_path))}
bash -n {shlex.quote(str(runner_path))}
stat -f '%Lp' {shlex.quote(str(runner_path))}
"""

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "700\n"


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
    return inspect_bound_close_writer_quiescence(path, now=now)


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


def _project_recovery_output_bytes(
    kind: str,
    document: bytes,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(_RECOVERY_OUTPUT_PROJECTOR), kind],
        input=document,
        capture_output=True,
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


def test_capture_diagnostic_projects_valid_refusal_reason_counts():
    observations = (
        _observation(reservation_ref="1" * 64),
        _observation(
            reservation_ref="2" * 64,
            classification=ReservationClassification.ACTIVE,
            reason_code="exact_close_order_currently_pending",
        ),
        _observation(
            reservation_ref="3" * 64,
            classification=ReservationClassification.ACTIVE,
            reason_code="exact_close_order_currently_pending",
        ),
        _observation(
            reservation_ref="4" * 64,
            classification=ReservationClassification.UNKNOWN,
            reason_code="exchange_history_incomplete",
        ),
        _observation(
            reservation_ref="5" * 64,
            classification=ReservationClassification.UNKNOWN,
            reason_code="exchange_history_incomplete",
        ),
    )
    document = _document(started_at=START, observations=observations)

    result = _project_recovery_output("capture-diagnostic", document)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action_count": 0,
        "counts": {
            "active": 2,
            "proven_terminal": 1,
            "total": 5,
            "unknown": 2,
        },
        "database_writes": 0,
        "exchange_writes": 0,
        "history_replays": 0,
        "reason_counts": {
            "exact_close_and_position_terminal": 1,
            "exact_close_order_currently_pending": 2,
            "exchange_history_incomplete": 2,
        },
        "status": "refused",
    }
    assert sum(json.loads(result.stdout)["reason_counts"].values()) == 5
    assert result.stderr == ""


def test_capture_diagnostic_hides_all_private_capture_authority():
    document = _document(
        started_at=START,
        observations=(
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    )

    result = _project_recovery_output("capture-diagnostic", document)

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == {
        "action_count",
        "counts",
        "database_writes",
        "exchange_writes",
        "history_replays",
        "reason_counts",
        "status",
    }
    for private_value in (
        FP_A,
        FP_B,
        FP_C,
        FP_D,
        "confirmation_token",
        "capture_identity",
        "capture_started_at",
        "capture_completed_at",
        "observations",
        "reservation_ref",
        "source_fingerprint",
        "exchange_fingerprint",
        "evidence_fingerprint",
        "exchange_snapshot_fingerprint",
    ):
        assert private_value not in result.stdout
    assert result.stderr == ""


def _invalid_capture_diagnostic_documents() -> tuple[bytes, ...]:
    valid = _document(
        started_at=START,
        observations=(
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    )
    payload = json.loads(valid)

    unknown_field = dict(payload)
    unknown_field["provider_row"] = "TOPSECRET-PROVIDER-ROW"

    unknown_reason = json.loads(valid)
    unknown_reason["observations"][0]["reason_code"] = "TOPSECRET-UNKNOWN-REASON"

    invalid_schema = json.loads(valid)
    invalid_schema["schema_version"] = True

    duplicate = valid.replace(
        '"action_count":0,',
        '"action_count":0,"action_count":0,',
        1,
    )
    non_finite = valid.replace('"action_count":0', '"action_count":NaN', 1)

    return (
        b"",
        b"{",
        b"\xff\xfe",
        (valid + (" " * (MAX_RECOVERY_PLAN_BYTES + 1))).encode("utf-8"),
        duplicate.encode("utf-8"),
        json.dumps(unknown_field).encode("utf-8"),
        json.dumps(unknown_reason).encode("utf-8"),
        json.dumps(invalid_schema).encode("utf-8"),
        non_finite.encode("utf-8"),
    )


@pytest.mark.parametrize("document", _invalid_capture_diagnostic_documents())
def test_capture_diagnostic_rejects_invalid_input_without_echoing(document):
    result = _project_recovery_output_bytes("capture-diagnostic", document)

    assert result.returncode == 2
    assert result.stdout == b'{"status":"diagnostic_unavailable"}\n'
    assert b"TOPSECRET" not in result.stdout
    assert result.stderr == b""


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
    payload = json.loads(result.stdout)
    assert payload == _inspect_writer_quiescence(
        database,
        now=datetime.now(timezone.utc),
    )
    assert payload == {
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
    helper_text = _WRITER_QUIESCENCE_LIBRARY.read_text(encoding="utf-8")

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
