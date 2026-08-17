from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "1" * 40
CANDIDATE = "2" * 40
OTHER = "3" * 40


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def updater_harness(tmp_path: Path):
    application = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    for directory in (
        application / ".git",
        application / ".venv/bin",
        application / "data/backups",
        fake_bin,
        state,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database = application / "data/research.db"
    database.write_bytes(b"sqlite")
    durable_updater = tmp_path / "durable-updater"
    durable_updater.write_text("old updater\n", encoding="utf-8")
    durable_updater.chmod(0o755)
    log = tmp_path / "events.log"
    (state / "head").write_text(f"{PREVIOUS}\n", encoding="utf-8")
    (state / "branch").write_text(f"{PREVIOUS}\n", encoding="utf-8")

    _write_executable(
        fake_bin / "git",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-C" ]; then shift 2; fi
case "${1:-}" in
  fetch)
    printf 'fetch\n' >>"$HARNESS_LOG"
    exit "${HARNESS_FETCH_RC:-0}"
    ;;
  status)
    printf 'status\n' >>"$HARNESS_LOG"
    [ "${HARNESS_STATUS_FAIL:-0}" != "1" ] || exit 1
    [ "${HARNESS_DIRTY:-0}" != "1" ] || printf ' M tracked.py\n'
    ;;
  symbolic-ref)
    printf '%s\n' "${HARNESS_CURRENT_BRANCH:-codex/test}"
    ;;
  rev-parse)
    case "${2:-}" in
      FETCH_HEAD) printf '%s\n' "${HARNESS_REMOTE_HEAD:-$HARNESS_CANDIDATE}" ;;
      HEAD) cat "$HARNESS_STATE/head" ;;
      refs/heads/*)
        if [ -n "${HARNESS_BRANCH_HEAD:-}" ]; then
          printf '%s\n' "$HARNESS_BRANCH_HEAD"
        else
          cat "$HARNESS_STATE/branch"
        fi
        ;;
      *) printf '%s\n' "$HARNESS_CANDIDATE" ;;
    esac
    ;;
  worktree)
    if [ "${2:-}" = "add" ]; then
      printf 'worktree-add\n' >>"$HARNESS_LOG"
      [ "${HARNESS_WORKTREE_FAIL:-0}" != "1" ] || exit 1
      mkdir -p "${4}/deploy" "${4}/src/telegram_kol_research"
      printf 'candidate updater\n' >"${4}/deploy/telegram-kol-update"
      touch "$HARNESS_STATE/worktree_registered"
    else
      printf 'worktree-remove\n' >>"$HARNESS_LOG"
      [ -f "$HARNESS_STATE/worktree_registered" ] || exit 1
      if [ -n "${HARNESS_WORKTREE_REMOVE_SIGNAL:-}" ] && [ ! -f "$HARNESS_STATE/worktree_remove_signaled" ]; then
        touch "$HARNESS_STATE/worktree_remove_signaled"
        kill -"$HARNESS_WORKTREE_REMOVE_SIGNAL" "$PPID"
        exit 128
      fi
      [ "${HARNESS_WORKTREE_REMOVE_FAIL:-0}" != "1" ] || exit 1
      rm -f "$HARNESS_STATE/worktree_registered"
      /bin/rm -rf -- "${4:-}"
    fi
    ;;
  diff)
    printf 'schema-diff\n' >>"$HARNESS_LOG"
    [ "${HARNESS_DIFF_ERROR:-0}" != "1" ] || exit 2
    case "${HARNESS_CHANGED_PATH:-}" in
      src/telegram_kol_research/models.py|src/telegram_kol_research/db.py|migrations/*)
        exit 1
        ;;
      *) exit 0 ;;
    esac
    ;;
  checkout)
    if [ "${2:-}" = "--detach" ]; then
      printf 'rollback-checkout\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_CHECKOUT_FAIL:-0}" != "1" ] || exit 1
      printf '%s\n' "$3" >"$HARNESS_STATE/head"
    else
      printf 'checkout\n' >>"$HARNESS_LOG"
      if [ "${HARNESS_CHECKOUT_FAIL:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/checkout_failed" ]; then
        touch "$HARNESS_STATE/checkout_failed"
        exit 1
      fi
      cat "$HARNESS_STATE/branch" >"$HARNESS_STATE/head"
    fi
    ;;
  merge)
    printf 'fast-forward\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MERGE_FAIL:-0}" != "1" ] || exit 1
    printf '%s\n' "$3" >"$HARNESS_STATE/head"
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
  update-ref)
    printf 'rollback-ref\n' >>"$HARNESS_LOG"
    [ "${HARNESS_ROLLBACK_REF_FAIL:-0}" != "1" ] || exit 1
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
esac
''',
    )
    _write_executable(
        fake_bin / "systemctl",
        r'''#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  show)
    if [ -f "$HARNESS_STATE/rollback_pending" ]; then
      if [ "${HARNESS_ROLLBACK_STOP_MODE:-normal}" = "stuck" ]; then
        unit_state=deactivating
      elif [ ! -f "$HARNESS_STATE/rollback_deactivating_seen" ]; then
        touch "$HARNESS_STATE/rollback_deactivating_seen"
        unit_state=deactivating
      else
        rm -f "$HARNESS_STATE/rollback_pending"
        touch "$HARNESS_STATE/stopped"
        unit_state=inactive
      fi
    elif [ -f "$HARNESS_STATE/pending_stop" ]; then
      if [ "${HARNESS_STOP_MODE:-normal}" = "stuck" ]; then
        unit_state=deactivating
      elif [ ! -f "$HARNESS_STATE/deactivating_seen" ]; then
        touch "$HARNESS_STATE/deactivating_seen"
        unit_state=deactivating
      else
        rm -f "$HARNESS_STATE/pending_stop"
        touch "$HARNESS_STATE/stopped"
        unit_state=inactive
      fi
    elif [ -f "$HARNESS_STATE/stopped" ]; then
      unit_state=inactive
    else
      unit_state=active
    fi
    printf '%s\n' "$unit_state" >>"$HARNESS_LOG"
    printf '%s\n' "$unit_state"
    ;;
  is-active)
    head_value="$(cat "$HARNESS_STATE/head")"
    if [ -f "$HARNESS_STATE/candidate_started" ]; then
      printf 'is-active\n' >>"$HARNESS_LOG"
      [ "${HARNESS_CANDIDATE_ACTIVE_FAIL:-0}" != "1" ] || exit 1
    elif [ -f "$HARNESS_STATE/rollback_started" ]; then
      printf 'rollback-is-active\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_ACTIVE_FAIL:-0}" != "1" ] || exit 1
    else
      printf 'initial-is-active\n' >>"$HARNESS_LOG"
    fi
    [ ! -f "$HARNESS_STATE/stopped" ]
    [ ! -f "$HARNESS_STATE/pending_stop" ]
    [ ! -f "$HARNESS_STATE/rollback_pending" ]
    ;;
  stop)
    printf 'stop\n' >>"$HARNESS_LOG"
    if [ ! -f "$HARNESS_STATE/first_stop" ]; then
      touch "$HARNESS_STATE/first_stop"
      case "${HARNESS_STOP_MODE:-normal}" in
        normal) touch "$HARNESS_STATE/stopped" ;;
        fail) exit 1 ;;
        nonzero_inactive) touch "$HARNESS_STATE/stopped"; exit 1 ;;
        deactivating_once|stuck) touch "$HARNESS_STATE/pending_stop" ;;
        term) kill -TERM "$PPID"; exit 143 ;;
        int) kill -INT "$PPID"; exit 130 ;;
      esac
    else
      case "${HARNESS_ROLLBACK_STOP_MODE:-normal}" in
        normal) touch "$HARNESS_STATE/stopped" ;;
        deactivating_once|stuck) touch "$HARNESS_STATE/rollback_pending" ;;
        fail) exit 1 ;;
      esac
    fi
    ;;
  start)
    head_value="$(cat "$HARNESS_STATE/head")"
    if [ "$head_value" = "$HARNESS_CANDIDATE" ]; then
      printf 'start\n' >>"$HARNESS_LOG"
      [ "${HARNESS_CANDIDATE_START_FAIL:-0}" != "1" ] || exit 1
      touch "$HARNESS_STATE/candidate_started"
      rm -f "$HARNESS_STATE/rollback_started"
    else
      printf 'rollback-start\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_START_FAIL:-0}" != "1" ] || exit 1
      touch "$HARNESS_STATE/rollback_started"
      rm -f "$HARNESS_STATE/candidate_started"
    fi
    rm -f "$HARNESS_STATE/stopped" "$HARNESS_STATE/pending_stop" "$HARNESS_STATE/rollback_pending"
    ;;
esac
''',
    )
    _write_executable(
        application / ".venv/bin/python",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "telegram_kol_research.deployment_active_write_check" ]; then
  case "${PYTHONPATH:-}" in
    */telegram-kol-stage.*/src) ;;
    *) printf 'wrong-pythonpath\n' >>"$HARNESS_LOG"; exit 4 ;;
  esac
  count_file="$HARNESS_STATE/active_check_count"
  count=0
  [ ! -f "$count_file" ] || count="$(cat "$count_file")"
  count=$((count + 1))
  printf '%s\n' "$count" >"$count_file"
  printf 'active-check-%s\n' "$count" >>"$HARNESS_LOG"
  [ "$count" -le 2 ] || exit 99
  if [ "$count" -eq 1 ]; then
    rc="${HARNESS_PRESTOP_ACTIVE_RC:-0}"
    output="${HARNESS_PRESTOP_ACTIVE_OUTPUT:-}"
  else
    rc="${HARNESS_POSTSTOP_ACTIVE_RC:-0}"
    output="${HARNESS_POSTSTOP_ACTIVE_OUTPUT:-}"
  fi
  if [ -n "$output" ]; then printf '%s\n' "$output"; fi
  case "$rc" in
    0) [ -n "$output" ] || printf 'active_write_count=0\n' ;;
    3) [ -n "$output" ] || printf 'active_write_count=1\n' ;;
    *) printf 'ERROR active_write_check_failed\n' >&2 ;;
  esac
  exit "$rc"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  if [ "$(cat "$HARNESS_STATE/head")" = "$HARNESS_CANDIDATE" ]; then
    printf 'pip-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_PIP_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'rollback-pip\n' >>"$HARNESS_LOG"
    [ "${HARNESS_ROLLBACK_PIP_FAIL:-0}" != "1" ] || exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "-" ] && [ "${2:-}" = "schema" ]; then
  printf 'online-backup\n' >>"$HARNESS_LOG"
  [ "${HARNESS_SCHEMA_BACKUP_FAIL:-0}" != "1" ] || exit 1
  printf 'backup-quick-check\n' >>"$HARNESS_LOG"
  [ "${HARNESS_BACKUP_CHECK_FAIL:-0}" != "1" ] || exit 1
  printf 'disposable-migration\n' >>"$HARNESS_LOG"
  [ "${HARNESS_MIGRATION_FAIL:-0}" != "1" ] || exit 1
  printf 'migration-quick-check\n' >>"$HARNESS_LOG"
  [ "${HARNESS_MIGRATION_CHECK_FAIL:-0}" != "1" ] || exit 1
  printf 'watermark-compare\n' >>"$HARNESS_LOG"
  [ "${HARNESS_WATERMARK_FAIL:-0}" != "1" ] || exit 1
  touch "$4" "$5"
  exit 0
