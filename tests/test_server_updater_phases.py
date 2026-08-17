from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "1" * 40
CANDIDATE = "2" * 40


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def updater_harness(tmp_path):
    application = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    for directory in (
        application / ".git",
        application / ".venv/bin",
        application / "data/web_cache",
        application / "data/backups",
        application / "deploy",
        fake_bin,
        state,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database = application / "data/research.db"
    database.write_bytes(b"sqlite")
    snapshot = application / "data/web_cache/deepcoin_live_positions.json"
    snapshot.write_text("{}", encoding="utf-8")
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
printf 'git %s\n' "$*" >>"$HARNESS_LOG"
if [ "${1:-}" = "-C" ]; then shift 2; fi
case "${1:-}" in
  fetch) exit "${HARNESS_FETCH_RESULT:-0}" ;;
  status)
    if [ "${HARNESS_STATUS_FAIL:-0}" = "1" ]; then exit 1; fi
    if [ "${HARNESS_DIRTY_TRACKED_TREE:-0}" = "1" ]; then
      printf ' M tracked.py\n'
    fi
    ;;
  symbolic-ref)
    printf '%s\n' "${HARNESS_CURRENT_BRANCH:-codex/test}"
    ;;
  rev-parse)
    case "${2:-}" in
      FETCH_HEAD) printf '%s\n' "$HARNESS_CANDIDATE" ;;
      HEAD) if [ -f "$HARNESS_STATE/head" ]; then cat "$HARNESS_STATE/head"; else printf '%s\n' "$HARNESS_PREVIOUS"; fi ;;
      refs/heads/*) if [ -f "$HARNESS_STATE/branch" ]; then cat "$HARNESS_STATE/branch"; else printf '%s\n' "$HARNESS_PREVIOUS"; fi ;;
      *) printf '%s\n' "$HARNESS_CANDIDATE" ;;
    esac
    ;;
  worktree)
    if [ "${2:-}" = "add" ]; then
      mkdir -p "${4}/deploy" "${4}/src"
      printf 'candidate updater\n' >"${4}/deploy/telegram-kol-update"
    else
      /bin/rm -rf -- "${4:-}"
    fi
    ;;
  checkout)
    if [ "${HARNESS_ROLLBACK_CHECKOUT_FAIL:-0}" = "1" ] && [ "${2:-}" = "--detach" ]; then exit 1; fi
    if [ "${HARNESS_CHECKOUT_FAIL:-0}" = "1" ] && [ "${2:-}" != "--detach" ] && [ ! -f "$HARNESS_STATE/checkout_failed" ]; then
      touch "$HARNESS_STATE/checkout_failed"
      exit 1
    fi
    if [ "${2:-}" = "--detach" ]; then
      printf '%s\n' "$3" >"$HARNESS_STATE/head"
    else
      if [ -f "$HARNESS_STATE/branch" ]; then cat "$HARNESS_STATE/branch" >"$HARNESS_STATE/head"; fi
    fi
    ;;
  merge)
    printf '%s\n' "$3" >"$HARNESS_STATE/head"
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
  update-ref)
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
esac
''',
    )
    _write_executable(
        fake_bin / "systemctl",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >>"$HARNESS_LOG"
case "${1:-}" in
  show)
    if [ -f "$HARNESS_STATE/rollback_deactivating" ]; then
      if [ "${HARNESS_ROLLBACK_STOP_STUCK:-0}" = "1" ]; then
        state=deactivating
      elif [ ! -f "$HARNESS_STATE/deactivating_seen" ]; then
        touch "$HARNESS_STATE/deactivating_seen"
        state=deactivating
      else
        rm -f "$HARNESS_STATE/rollback_deactivating"
        touch "$HARNESS_STATE/stopped"
        state=inactive
      fi
    elif [ -f "$HARNESS_STATE/stopped" ]; then
      state=inactive
    else
      state=active
    fi
    printf 'active-state %s\n' "$state" >>"$HARNESS_LOG"
    printf '%s\n' "$state"
    ;;
  is-active)
    if [ "${HARNESS_HEALTH_FAIL:-0}" = "1" ] && [ -f "$HARNESS_STATE/candidate_started" ]; then exit 1; fi
    [ ! -f "$HARNESS_STATE/stopped" ]
    ;;
  stop)
    if [ ! -f "$HARNESS_STATE/first_stop" ]; then
      touch "$HARNESS_STATE/first_stop"
      case "${HARNESS_STOP_MODE:-normal}" in
        normal) touch "$HARNESS_STATE/stopped"; exit 0 ;;
        nonzero_inactive) touch "$HARNESS_STATE/stopped"; exit 1 ;;
        stays_active) exit 0 ;;
        deferred) touch "$HARNESS_STATE/deferred_stop"; exit 124 ;;
        term) kill -TERM "$PPID"; exit 143 ;;
        int) kill -INT "$PPID"; exit 130 ;;
      esac
    fi
    if [ "${HARNESS_ROLLBACK_STOP_DEFERRED:-0}" = "1" ] || [ "${HARNESS_ROLLBACK_STOP_STUCK:-0}" = "1" ]; then
      touch "$HARNESS_STATE/rollback_deactivating"
      exit 124
    fi
    touch "$HARNESS_STATE/stopped"
    ;;
  start)
    if [ "${HARNESS_ROLLBACK_START_FAIL:-0}" = "1" ] && [ -f "$HARNESS_STATE/head" ] && [ "$(cat "$HARNESS_STATE/head")" = "$HARNESS_PREVIOUS" ]; then exit 1; fi
    if [ "${HARNESS_CANDIDATE_START_FAIL:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/start_failed" ]; then
      touch "$HARNESS_STATE/start_failed"
      exit 1
    fi
    if [ -f "$HARNESS_STATE/head" ] && [ "$(cat "$HARNESS_STATE/head")" = "$HARNESS_CANDIDATE" ]; then
      touch "$HARNESS_STATE/candidate_started"
    else
      touch "$HARNESS_STATE/rollback_phase"
      rm -f "$HARNESS_STATE/candidate_started"
    fi
    rm -f "$HARNESS_STATE/stopped" "$HARNESS_STATE/deferred_stop"
    ;;
  status|--no-pager) printf 'active\n' ;;
esac
''',
    )
    _write_executable(
        application / ".venv/bin/python",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'python %s\n' "$*" >>"$HARNESS_LOG"
if [[ " $* " = *" -m pip install "* ]]; then
  if [ "${HARNESS_ROLLBACK_PIP_FAIL:-0}" = "1" ] && [ -f "$HARNESS_STATE/pip_failed" ]; then exit 1; fi
  if [ "${HARNESS_PIP_FAIL_ONCE:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/pip_failed" ]; then
    touch "$HARNESS_STATE/pip_failed"
    exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "-" ]; then
  mode="${2:-}"
  case "$mode" in
    surface) printf '%s\n' "${HARNESS_SCHEMA_CHANGED:-false}" ;;
    schema)
      printf 'schema backup\n' >>"$HARNESS_LOG"
      [ "${HARNESS_SCHEMA_BACKUP_FAIL:-0}" != "1" ] || exit 1
      printf 'schema quick_check\n' >>"$HARNESS_LOG"
      [ "${HARNESS_SCHEMA_QUICK_CHECK_FAIL:-0}" != "1" ] || exit 1
      printf 'schema migration\n' >>"$HARNESS_LOG"
      [ "${HARNESS_SCHEMA_MIGRATION_FAIL:-0}" != "1" ] || exit 1
      touch "$4" "$5"
      printf '{"backup_verified":true,"migration_dry_run_verified":true,"watermark_verified":true}\n' >"$6"
      ;;
    facts)
      printf '{"complete":true,"protected_live_positions":0}\n' >"$5"
      printf '{"raw_messages":1,"execution_events":1}\n' >"$6"
      if [ ! -f "$7" ]; then
        printf '{"backup_verified":false,"migration_dry_run_verified":false,"watermark_verified":false}\n' >"$7"
      fi
      ;;
    fingerprint) printf '%064d\n' 0 | tr '0' 'a' ;;
  esac
  exit 0
fi
if [[ " $* " = *"telegram_kol_research.deployment_preflight_cli"* ]]; then
  command="${3:-}"
  if [ "$command" = "surface" ]; then
    [ "${HARNESS_SURFACE_RESULT:-0}" = "0" ] || exit "$HARNESS_SURFACE_RESULT"
    printf '{"schema_changed":%s,"writer_changed":false}\n' "${HARNESS_SCHEMA_CHANGED:-false}"
    exit 0
  fi
  phase="preliminary"
  output=""
  previous=""
  for value in "$@"; do
    if [ "$previous" = "--phase" ] || [ "$previous" = "--expected-phase" ]; then phase="$value"; fi
    if [ "$previous" = "--output" ]; then output="$value"; fi
    previous="$value"
  done
  if [ "$command" = "collect" ]; then
    if [ -n "$output" ]; then
      mkdir -p "$(dirname "$output")"
      printf '{"fingerprint":"%064d","decision":"PASS","reason_codes":[]}\n' 0 | tr '0' 'a' >"$output"
      chmod 0600 "$output"
    fi
    if [ "$phase" = "preliminary" ]; then exit "${HARNESS_PRELIMINARY_COLLECT_RESULT:-0}"; fi
    exit "${HARNESS_FINAL_COLLECT_RESULT:-0}"
  fi
  if [ "$command" = "verify" ]; then
    if [ "$phase" = "preliminary" ]; then exit "${HARNESS_PRELIMINARY_VERIFY_RESULT:-0}"; fi
    exit "${HARNESS_FINAL_VERIFY_RESULT:-0}"
  fi
fi
exit 0
''',
    )
    (application / ".venv/bin/activate").write_text(
        'PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):$PATH"\nexport PATH\n',
        encoding="utf-8",
    )
    _write_executable(
        fake_bin / "install",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'install %s\n' "$*" >>"$HARNESS_LOG"
if [ "${1:-}" = "-d" ] || [[ " $* " = *" -d "* ]]; then exec /usr/bin/install "$@"; fi
[ "${HARNESS_UPDATER_INSTALL_FAIL:-0}" != "1" ] || exit 1
count=$#
eval "source=\${$((count - 1))}"
eval "target=\${$count}"
/bin/cp "$source" "$target"
chmod 0755 "$target"
''',
    )
    _write_executable(fake_bin / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "timeout",
        '#!/usr/bin/env bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "APP_DIR": str(application),
                "DATABASE_PATH": str(database),
                "LIVE_SNAPSHOT_PATH": str(snapshot),
                "PREFLIGHT_DIR": str(tmp_path / "preflight"),
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
            timeout=10,
        )

    return run, log, state


def _events(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("HARNESS_PRELIMINARY_COLLECT_RESULT", "3"),
        ("HARNESS_PRELIMINARY_COLLECT_RESULT", "4"),
        ("HARNESS_PRELIMINARY_VERIFY_RESULT", "3"),
        ("HARNESS_PRELIMINARY_VERIFY_RESULT", "4"),
    ],
)
def test_phase_a_failure_never_stops_or_mutates(updater_harness, fault, expected):
    run, log, _ = updater_harness
    result = run(**{fault: expected})
    events = _events(log)

    assert result.returncode != 0
    assert "systemctl stop telegram-kol.service" not in events
    assert not any(" checkout codex/test" in event for event in events)
    assert not any("-m pip install -e" in event for event in events)


@pytest.mark.parametrize(
    ("fault", "value"),
    [
        ("HARNESS_STATUS_FAIL", "1"),
        ("HARNESS_DIRTY_TRACKED_TREE", "1"),
        ("HARNESS_CURRENT_BRANCH", "codex/wrong-branch"),
    ],
)
def test_unrollbackable_checkout_is_rejected_before_stop(updater_harness, fault, value):
    run, log, _ = updater_harness
    result = run(**{fault: value})

    assert result.returncode == 4
    assert "systemctl stop telegram-kol.service" not in _events(log)


@pytest.mark.parametrize(
    "fault",
    [
        "HARNESS_SCHEMA_BACKUP_FAIL",
        "HARNESS_SCHEMA_QUICK_CHECK_FAIL",
        "HARNESS_SCHEMA_MIGRATION_FAIL",
    ],
)
def test_schema_failure_never_stops_service(updater_harness, fault):
    run, log, _ = updater_harness
    result = run(HARNESS_SCHEMA_CHANGED="true", **{fault: "1"})

    assert result.returncode != 0
    assert "systemctl stop telegram-kol.service" not in _events(log)


def test_success_order_binds_both_phases_before_mutation(updater_harness):
    run, log, _ = updater_harness
    result = run()
    events = _events(log)

    assert result.returncode == 0, result.stderr
    preliminary = next(i for i, value in enumerate(events) if " collect " in value and " preliminary " in value)
    preliminary_verify = next(i for i, value in enumerate(events) if " verify " in value and " preliminary " in value)
    stop = events.index("systemctl stop telegram-kol.service")
    final = next(i for i, value in enumerate(events) if " collect " in value and " final " in value)
    final_verify = next(i for i, value in enumerate(events) if " verify " in value and " final " in value)
    checkout = next(i for i, value in enumerate(events) if " checkout codex/test" in value)
    package = next(i for i, value in enumerate(events) if "-m pip install -e" in value)
    start = next(i for i, value in enumerate(events) if value == "systemctl start telegram-kol.service")
    updater_install = max(i for i, value in enumerate(events) if value.startswith("install ") and "durable-updater" in value)
    assert preliminary < preliminary_verify < stop < final < final_verify
    assert final_verify < checkout < package < start < updater_install
    final_calls = [value for value in events if " final " in value and (" collect " in value or " verify " in value)]
    assert all("--preliminary-fingerprint" in value for value in final_calls)


@pytest.mark.parametrize(
    ("fault", "value"),
    [
        ("HARNESS_FINAL_COLLECT_RESULT", "3"),
        ("HARNESS_FINAL_COLLECT_RESULT", "4"),
        ("HARNESS_FINAL_VERIFY_RESULT", "3"),
        ("HARNESS_FINAL_VERIFY_RESULT", "4"),
    ],
)
def test_phase_b_failure_restarts_old_service_without_checkout(
    updater_harness, fault, value
):
    run, log, state = updater_harness
    result = run(**{fault: value})
    events = _events(log)

    assert result.returncode != 0
    assert "systemctl start telegram-kol.service" in events
    assert not any(" checkout codex/test" in event for event in events)
    assert not (state / "stopped").exists()


@pytest.mark.parametrize(
    "mode", ["nonzero_inactive", "stays_active", "deferred", "term", "int"]
)
def test_every_stop_failure_reasserts_old_service_active(updater_harness, mode):
    run, log, state = updater_harness
    result = run(HARNESS_STOP_MODE=mode)
    events = _events(log)

    assert result.returncode != 0
    assert "systemctl start telegram-kol.service" in events
    assert not any(" collect " in event and " final " in event for event in events)
    assert not (state / "stopped").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "HARNESS_CHECKOUT_FAIL",
        "HARNESS_PIP_FAIL_ONCE",
        "HARNESS_CANDIDATE_START_FAIL",
        "HARNESS_HEALTH_FAIL",
        "HARNESS_UPDATER_INSTALL_FAIL",
    ],
)
def test_post_checkout_failure_restores_checkout_package_and_service(
    updater_harness, fault
):
    run, log, state = updater_harness
    result = run(**{fault: "1"})
    events = _events(log)

    assert result.returncode != 0
    assert (state / "head").read_text(encoding="utf-8").strip() == PREVIOUS
    assert (state / "branch").read_text(encoding="utf-8").strip() == PREVIOUS
    assert "systemctl start telegram-kol.service" in events
    assert not (state / "stopped").exists()
    if fault in {"HARNESS_CANDIDATE_START_FAIL", "HARNESS_HEALTH_FAIL"}:
        assert events.count("systemctl stop telegram-kol.service") >= 2


def test_rollback_waits_for_deactivating_unit_to_become_inactive(updater_harness):
    run, log, state = updater_harness
    result = run(
        HARNESS_PIP_FAIL_ONCE="1",
        HARNESS_ROLLBACK_STOP_DEFERRED="1",
    )
    events = _events(log)

    assert result.returncode != 0
    deactivating = events.index("active-state deactivating")
    inactive = events.index("active-state inactive", deactivating + 1)
    rollback_checkout = next(
        index
        for index, event in enumerate(events)
        if event.endswith(f" checkout --detach {PREVIOUS}")
    )
    assert deactivating < inactive < rollback_checkout
    assert (state / "head").read_text(encoding="utf-8").strip() == PREVIOUS


def test_rollback_never_mutates_files_while_unit_remains_deactivating(
    updater_harness,
):
    run, log, state = updater_harness
    result = run(
        HARNESS_PIP_FAIL_ONCE="1",
        HARNESS_ROLLBACK_STOP_STUCK="1",
    )
    events = _events(log)

    assert result.returncode == 4
    assert "active-state deactivating" in events
    assert not any(
        event.endswith(f" checkout --detach {PREVIOUS}") for event in events
    )
    assert (state / "head").read_text(encoding="utf-8").strip() == CANDIDATE
    assert "ROLLBACK FAILED" in result.stderr


@pytest.mark.parametrize(
    "fault",
    ["HARNESS_ROLLBACK_CHECKOUT_FAIL", "HARNESS_ROLLBACK_PIP_FAIL", "HARNESS_ROLLBACK_START_FAIL"],
)
def test_rollback_failure_is_hard_failure(updater_harness, fault):
    run, _, _ = updater_harness
    trigger = {"HARNESS_PIP_FAIL_ONCE": "1", fault: "1"}
    result = run(**trigger)

    assert result.returncode == 4
    assert "ROLLBACK FAILED" in result.stderr or "could not restore" in result.stderr


def test_warn_continues_only_through_successful_verification(updater_harness):
    run, log, _ = updater_harness
    accepted = run(
        HARNESS_PRELIMINARY_COLLECT_RESULT="2",
        HARNESS_PRELIMINARY_VERIFY_RESULT="2",
        HARNESS_FINAL_COLLECT_RESULT="2",
        HARNESS_FINAL_VERIFY_RESULT="2",
    )
    assert accepted.returncode == 0, accepted.stderr
    assert any(" checkout codex/test" in value for value in _events(log))

    rejected = run(
        HARNESS_PRELIMINARY_COLLECT_RESULT="2",
        HARNESS_PRELIMINARY_VERIFY_RESULT="4",
    )
    assert rejected.returncode == 4
    assert "systemctl stop telegram-kol.service" not in _events(log)

    mismatched = run(
        HARNESS_PRELIMINARY_COLLECT_RESULT="2",
        HARNESS_PRELIMINARY_VERIFY_RESULT="0",
    )
    assert mismatched.returncode == 4
    assert "systemctl stop telegram-kol.service" not in _events(log)