fi
exit 64
''',
    )
    _write_executable(
        fake_bin / "install",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-d" ] || [[ " $* " = *" -d "* ]]; then exec /usr/bin/install "$@"; fi
count=$#
eval "source=\${$((count - 1))}"
eval "target=\${$count}"
if [[ "$target" = *.candidate.* ]]; then
  printf 'durable-updater-install\n' >>"$HARNESS_LOG"
  [ "${HARNESS_DURABLE_INSTALL_FAIL:-0}" != "1" ] || exit 1
else
  printf 'updater-restore\n' >>"$HARNESS_LOG"
  [ "${HARNESS_UPDATER_RESTORE_FAIL:-0}" != "1" ] || exit 1
fi
/bin/cp "$source" "$target"
chmod 0755 "$target"
''',
    )
    _write_executable(
        fake_bin / "mv",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'durable-updater-move\n' >>"$HARNESS_LOG"
[ "${HARNESS_DURABLE_MOVE_FAIL:-0}" != "1" ] || exit 1
if [ "${HARNESS_DURABLE_MOVE_FAIL_AFTER:-0}" = "1" ]; then
  /bin/mv "$@"
  exit 1
fi
exec /bin/mv "$@"
''',
    )
    _write_executable(
        fake_bin / "curl",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'http-health\n' >>"$HARNESS_LOG"
[ "${HARNESS_HTTP_FAIL:-0}" != "1" ]
''',
    )
    _write_executable(
        fake_bin / "flock",
        '#!/usr/bin/env bash\n[ "${HARNESS_FLOCK_FAIL:-0}" != "1" ]\n',
    )
    _write_executable(
        fake_bin / "rm",
        r'''#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *telegram-kol-updater-backup.*)
    [ "${HARNESS_FINAL_BACKUP_REMOVE_FAIL:-0}" != "1" ] || exit 1
    ;;
  *schema-dry-run-*)
    [ "${HARNESS_FINAL_DRY_RUN_REMOVE_FAIL:-0}" != "1" ] || exit 1
    ;;
esac
exec /bin/rm "$@"
''',
    )
    _write_executable(
        fake_bin / "timeout",
        '#!/usr/bin/env bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "APP_DIR": str(application),
                "DATABASE_PATH": str(database),
                "LOCK_PATH": str(tmp_path / "update.lock"),
                "STAGE_PARENT": str(tmp_path),
                "UPDATER_PATH": str(durable_updater),
                "EXPECTED_COMMIT": CANDIDATE,
                "BRANCH": "codex/test",
                "STOP_TIMEOUT_SECONDS": "1",
                "HARNESS_LOG": str(log),
                "HARNESS_STATE": str(state),
                "HARNESS_PREVIOUS": PREVIOUS,
                "HARNESS_CANDIDATE": CANDIDATE,
            }
        )
        environment.update(overrides)
        return subprocess.run(
            ["bash", str(ROOT / "deploy/telegram-kol-update")],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=15,
        )

    return run, log, state


def _events(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


def test_success_has_exact_short_authorization_order(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    relevant = [
        event
        for event in _events(log)
        if event
        in {
            "fetch",
            "worktree-add",
            "schema-diff",
            "active-check-1",
            "stop",
            "inactive",
            "active-check-2",
            "checkout",
            "fast-forward",
            "pip-install",
            "start",
            "is-active",
            "http-health",
            "durable-updater-install",
            "durable-updater-move",
            "worktree-remove",
        }
    ]
    assert relevant == [
        "fetch",
        "worktree-add",
        "schema-diff",
        "active-check-1",
        "stop",
        "inactive",
        "active-check-2",
        "checkout",
        "fast-forward",
        "pip-install",
        "start",
        "is-active",
        "http-health",
        "durable-updater-install",
        "durable-updater-move",
        "worktree-remove",
    ]


def test_success_uses_candidate_source_and_exactly_two_checks(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    assert "wrong-pythonpath" not in _events(log)
    assert (state / "active_check_count").read_text().strip() == "2"


def test_updater_has_no_retired_gate_mechanism_or_event(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    combined = "\n".join(_events(log)) + (ROOT / "deploy/telegram-kol-update").read_text()
    forbidden = (
        "deployment_preflight_cli",
        "surface",
        "snapshot",
        "watermark_artifact",
        "preliminary",
        "final_artifact",
        "fingerprint",
        "WARN",
        "BLOCK",
    )
    assert all(value not in combined for value in forbidden)


@pytest.mark.parametrize(("rc", "expected_rc"), (("3", 3), ("4", 4)))
def test_prestop_nonzero_refuses_without_stop_or_mutation(
    updater_harness,
    rc: str,
    expected_rc: int,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_PRESTOP_ACTIVE_RC=rc)

    assert result.returncode == expected_rc
    events = _events(log)
    assert "stop" not in events
    assert "checkout" not in events
    assert "pip-install" not in events
    assert "start" not in events


def test_malformed_zero_output_fails_closed_before_stop(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_PRESTOP_ACTIVE_OUTPUT="active_write_count=00")

    assert result.returncode == 4
    assert "stop" not in _events(log)


@pytest.mark.parametrize(("rc", "expected_rc"), (("3", 3), ("4", 4)))
def test_poststop_nonzero_restarts_old_service_without_mutation(
    updater_harness,
    rc: str,
    expected_rc: int,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_POSTSTOP_ACTIVE_RC=rc)

    assert result.returncode == expected_rc
    events = _events(log)
    assert events.index("inactive") < events.index("active-check-2")
    assert "checkout" not in events
    assert "pip-install" not in events
    assert "rollback-start" in events
    assert "rollback-is-active" in events
    assert not (state / "stopped").exists()


def test_delayed_stop_waits_for_inactive_before_second_check(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE="deactivating_once")

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert events.index("deactivating") < events.index("inactive")
    assert events.index("inactive") < events.index("active-check-2")
    assert events.index("active-check-2") < events.index("checkout")


def test_permanent_deactivating_is_hard_failure_without_checkout(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE="stuck")

    assert result.returncode == 4
    events = _events(log)
    assert "deactivating" in events
    assert "active-check-2" not in events
    assert "checkout" not in events


@pytest.mark.parametrize("mode", ("fail", "nonzero_inactive", "term", "int"))
def test_stop_fault_reasserts_old_service_without_checkout(
    updater_harness,
    mode: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE=mode)

    expected = {"fail": 4, "nonzero_inactive": 4, "term": 143, "int": 130}
    assert result.returncode == expected[mode]
    events = _events(log)
    assert "checkout" not in events
    assert "rollback-start" in events


@pytest.mark.parametrize(
    "changed_path",
    (
        "src/telegram_kol_research/models.py",
        "src/telegram_kol_research/db.py",
        "migrations/001_example.py",
    ),
)
def test_schema_paths_run_backup_migration_checks_before_first_active_check(
    updater_harness,
    changed_path: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_CHANGED_PATH=changed_path)

    assert result.returncode == 0, result.stderr
    events = _events(log)
    schema_events = [
        "online-backup",
        "backup-quick-check",
        "disposable-migration",
        "migration-quick-check",
        "watermark-compare",
    ]
    assert [event for event in events if event in schema_events] == schema_events
    assert events.index("watermark-compare") < events.index("active-check-1")


@pytest.mark.parametrize(
    "changed_path",
    ("src/telegram_kol_research/web.py", "docs/server-deployment.md"),
)
def test_nonschema_paths_skip_all_schema_work(
    updater_harness,
    changed_path: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_CHANGED_PATH=changed_path)

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert "online-backup" not in events
    assert "disposable-migration" not in events
    assert "watermark-compare" not in events


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_SCHEMA_BACKUP_FAIL",
        "HARNESS_BACKUP_CHECK_FAIL",
        "HARNESS_MIGRATION_FAIL",
        "HARNESS_MIGRATION_CHECK_FAIL",
        "HARNESS_WATERMARK_FAIL",
    ),
)
def test_schema_fault_never_reaches_active_check_or_stop(
    updater_harness,
    fault: str,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_CHANGED_PATH="src/telegram_kol_research/models.py",
        **{fault: "1"},
    )

    assert result.returncode == 4
    events = _events(log)
    assert "active-check-1" not in events
    assert "stop" not in events


@pytest.mark.parametrize(
    ("fault", "value"),
    (
        ("HARNESS_REMOTE_HEAD", OTHER),
        ("HARNESS_FETCH_RC", "1"),
        ("HARNESS_FLOCK_FAIL", "1"),
        ("HARNESS_STATUS_FAIL", "1"),
        ("HARNESS_DIRTY", "1"),
        ("HARNESS_CURRENT_BRANCH", "codex/wrong"),
        ("HARNESS_BRANCH_HEAD", OTHER),
        ("HARNESS_WORKTREE_FAIL", "1"),
        ("HARNESS_DIFF_ERROR", "1"),
    ),
)
def test_precondition_fault_never_stops_or_mutates(
    updater_harness,
    fault: str,
    value: str,
) -> None:
    run, log, _ = updater_harness

    result = run(**{fault: value})

    assert result.returncode == 4
    events = _events(log)
    assert "stop" not in events
    assert "checkout" not in events


def test_failed_worktree_add_removes_the_exact_empty_stage_directory(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_WORKTREE_FAIL="1")

    assert result.returncode == 4
    assert not list(log.parent.glob("telegram-kol-stage.*"))


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_CHECKOUT_FAIL",
        "HARNESS_MERGE_FAIL",
        "HARNESS_PIP_FAIL",
        "HARNESS_CANDIDATE_START_FAIL",
        "HARNESS_CANDIDATE_ACTIVE_FAIL",
        "HARNESS_HTTP_FAIL",
        "HARNESS_DURABLE_INSTALL_FAIL",
        "HARNESS_DURABLE_MOVE_FAIL",
    ),
)
def test_poststop_fault_restores_old_checkout_package_updater_and_service(
    updater_harness,
    fault: str,
) -> None:
    run, log, state = updater_harness

    result = run(**{fault: "1"})

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS
    events = _events(log)
    assert "rollback-start" in events
    assert "rollback-is-active" in events
    if fault in {"HARNESS_DURABLE_MOVE_FAIL"}:
        assert "updater-restore" in events


def test_rollback_waits_for_inactive_before_restoring_files(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_PIP_FAIL="1",
        HARNESS_ROLLBACK_STOP_MODE="deactivating_once",
    )

    assert result.returncode != 0
    events = _events(log)
    rollback_stop = len(events) - 1 - events[::-1].index("stop")
    deactivating = events.index("deactivating", rollback_stop + 1)
    inactive = events.index("inactive", deactivating + 1)
    checkout = events.index("rollback-checkout")
    assert rollback_stop < deactivating < inactive < checkout


def test_rollback_never_restores_files_while_unit_is_deactivating(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(
        HARNESS_PIP_FAIL="1",
        HARNESS_ROLLBACK_STOP_MODE="stuck",
    )

    assert result.returncode == 4
    events = _events(log)
    assert "deactivating" in events
    assert "rollback-checkout" not in events
    assert (state / "head").read_text().strip() == CANDIDATE
    assert "ROLLBACK FAILED" in result.stderr


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_ROLLBACK_CHECKOUT_FAIL",
        "HARNESS_ROLLBACK_REF_FAIL",
        "HARNESS_ROLLBACK_PIP_FAIL",
        "HARNESS_ROLLBACK_START_FAIL",
        "HARNESS_ROLLBACK_ACTIVE_FAIL",
        "HARNESS_UPDATER_RESTORE_FAIL",
    ),
)
def test_rollback_failure_is_hard_failure(
    updater_harness,
    fault: str,
) -> None:
    run, _, _ = updater_harness

    result = run(HARNESS_DURABLE_MOVE_FAIL="1", **{fault: "1"})

    assert result.returncode == 4
    assert "ROLLBACK FAILED" in result.stderr


@pytest.mark.parametrize(
    ("fault", "extra"),
    (
        ("HARNESS_WORKTREE_REMOVE_FAIL", {}),
        ("HARNESS_FINAL_BACKUP_REMOVE_FAIL", {}),
        (
            "HARNESS_FINAL_DRY_RUN_REMOVE_FAIL",
            {"HARNESS_CHANGED_PATH": "src/telegram_kol_research/models.py"},
        ),
    ),
)
def test_final_cleanup_failure_rolls_back_instead_of_applying_candidate(
    updater_harness,
    fault: str,
    extra: dict[str, str],
) -> None:
    run, _, state = updater_harness

    result = run(**{fault: "1"}, **extra)

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS


def test_hard_rollback_failure_preserves_recovery_materials(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_DURABLE_MOVE_FAIL_AFTER="1",
        HARNESS_UPDATER_RESTORE_FAIL="1",
    )

    assert result.returncode == 4
    assert "ROLLBACK FAILED" in result.stderr
    assert list(log.parent.glob("telegram-kol-updater-backup.*"))
    assert list(log.parent.glob("telegram-kol-stage.*"))
    assert "worktree-remove" not in _events(log)
    assert (log.parent / "durable-updater").read_text() == "candidate updater\n"


@pytest.mark.parametrize(("signal", "expected"), (("TERM", 143), ("INT", 130)))
def test_signal_during_final_cleanup_rolls_back_candidate(
    updater_harness,
    signal: str,
    expected: int,
) -> None:
    run, _, state = updater_harness

    result = run(HARNESS_WORKTREE_REMOVE_SIGNAL=signal)

    assert result.returncode == expected
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS


def test_http_health_is_bounded_and_uses_required_endpoint() -> None:
    source = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8000/api/trading-settings" in source
    assert "--max-time 2" in source
    assert "-o /dev/null" in source
    assert "20" in source
    assert "sleep 0.5" in source
